"""加密的 API 密钥管理。

三层架构：
    1. RSA 2048 密钥对：私钥存系统 keyring（用户级访问保护），公钥存文件
    2. AES-256 主密钥（K）：随机生成，用 RSA 公钥加密后存 secrets.meta
    3. 用户密钥：每条用 K + 随机 nonce 通过 AES-GCM 加密后存 secrets.enc

用户体验：
    - 首次启动自动生成所有密钥基础设施，无需输入密码
    - 后续启动自动从 keyring 解密 K → 解密所有用户密钥
    - 用户只看到密钥的逻辑 ID（如 deepseek_main），实际密文对用户不可见

安全特性：
    - AES-GCM 提供认证加密：篡改 ciphertext 会立即被发现
    - 每条密钥独立的 nonce + associated_data（key_id），防重放
    - 即使 secrets.enc 被偷走，没有 RSA 私钥无法解密
    - RSA 私钥保存在系统密钥环，仅当前用户可访问

风险提示：
    - keyring 数据丢失（重装系统、用户删除）= 所有密钥永久无法解密
    - 推荐用户定期导出 RSA 私钥并妥善保管
"""

from __future__ import annotations

import base64
import logging
import os
import secrets as _stdlib_secrets

import orjson
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import keyring

from .paths import AppPaths

logger = logging.getLogger(__name__)


class SecretsError(Exception):
    """密钥管理相关异常。"""


class SecretsManager:
    """API 密钥的加密存储管理器。

    用法：
        sm = SecretsManager(paths)
        sm.initialize()                            # 首次自动建立基础设施
        sm.set("deepseek_main", "sk-...")          # 加密保存
        key = sm.get("deepseek_main")              # 解密读取
        sm.list_ids()                              # 列出所有 ID（不含密文）
        sm.delete("deepseek_main")
    """

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._aes_key: bytes | None = None
        # secrets.enc 内存中的明文索引：{key_id: base64(nonce + ciphertext)}
        self._records: dict[str, str] = {}
        self._initialized: bool = False

    # ============================================================
    # 公开 API
    # ============================================================

    def initialize(self) -> None:
        """加载或生成密钥基础设施。必须在使用 set/get 前调用。"""
        self.paths.DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not self._rsa_keys_exist():
            logger.info("首次启动：生成 RSA 密钥对")
            self._generate_rsa_keys()

        if not self.paths.SECRETS_META_FILE.exists():
            logger.info("首次启动：生成 AES 主密钥")
            self._generate_aes_master_key()

        self._aes_key = self._load_aes_master_key()
        self._records = self._load_records()
        self._initialized = True
        logger.info(f"密钥管理器已加载，{len(self._records)} 条密钥")

    def set(self, key_id: str, plaintext: str) -> None:
        """加密并保存一条密钥。已存在则覆盖。"""
        self._require_initialized()
        if not key_id:
            raise SecretsError("key_id 不能为空")
        if not plaintext:
            raise SecretsError("plaintext 不能为空")

        aesgcm = AESGCM(self._aes_key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(
            nonce,
            plaintext.encode("utf-8"),
            associated_data=key_id.encode("utf-8"),
        )
        self._records[key_id] = base64.b64encode(nonce + ciphertext).decode("ascii")
        self._save_records()
        logger.debug(f"密钥已保存: {key_id}")

    def get(self, key_id: str) -> str | None:
        """解密读取一条密钥。不存在或解密失败返回 None。"""
        self._require_initialized()
        if key_id not in self._records:
            return None

        blob = base64.b64decode(self._records[key_id])
        if len(blob) < 13:  # nonce(12) + 至少 1 字节密文
            logger.warning(f"密钥 {key_id} 数据损坏")
            return None

        nonce, ciphertext = blob[:12], blob[12:]
        aesgcm = AESGCM(self._aes_key)
        try:
            return aesgcm.decrypt(
                nonce, ciphertext, associated_data=key_id.encode("utf-8")
            ).decode("utf-8")
        except Exception as e:
            logger.error(f"密钥 {key_id} 解密失败: {type(e).__name__}")
            return None

    def get_required(self, key_id: str) -> str:
        """解密读取一条密钥。不存在或失败抛异常。"""
        val = self.get(key_id)
        if val is None:
            raise SecretsError(f"必需的密钥 {key_id} 不存在或损坏")
        return val

    def delete(self, key_id: str) -> bool:
        """删除一条密钥。返回是否实际删除了。"""
        self._require_initialized()
        if key_id in self._records:
            del self._records[key_id]
            self._save_records()
            logger.info(f"密钥已删除: {key_id}")
            return True
        return False

    def list_ids(self) -> list[str]:
        """列出所有密钥 ID。"""
        self._require_initialized()
        return sorted(self._records.keys())

    def has(self, key_id: str) -> bool:
        """检查密钥是否存在（不解密）。"""
        self._require_initialized()
        return key_id in self._records

    def export_rsa_private_key(self) -> str:
        """导出 RSA 私钥（PEM 格式字符串），供用户备份。

        警告：调用方必须明确告知用户妥善保管，私钥泄露即所有密钥泄露。
        """
        self._require_initialized()
        pem_str = self._load_rsa_private_pem()
        if pem_str is None:
            raise SecretsError("RSA 私钥已从系统密钥环丢失")
        return pem_str

    def import_rsa_private_key(self, private_pem: str) -> None:
        """从备份恢复 RSA 私钥（用于系统重装后恢复）。

        会覆盖当前密钥环中的私钥。必须在 initialize 之前调用。
        """
        # 验证 PEM 可解析
        try:
            serialization.load_pem_private_key(private_pem.encode(), password=None)
        except Exception as e:
            raise SecretsError(f"PEM 私钥格式无效: {e}") from e

        self._save_rsa_private_to_keyring(private_pem)
        logger.info("RSA 私钥已恢复到密钥环")

    # ============================================================
    # 内部：RSA 密钥
    # ============================================================

    # RSA 私钥分段大小（ASCII 字符）。Windows Credential Manager 单条 password
    # 字段的 UTF-16 编码上限是 2560 字节，即 1280 字符。留裕度选 900，
    # 1700 字节的 PKCS8 PEM 拆 2 段即可。
    _RSA_CHUNK_SIZE: int = 900

    def _rsa_part_name(self, idx: int) -> str:
        """段 idx 对应的 keyring username。段 0 固定用 KEYRING_RSA_PRIVATE_KEY，
        其余用 `..._part_N`。"""
        if idx == 0:
            return self.paths.KEYRING_RSA_PRIVATE_KEY
        return f"{self.paths.KEYRING_RSA_PRIVATE_KEY}_part_{idx}"

    def _save_rsa_private_to_keyring(self, private_pem: str) -> None:
        """把 RSA 私钥分段存 keyring，绕过 Windows Credential 单条 2560 字节限制。"""
        chunks = [
            private_pem[i : i + self._RSA_CHUNK_SIZE]
            for i in range(0, len(private_pem), self._RSA_CHUNK_SIZE)
        ]
        if not chunks:
            raise SecretsError("拒绝保存空 RSA 私钥")
        # 先清旧的，避免段数变小时残留旧段
        self._clear_rsa_private_from_keyring()
        for i, chunk in enumerate(chunks):
            keyring.set_password(
                self.paths.KEYRING_SERVICE,
                self._rsa_part_name(i),
                chunk,
            )

    def _load_rsa_private_pem(self) -> str | None:
        """从 keyring 拼回完整 RSA 私钥 PEM。第 0 段不存在返回 None。"""
        first = keyring.get_password(
            self.paths.KEYRING_SERVICE, self._rsa_part_name(0)
        )
        if first is None:
            return None
        parts: list[str] = [first]
        i = 1
        while True:
            part = keyring.get_password(
                self.paths.KEYRING_SERVICE, self._rsa_part_name(i)
            )
            if part is None:
                break
            parts.append(part)
            i += 1
        return "".join(parts)

    def _clear_rsa_private_from_keyring(self) -> None:
        """删掉所有段（用于覆盖前清场或外部清理）。"""
        i = 0
        while True:
            name = self._rsa_part_name(i)
            if keyring.get_password(self.paths.KEYRING_SERVICE, name) is None:
                break
            try:
                keyring.delete_password(self.paths.KEYRING_SERVICE, name)
            except Exception as e:
                logger.warning(f"keyring 删除段 {name} 失败（已存在但删不掉）: {e}")
                break
            i += 1

    def _rsa_keys_exist(self) -> bool:
        pub_exists = self.paths.RSA_PUBLIC_KEY_FILE.exists()
        priv_exists = (
            keyring.get_password(self.paths.KEYRING_SERVICE, self._rsa_part_name(0))
            is not None
        )
        return pub_exists and priv_exists

    def _generate_rsa_keys(self) -> None:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._save_rsa_private_to_keyring(private_pem.decode("ascii"))
        self.paths.RSA_PUBLIC_KEY_FILE.write_bytes(public_pem)

    def _load_rsa_private(self):
        pem_str = self._load_rsa_private_pem()
        if pem_str is None:
            raise SecretsError(
                "RSA 私钥已从系统密钥环丢失。"
                "如果你重装了系统/换了用户账号，请恢复备份的私钥。"
            )
        return serialization.load_pem_private_key(pem_str.encode("ascii"), password=None)

    def _load_rsa_public(self):
        if not self.paths.RSA_PUBLIC_KEY_FILE.exists():
            raise SecretsError(f"RSA 公钥文件不存在: {self.paths.RSA_PUBLIC_KEY_FILE}")
        return serialization.load_pem_public_key(self.paths.RSA_PUBLIC_KEY_FILE.read_bytes())

    # ============================================================
    # 内部：AES 主密钥
    # ============================================================

    def _generate_aes_master_key(self) -> None:
        aes_key = AESGCM.generate_key(bit_length=256)
        public = self._load_rsa_public()
        encrypted = public.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        meta = {
            "version": 1,
            "aes_key_encrypted": base64.b64encode(encrypted).decode("ascii"),
            "verification": base64.b64encode(_stdlib_secrets.token_bytes(8)).decode("ascii"),
        }
        self.paths.SECRETS_META_FILE.write_bytes(orjson.dumps(meta, option=orjson.OPT_INDENT_2))

    def _load_aes_master_key(self) -> bytes:
        meta = orjson.loads(self.paths.SECRETS_META_FILE.read_bytes())
        encrypted = base64.b64decode(meta["aes_key_encrypted"])
        private = self._load_rsa_private()
        try:
            return private.decrypt(
                encrypted,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
        except Exception as e:
            raise SecretsError(
                f"AES 主密钥解密失败：RSA 私钥与 secrets.meta 不匹配。错误: {e}"
            ) from e

    # ============================================================
    # 内部：用户密钥记录
    # ============================================================

    def _load_records(self) -> dict[str, str]:
        if not self.paths.SECRETS_FILE.exists():
            return {}
        data = self.paths.SECRETS_FILE.read_bytes()
        if not data:
            return {}
        try:
            return orjson.loads(data)
        except orjson.JSONDecodeError as e:
            raise SecretsError(f"secrets.enc 格式损坏: {e}") from e

    def _save_records(self) -> None:
        tmp = self.paths.SECRETS_FILE.with_suffix(".enc.tmp")
        tmp.write_bytes(orjson.dumps(self._records, option=orjson.OPT_INDENT_2))
        tmp.replace(self.paths.SECRETS_FILE)  # 原子替换

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise SecretsError("SecretsManager 尚未初始化，请先调用 initialize()")
