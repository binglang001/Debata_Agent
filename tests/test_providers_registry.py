"""测试 provider 注册中心与构造工厂。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app_config.schema import ProviderConfig
from app_config.secrets import SecretsManager
from providers import (
    AnthropicProvider,
    OpenAICompatProvider,
    ProviderError,
    ProviderRegistry,
    build_provider,
    known_protocols,
    register_protocol,
)
from providers.presets_loader import load_all_presets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = PROJECT_ROOT / "providers" / "presets"


@pytest.fixture
def builtin_presets():
    return load_all_presets(PRESETS_DIR)


@pytest.fixture
def secrets(tmp_paths, fake_keyring):
    sm = SecretsManager(tmp_paths)
    sm.initialize()
    sm.set("ds_key", "sk-test123")
    sm.set("anthro_key", "sk-ant-test")
    return sm


# ============================================================
# build_provider 路径
# ============================================================


def test_build_from_preset_openai_compat(builtin_presets, secrets):
    cfg = ProviderConfig(preset="deepseek", api_key_id="ds_key")
    provider = build_provider("ds_main", cfg, secrets, builtin_presets)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "ds_main"
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.api_key == "sk-test123"


def test_build_from_preset_anthropic(builtin_presets, secrets):
    cfg = ProviderConfig(preset="anthropic", api_key_id="anthro_key")
    provider = build_provider("claude", cfg, secrets, builtin_presets)
    assert isinstance(provider, AnthropicProvider)
    assert provider.api_key == "sk-ant-test"


def test_build_overrides_base_url(builtin_presets, secrets):
    cfg = ProviderConfig(
        preset="deepseek",
        base_url="https://proxy.example.com/v1",
        api_key_id="ds_key",
    )
    provider = build_provider("dsmain", cfg, secrets, builtin_presets)
    assert provider.base_url == "https://proxy.example.com/v1"


def test_build_custom_provider(secrets):
    cfg = ProviderConfig(
        protocol="openai_compat",
        base_url="https://custom.example.com/v1",
        api_key_id="ds_key",
    )
    provider = build_provider("custom", cfg, secrets, {})
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.base_url == "https://custom.example.com/v1"


def test_build_unknown_preset_raises(secrets):
    cfg = ProviderConfig(preset="totally_unknown", api_key_id="ds_key")
    with pytest.raises(ProviderError, match="未知预设"):
        build_provider("x", cfg, secrets, {})


def test_build_missing_api_key_warns(builtin_presets, secrets, caplog):
    """密钥不存在时应警告但仍构造（用空 key）。"""
    import logging
    caplog.set_level(logging.WARNING)
    cfg = ProviderConfig(preset="deepseek", api_key_id="missing")
    provider = build_provider("ds", cfg, secrets, builtin_presets)
    assert provider.api_key == ""
    assert "找不到" in caplog.text


def test_build_passes_reasoning_style(builtin_presets, secrets):
    """openai_compat 协议时，reasoning_style 应从 preset 传给 provider。"""
    cfg = ProviderConfig(preset="qwen", api_key_id="ds_key")
    provider = build_provider("qwen", cfg, secrets, builtin_presets)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.reasoning_style == "qwen_enable_thinking"


# ============================================================
# ProviderRegistry
# ============================================================


def test_registry_register_and_get(secrets, builtin_presets):
    reg = ProviderRegistry()
    cfg = ProviderConfig(preset="deepseek", api_key_id="ds_key")
    p = build_provider("ds", cfg, secrets, builtin_presets)
    reg.register(p)

    assert reg.has("ds")
    assert reg.get("ds") is p
    assert "ds" in reg.list_names()


def test_registry_duplicate_raises(secrets, builtin_presets):
    reg = ProviderRegistry()
    cfg = ProviderConfig(preset="deepseek", api_key_id="ds_key")
    reg.register(build_provider("ds", cfg, secrets, builtin_presets))
    with pytest.raises(ValueError, match="重复"):
        reg.register(build_provider("ds", cfg, secrets, builtin_presets))


def test_registry_get_missing_raises():
    reg = ProviderRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")


def test_registry_load_presets():
    reg = ProviderRegistry()
    reg.load_presets(PRESETS_DIR)
    assert "deepseek" in reg.presets
    assert len(reg.presets) >= 7


def test_registry_build_from_config(secrets):
    reg = ProviderRegistry()
    reg.load_presets(PRESETS_DIR)

    cfgs = {
        "ds_main": ProviderConfig(preset="deepseek", api_key_id="ds_key"),
        "claude": ProviderConfig(preset="anthropic", api_key_id="anthro_key"),
    }
    reg.build_from_config(cfgs, secrets)
    assert reg.has("ds_main")
    assert reg.has("claude")


@pytest.mark.asyncio
async def test_registry_close_all(secrets, builtin_presets):
    reg = ProviderRegistry()
    cfg = ProviderConfig(preset="deepseek", api_key_id="ds_key")
    reg.register(build_provider("ds", cfg, secrets, builtin_presets))
    # 不应抛异常
    await reg.close_all()


# ============================================================
# 协议注册
# ============================================================


def test_known_protocols():
    protos = known_protocols()
    assert "openai_compat" in protos
    assert "anthropic" in protos


def test_register_protocol_duplicate_raises():
    with pytest.raises(ValueError, match="已注册"):
        register_protocol("openai_compat", lambda **k: None)
