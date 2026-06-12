"""测试配置 schema、加载、保存往返。"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from app_config.loader import ConfigError, load_config, save_config
from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    ASRFeatureConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
)


def _minimal_config() -> RootConfig:
    return RootConfig(
        agents=AgentsConfig(
            chat=AgentConfig(
                provider="ds",
                model="deepseek-chat",
            )
        ),
        providers={
            "ds": ProviderConfig(preset="deepseek", api_key_id="ds_main"),
        },
        adapters={
            "default": NapCatAdapterConfig(),
        },
    )


def test_minimal_valid_config():
    cfg = _minimal_config()
    assert cfg.agents.chat.provider == "ds"
    assert cfg.version == 2


def test_agent_provider_must_exist():
    """chat agent 的 provider 必须在 providers 中定义。"""
    with pytest.raises(ValueError, match="未在 providers 中定义"):
        RootConfig(
            agents=AgentsConfig(
                chat=AgentConfig(
                    provider="nonexistent",
                    model="x",
                )
            ),
            providers={},
        )


def test_extra_fields_rejected():
    """unknown 字段应被拒绝（防止拼写错误）。"""
    with pytest.raises(ValidationError):
        RootConfig.model_validate(
            {
                "agents": {
                    "chat": {"provider": "ds", "model": "x"},
                },
                "providers": {"ds": {"preset": "deepseek"}},
                "unknown_field": "boom",
            }
        )


def test_custom_provider_requires_protocol_and_url():
    """自定义提供商（无 preset）必须填 protocol 和 base_url。"""
    with pytest.raises(ValueError, match="protocol 和 base_url"):
        ProviderConfig()  # 全空

    with pytest.raises(ValueError, match="protocol 和 base_url"):
        ProviderConfig(protocol="openai_compat")  # 缺 base_url

    # 完整自定义 OK
    cfg = ProviderConfig(
        protocol="openai_compat",
        base_url="https://api.x.com",
        api_key_id="x_key",
    )
    assert cfg.base_url == "https://api.x.com"


def test_preset_only_provider():
    """仅指定 preset 也合法。"""
    cfg = ProviderConfig(preset="deepseek", api_key_id="ds")
    assert cfg.preset == "deepseek"


def test_old_version_rejected():
    with pytest.raises(ValueError, match="配置版本过旧"):
        RootConfig(
            version=1,
            agents=AgentsConfig(
                chat=AgentConfig(provider="ds", model="x"),
            ),
            providers={"ds": ProviderConfig(preset="deepseek")},
        )


def test_temperature_range():
    with pytest.raises(ValidationError):
        AgentConfig(provider="x", model="y", temperature=3.0)


def test_save_load_roundtrip(tmp_paths, fake_keyring):
    cfg = _minimal_config()
    cfg.app.theme = "dark"
    save_config(tmp_paths, cfg, backup=False)

    assert tmp_paths.CONFIG_FILE.exists()

    loaded = load_config(tmp_paths)
    assert loaded.agents.chat.provider == "ds"
    assert loaded.providers["ds"].preset == "deepseek"
    assert loaded.app.theme == "dark"


def test_theme_default_is_auto():
    cfg = _minimal_config()

    assert cfg.app.theme == "auto"


def test_load_missing_config_raises(tmp_paths, fake_keyring):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_paths)


def test_load_invalid_yaml_raises(tmp_paths, fake_keyring):
    tmp_paths.CONFIG_FILE.write_text("not: valid: yaml: [", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML 解析失败"):
        load_config(tmp_paths)


def test_load_invalid_schema_raises(tmp_paths, fake_keyring):
    tmp_paths.CONFIG_FILE.write_text(
        yaml.safe_dump(
            {
                "agents": {
                    "chat": {"provider": "missing", "model": "x"},
                },
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="配置校验未通过"):
        load_config(tmp_paths)


def test_save_creates_backup(tmp_paths, fake_keyring):
    cfg = _minimal_config()
    save_config(tmp_paths, cfg, backup=False)
    assert tmp_paths.CONFIG_FILE.exists()

    # 第二次保存应生成 .bak
    save_config(tmp_paths, cfg, backup=True)
    backup_path = tmp_paths.CONFIG_FILE.with_suffix(".yaml.bak")
    assert backup_path.exists()


def test_optional_agents_default_none(tmp_paths, fake_keyring):
    cfg = _minimal_config()
    assert cfg.agents.proactive is None
    assert cfg.agents.summary is None


def test_features_default_disabled():
    cfg = _minimal_config()
    assert cfg.features.vision.enabled is False
    assert cfg.features.asr.enabled is False
    assert cfg.features.tts.enabled is False


def test_default_asr_legacy_config_is_kept_for_migration():
    cfg = ASRFeatureConfig()
    assert cfg.local_model == "large-v3"
