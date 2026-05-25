"""跨平台路径管理 —— 所有运行时路径的唯一来源。

新结构（V2）：
    项目根/
        data/                       <- 用户运行时数据（gitignore）
            config.yaml             <- 主配置文件
            secrets.enc             <- AES 加密后的 API 密钥库
            secrets.meta            <- RSA 加密的 AES 主密钥
            rsa_public.pem          <- RSA 公钥（可公开）
            memory/{name}/          <- 各人格的对话历史
            logs/                   <- 结构化日志
            emoji/                  <- 表情包资源
        personas/{name}/            <- 所有人格平级（仓库自带 + 用户自创共存）
                                       仓库自带的（如 diana/）入 git
                                       用户自创的由 .gitignore 排除
        providers/presets/          <- 提供商预设（git 追踪）

RSA 私钥不落盘，仅保存在系统密钥环（keyring）。
"""

from __future__ import annotations

from pathlib import Path


class AppPaths:
    """所有路径的中心化管理。

    通过依赖注入传递给需要路径的模块，便于测试时替换为临时目录。
    """

    KEYRING_SERVICE: str = "Diana_Agent"
    KEYRING_RSA_PRIVATE_KEY: str = "rsa_private_key"

    def __init__(self, project_root: Path | None = None) -> None:
        if project_root is None:
            # app_config/paths.py → app_config/ → 项目根
            project_root = Path(__file__).resolve().parent.parent
        self.PROJECT_ROOT: Path = project_root

        # === 运行时数据目录（gitignore） ===
        self.DATA_DIR: Path = project_root / "data"
        self.CONFIG_FILE: Path = self.DATA_DIR / "config.yaml"
        self.SECRETS_FILE: Path = self.DATA_DIR / "secrets.enc"
        self.SECRETS_META_FILE: Path = self.DATA_DIR / "secrets.meta"
        self.RSA_PUBLIC_KEY_FILE: Path = self.DATA_DIR / "rsa_public.pem"

        self.MEMORY_DIR: Path = self.DATA_DIR / "memory"
        self.LOGS_DIR: Path = self.DATA_DIR / "logs"
        self.EMOJI_DIR: Path = self.DATA_DIR / "emoji"
        self.UPLOADS_DIR: Path = self.DATA_DIR / "uploads"

        # === 仓库内 personas/ —— 所有人格平级（仓库自带 + 用户自创共存）===
        # 仓库自带（如 diana/）入 git，其它由 .gitignore 排除
        self.PERSONAS_DIR: Path = project_root / "personas"

        # === 仓库内置资源（git 追踪） ===
        self.PROVIDER_PRESETS_DIR: Path = project_root / "providers" / "presets"

    def ensure_data_dirs(self) -> None:
        """确保所有 data 子目录存在。幂等。"""
        for d in (
            self.DATA_DIR,
            self.MEMORY_DIR,
            self.LOGS_DIR,
            self.EMOJI_DIR,
            self.UPLOADS_DIR,
        ):
            d.mkdir(parents=True, exist_ok=True)
        # PERSONAS_DIR 在仓库根目录，必然存在（init 时已 ensure）
        self.PERSONAS_DIR.mkdir(parents=True, exist_ok=True)

    def memory_dir_for(self, persona: str) -> Path:
        """获取指定人格的记忆目录。"""
        return self.MEMORY_DIR / persona

    def persona_dir_for(self, persona: str) -> Path:
        """获取指定人格的资源目录。所有人格平级在 PERSONAS_DIR 下。"""
        return self.PERSONAS_DIR / persona
