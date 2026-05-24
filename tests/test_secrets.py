"""测试 SecretsManager —— 加密、解密、持久化、密钥环交互。"""

from __future__ import annotations

import pytest

from app_config.secrets import SecretsError, SecretsManager


def test_initialize_creates_keys(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()

    assert tmp_paths.RSA_PUBLIC_KEY_FILE.exists()
    assert tmp_paths.SECRETS_META_FILE.exists()
    # 私钥应保存在 keyring
    assert (tmp_paths.KEYRING_SERVICE, tmp_paths.KEYRING_RSA_PRIVATE_KEY) in fake_keyring.store


def test_set_get_roundtrip(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()

    sm.set("deepseek_main", "sk-test123456789")
    assert sm.get("deepseek_main") == "sk-test123456789"


def test_get_missing_returns_none(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()
    assert sm.get("nonexistent") is None


def test_get_required_missing_raises(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()
    with pytest.raises(SecretsError):
        sm.get_required("nonexistent")


def test_overwrite_secret(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()

    sm.set("api", "first")
    sm.set("api", "second")
    assert sm.get("api") == "second"


def test_delete_secret(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()

    sm.set("temp", "value")
    assert sm.has("temp")

    deleted = sm.delete("temp")
    assert deleted is True
    assert not sm.has("temp")
    assert sm.get("temp") is None

    # 再删一次返回 False
    assert sm.delete("temp") is False


def test_list_ids(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()

    sm.set("a", "1")
    sm.set("b", "2")
    sm.set("c", "3")
    assert sm.list_ids() == ["a", "b", "c"]


def test_persistence_across_instances(tmp_paths, fake_keyring):
    """模拟程序重启：第二个 SecretsManager 实例应能读取第一个写入的密钥。"""
    sm1 = SecretsManager(tmp_paths)
    sm1.initialize()
    sm1.set("key", "value")

    sm2 = SecretsManager(tmp_paths)
    sm2.initialize()
    assert sm2.get("key") == "value"


def test_tamper_detection(tmp_paths, fake_keyring):
    """篡改密文应导致解密失败。"""
    import base64
    import orjson

    sm = SecretsManager(tmp_paths)
    sm.initialize()
    sm.set("key", "original")

    # 直接篡改 secrets.enc
    records = orjson.loads(tmp_paths.SECRETS_FILE.read_bytes())
    blob = base64.b64decode(records["key"])
    # 翻转最后一个字节
    tampered = blob[:-1] + bytes([blob[-1] ^ 0xFF])
    records["key"] = base64.b64encode(tampered).decode()
    tmp_paths.SECRETS_FILE.write_bytes(orjson.dumps(records))

    # 新实例读取应失败（GCM 完整性校验）
    sm2 = SecretsManager(tmp_paths)
    sm2.initialize()
    assert sm2.get("key") is None


def test_associated_data_protection(tmp_paths, fake_keyring):
    """改了 key_id 但保留 ciphertext 应解密失败（防止换名攻击）。"""
    sm = SecretsManager(tmp_paths)
    sm.initialize()
    sm.set("real_id", "secret_value")

    import orjson
    records = orjson.loads(tmp_paths.SECRETS_FILE.read_bytes())
    # 把同样的密文挂到别的 ID 下
    records["fake_id"] = records["real_id"]
    tmp_paths.SECRETS_FILE.write_bytes(orjson.dumps(records))

    sm2 = SecretsManager(tmp_paths)
    sm2.initialize()
    assert sm2.get("real_id") == "secret_value"  # 原 ID 仍可解密
    assert sm2.get("fake_id") is None              # 换名应失败


def test_uninitialized_raises(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    with pytest.raises(SecretsError):
        sm.set("a", "b")
    with pytest.raises(SecretsError):
        sm.get("a")
    with pytest.raises(SecretsError):
        sm.list_ids()


def test_empty_inputs_rejected(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()
    with pytest.raises(SecretsError):
        sm.set("", "value")
    with pytest.raises(SecretsError):
        sm.set("id", "")


def test_keyring_loss_blocks_recovery(tmp_paths, fake_keyring):
    """模拟 keyring 数据丢失（重装系统场景）。"""
    sm1 = SecretsManager(tmp_paths)
    sm1.initialize()
    sm1.set("k", "v")

    # 模拟 keyring 被清空
    fake_keyring.store.clear()

    sm2 = SecretsManager(tmp_paths)
    # initialize 会尝试解密 AES 主密钥，没有 RSA 私钥时应抛错或重新生成
    # 实际上 _rsa_keys_exist 会返回 False（公钥还在但私钥没了），重新生成 RSA
    # 但新私钥无法解 secrets.meta 中的旧 AES 密钥
    with pytest.raises(SecretsError):
        sm2.initialize()


def test_export_import_rsa_private_key(tmp_paths, fake_keyring):
    """导出私钥后丢失 keyring 数据，导入应能恢复访问。"""
    sm1 = SecretsManager(tmp_paths)
    sm1.initialize()
    sm1.set("k", "secret")

    backup = sm1.export_rsa_private_key()
    assert "BEGIN PRIVATE KEY" in backup or "BEGIN RSA PRIVATE KEY" in backup

    # 模拟系统重装：清空 keyring
    fake_keyring.store.clear()

    # 恢复私钥
    sm2 = SecretsManager(tmp_paths)
    sm2.import_rsa_private_key(backup)
    sm2.initialize()
    assert sm2.get("k") == "secret"
