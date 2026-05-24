"""测试旧配置（V1）迁移到 V2 的完整流程。"""

from __future__ import annotations

import yaml

from app_config.loader import load_config
from app_config.migrate import (
    LEGACY_SECRET_MAP,
    build_config_from_legacy,
    detect_legacy,
    is_placeholder,
    migrate_emoji,
    migrate_personas,
    migrate_secrets,
    run_full_migration,
)
from app_config.secrets import SecretsManager


def _write_legacy_env(paths, **kv):
    lines = [f"{k}={v}" for k, v in kv.items()]
    paths.LEGACY_ENV_FILE.write_text("\n".join(lines), encoding="utf-8")


def _write_legacy_config(paths, data: dict):
    paths.LEGACY_CONFIG_FILE.write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


def test_is_placeholder():
    assert is_placeholder("")
    assert is_placeholder(None)
    assert is_placeholder("你的API_Key")
    assert is_placeholder("your_token_here")
    assert is_placeholder("<INSERT_KEY>")
    assert is_placeholder("xxx_xxx")
    assert is_placeholder("...")
    assert not is_placeholder("sk-real-key-123")
    assert not is_placeholder("napcat_token_abc")


def test_detect_legacy_none(tmp_paths):
    result = detect_legacy(tmp_paths)
    assert all(v is False for v in result.values())


def test_detect_legacy_with_env(tmp_paths):
    _write_legacy_env(tmp_paths, DEEPSEEK_API_KEY="sk-test")
    result = detect_legacy(tmp_paths)
    assert result["env"] is True


def test_migrate_secrets_skips_placeholders(tmp_paths, fake_keyring):
    _write_legacy_env(
        tmp_paths,
        DEEPSEEK_API_KEY="sk-real-key",
        VOLCENGINE_API_KEY="你的火山引擎API_Key",  # 占位
        QWEATHER_KEY="qw-real-key",
        ONEBOT_ACCESS_TOKEN="",  # 空
    )

    sm = SecretsManager(tmp_paths)
    sm.initialize()
    migrated = migrate_secrets(tmp_paths, sm)

    assert "deepseek_main" in migrated
    assert "qweather" in migrated
    assert "volcengine_main" not in migrated  # 占位被跳过
    assert "napcat_default_token" not in migrated  # 空被跳过

    assert sm.get("deepseek_main") == "sk-real-key"
    assert sm.get("qweather") == "qw-real-key"


def test_migrate_secrets_skips_already_present(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()
    sm.set("deepseek_main", "preexisting")

    _write_legacy_env(tmp_paths, DEEPSEEK_API_KEY="from-env")
    migrated = migrate_secrets(tmp_paths, sm)

    assert "deepseek_main" not in migrated
    assert sm.get("deepseek_main") == "preexisting"  # 不覆盖


def test_build_config_from_legacy(tmp_paths, fake_keyring):
    _write_legacy_env(
        tmp_paths,
        DEEPSEEK_API_KEY="sk-test",
        QWEATHER_KEY="qw-test",
        QWEATHER_HOST="api.qweather.com",
        ONEBOT_ACCESS_TOKEN="napcat-token",
        HOST="127.0.0.1",
        PORT="8080",
    )
    _write_legacy_config(
        tmp_paths,
        {
            "persona": "yuexi",
            "model": {
                "pro": "deepseek-chat",
                "flash": "deepseek-chat",
                "temperature": 0.7,
                "max_tokens": 32768,
            },
            "agent": {
                "max_loops": 20,
                "merge_window": 1.0,
                "greeting_interval": 300,
            },
            "summarize": {
                "trigger_at": 15000,
                "range_start": 5000,
                "range_end": 7000,
            },
        },
    )

    sm = SecretsManager(tmp_paths)
    sm.initialize()
    migrate_secrets(tmp_paths, sm)

    cfg = build_config_from_legacy(tmp_paths, sm)

    assert cfg.persona.active == "yuexi"
    assert cfg.agents.chat.model == "deepseek-chat"
    assert cfg.agents.chat.temperature == 0.7
    assert cfg.agents.chat.max_tokens == 32768
    assert cfg.agents.chat.max_loops == 20
    assert cfg.behavior.merge_window == 1.0
    assert cfg.behavior.greeting_interval == 300
    assert cfg.behavior.summarize.trigger_at_messages == 15000

    # NapCat 适配器从 .env 取 host/port，按 V1 NoneBot 行为设 mode=server
    napcat = cfg.adapters["default"]
    assert napcat.mode == "server"
    assert napcat.host == "127.0.0.1"
    assert napcat.port == 8080
    assert napcat.path == "/onebot/v11/ws"
    assert napcat.access_token_id == "napcat_default_token"

    # Provider 配置正确
    assert "deepseek_main" in cfg.providers
    assert cfg.providers["deepseek_main"].api_key_id == "deepseek_main"

    # 天气功能因为有密钥+host 自动启用
    assert cfg.features.weather.enabled is True
    assert cfg.features.weather.host == "api.qweather.com"


def test_migrate_personas(tmp_paths):
    """V2 设计下 PERSONAS_DIR 直接是项目根的 personas/，
    migrate_personas 退化为"扫描已就位的人格"。"""
    (tmp_paths.PERSONAS_DIR / "yuexi").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "yuexi" / "persona_prompt.py").write_text(
        "PERSONA_PROMPT = 'test'", encoding="utf-8"
    )
    # 下划线开头的应被忽略
    (tmp_paths.PERSONAS_DIR / "__pycache__").mkdir()

    moved = migrate_personas(tmp_paths)

    assert "yuexi" in moved
    assert "__pycache__" not in moved


def test_migrate_personas_ignores_dirs_without_prompt(tmp_paths):
    """没有 persona_prompt.py 的目录不算"已就位"。"""
    (tmp_paths.PERSONAS_DIR / "no_prompt").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "valid").mkdir(parents=True)
    (tmp_paths.PERSONAS_DIR / "valid" / "persona_prompt.py").write_text(
        "PERSONA_PROMPT = 'x'", encoding="utf-8"
    )

    moved = migrate_personas(tmp_paths)
    assert "valid" in moved
    assert "no_prompt" not in moved


def test_migrate_emoji(tmp_paths):
    tmp_paths.LEGACY_EMOJI_DIR.mkdir()
    (tmp_paths.LEGACY_EMOJI_DIR / "happy.jpg").write_bytes(b"fake-jpg")
    (tmp_paths.LEGACY_EMOJI_DIR / "sad.png").write_bytes(b"fake-png")
    # 子目录不应被迁移（按设计 emoji 是平铺的）
    (tmp_paths.LEGACY_EMOJI_DIR / "subdir").mkdir()

    count = migrate_emoji(tmp_paths)
    assert count == 2
    assert (tmp_paths.EMOJI_DIR / "happy.jpg").exists()
    assert (tmp_paths.EMOJI_DIR / "sad.png").exists()


def test_run_full_migration(tmp_paths, fake_keyring):
    _write_legacy_env(
        tmp_paths,
        DEEPSEEK_API_KEY="sk-real",
        QWEATHER_KEY="qw-real",
    )
    _write_legacy_config(
        tmp_paths,
        {"persona": "user_legacy", "model": {"pro": "deepseek-chat"}},
    )
    (tmp_paths.LEGACY_PERSONAS_DIR / "myp").mkdir(parents=True)
    (tmp_paths.LEGACY_PERSONAS_DIR / "myp" / "persona_prompt.py").write_text("X = 1", encoding="utf-8")
    tmp_paths.LEGACY_EMOJI_DIR.mkdir()
    (tmp_paths.LEGACY_EMOJI_DIR / "a.jpg").write_bytes(b"x")

    sm = SecretsManager(tmp_paths)
    report = run_full_migration(tmp_paths, sm)

    assert report["config_created"] is True
    assert "deepseek_main" in report["migrated_secrets"]
    assert "qweather" in report["migrated_secrets"]
    assert "myp" in report["migrated_personas"]
    assert report["migrated_emoji_count"] == 1

    # 新配置正常加载，且 persona.active 透传旧值（migrate 不强改）
    cfg = load_config(tmp_paths)
    assert cfg.persona.active == "user_legacy"


def test_run_full_migration_idempotent(tmp_paths, fake_keyring):
    """重复运行不应破坏已有数据。"""
    _write_legacy_env(tmp_paths, DEEPSEEK_API_KEY="sk-real")

    sm = SecretsManager(tmp_paths)
    report1 = run_full_migration(tmp_paths, sm)
    assert report1["config_created"] is True

    # 第二次运行：config 已存在，不再创建
    sm2 = SecretsManager(tmp_paths)
    report2 = run_full_migration(tmp_paths, sm2)
    assert report2["config_created"] is False
    # 密钥也不会重复迁移
    assert "deepseek_main" not in report2["migrated_secrets"]


def test_legacy_secret_map_coverage():
    """确保 LEGACY_SECRET_MAP 覆盖了原 .env.example 中的所有密钥变量。"""
    expected_legacy_keys = {
        "DEEPSEEK_API_KEY",
        "VOLCENGINE_API_KEY",
        "QWEATHER_KEY",
        "ONEBOT_ACCESS_TOKEN",
    }
    assert expected_legacy_keys == set(LEGACY_SECRET_MAP.keys())
