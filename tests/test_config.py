"""测试配置 schema、加载、保存往返。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app_config.loader import ConfigError, load_config, save_config, set_active_config
from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    ASRFeatureConfig,
    ContextLengthBudgetRule,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
    ToolResultBudgetConfig,
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
    budgets = cfg.behavior.context.tool_result_budgets
    assert budgets["read_file"].inline_budget_tokens == 2500
    assert budgets["get_recent_chat_messages"].artifact_threshold_tokens == 3000


def test_budget_limit_schema_defaults_load_for_legacy_config():
    cfg = RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "ds", "model": "deepseek-chat"}},
            "providers": {"ds": {"preset": "deepseek", "api_key_id": "ds_main"}},
        }
    )

    assert cfg.agents.chat.tool_loop_final_max_tokens == 4096
    assert cfg.behavior.summarize.trigger_at_tokens is None
    assert cfg.behavior.summarize.target_after_tokens is None
    assert cfg.behavior.summarize.trigger_at_context_percent == 75
    assert cfg.behavior.summarize.target_after_context_percent == 50
    assert cfg.behavior.summarize.retry_target_after_context_percent == 30
    assert "range_start_messages" not in type(cfg.behavior.summarize).model_fields
    assert "range_end_messages" not in type(cfg.behavior.summarize).model_fields
    assert cfg.behavior.context.prompt_overhead_estimate_tokens == 12000
    assert "min_working_history_tokens" not in type(cfg.behavior.context).model_fields
    assert "current_conversation_min_records" not in type(cfg.behavior.context).model_fields
    assert "runtime_record_keep_count" not in type(cfg.behavior.context).model_fields
    assert "send_receipt_keep_count" not in type(cfg.behavior.context).model_fields
    assert "no_action_keep_count" not in type(cfg.behavior.context).model_fields
    rec = cfg.behavior.context.recommended_context_budget
    assert rec.model_name_budget_tokens["deepseek-v4-pro"] == 350_000
    assert rec.model_name_budget_tokens["deepseek-v4"] == 300_000
    assert rec.model_name_budget_tokens["claude"] == 150_000
    assert rec.context_length_rules[0].min_context_length_tokens == 1_000_000
    assert rec.context_length_rules[0].budget_tokens == 300_000
    assert rec.context_length_rules[1].min_context_length_tokens == 200_000
    assert rec.context_length_rules[1].budget_tokens == 150_000
    assert rec.context_length_rules[2].min_context_length_tokens == 128_000
    assert rec.context_length_rules[2].budget_tokens == 96_000
    assert rec.context_length_scale_percent == 75
    assert rec.min_scaled_budget_tokens == 4096
    assert rec.fallback_budget_tokens == 96_000
    assert cfg.behavior.proactive_router_text_limit_tokens == 256
    assert cfg.behavior.proactive_router_tool_result_inline_tokens == 96
    assert cfg.behavior.proactive_router_tool_result_hard_cap_tokens == 160
    assert cfg.behavior.proactive_router_summary_limit_tokens == 1024
    assert cfg.behavior.proactive_router_history_token_budget == 16384
    assert "persona_refine_history_turns" not in type(cfg.behavior).model_fields
    assert cfg.features.vision.max_tokens == 1024


def test_data_config_explicit_budget_defaults_roundtrip(tmp_paths):
    config_path = Path(__file__).resolve().parent.parent / "data" / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    expected = {
        ("agents", "chat", "tool_loop_final_max_tokens"): 4096,
        ("features", "vision", "max_tokens"): 1024,
        ("behavior", "summarize", "trigger_at_context_percent"): 75,
        ("behavior", "summarize", "target_after_context_percent"): 50,
        ("behavior", "summarize", "retry_target_after_context_percent"): 30,
        ("behavior", "context", "prompt_overhead_estimate_tokens"): 12000,
        (
            "behavior",
            "context",
            "recommended_context_budget",
            "model_name_budget_tokens",
            "deepseek-v4-pro",
        ): 350_000,
        (
            "behavior",
            "context",
            "recommended_context_budget",
            "model_name_budget_tokens",
            "deepseek-v4",
        ): 300_000,
        (
            "behavior",
            "context",
            "recommended_context_budget",
            "model_name_budget_tokens",
            "claude",
        ): 150_000,
        (
            "behavior",
            "context",
            "recommended_context_budget",
            "context_length_scale_percent",
        ): 75,
        (
            "behavior",
            "context",
            "recommended_context_budget",
            "min_scaled_budget_tokens",
        ): 4096,
        (
            "behavior",
            "context",
            "recommended_context_budget",
            "fallback_budget_tokens",
        ): 96_000,
        ("behavior", "proactive_router_text_limit_tokens"): 256,
        ("behavior", "proactive_router_tool_result_inline_tokens"): 96,
        ("behavior", "proactive_router_tool_result_hard_cap_tokens"): 160,
        ("behavior", "proactive_router_summary_limit_tokens"): 1024,
        ("behavior", "proactive_router_history_token_budget"): 16384,
    }

    for keys, value in expected.items():
        node = raw
        for key in keys:
            node = node[key]
        assert node == value

    RootConfig.model_validate(raw)
    tmp_paths.CONFIG_FILE.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    loaded = load_config(tmp_paths, set_global=False)
    assert loaded.agents.chat.tool_loop_final_max_tokens == 4096
    assert loaded.features.vision.max_tokens == 1024
    loaded_rules = loaded.behavior.context.recommended_context_budget.context_length_rules
    assert [(r.min_context_length_tokens, r.budget_tokens) for r in loaded_rules] == [
        (1_000_000, 300_000),
        (200_000, 150_000),
        (128_000, 96_000),
    ]

    save_config(tmp_paths, loaded, backup=False)
    saved = yaml.safe_load(tmp_paths.CONFIG_FILE.read_text(encoding="utf-8"))
    for keys, value in expected.items():
        node = saved
        for key in keys:
            node = node[key]
        assert node == value
    saved_rules = saved["behavior"]["context"]["recommended_context_budget"][
        "context_length_rules"
    ]
    assert saved_rules == [
        {"min_context_length_tokens": 1_000_000, "budget_tokens": 300_000},
        {"min_context_length_tokens": 200_000, "budget_tokens": 150_000},
        {"min_context_length_tokens": 128_000, "budget_tokens": 96_000},
    ]
    assert "persona_refine_history_turns" not in saved["behavior"]
    assert "range_start_messages" not in saved["behavior"]["summarize"]
    assert "range_end_messages" not in saved["behavior"]["summarize"]
    assert "min_working_history_tokens" not in saved["behavior"]["context"]
    assert "current_conversation_min_records" not in saved["behavior"]["context"]
    assert "runtime_record_keep_count" not in saved["behavior"]["context"]
    assert "send_receipt_keep_count" not in saved["behavior"]["context"]
    assert "no_action_keep_count" not in saved["behavior"]["context"]


def test_deprecated_context_window_fields_are_ignored_and_not_saved(tmp_paths):
    raw = _minimal_config().model_dump(mode="json", exclude_none=True)
    raw["behavior"]["summarize"].update(
        {
            "range_start_messages": 50,
            "range_end_messages": 150,
        }
    )
    raw["behavior"]["context"].update(
        {
            "min_working_history_tokens": 4096,
            "current_conversation_min_records": 8,
            "runtime_record_keep_count": 12,
            "send_receipt_keep_count": 4,
            "no_action_keep_count": 8,
        }
    )

    cfg = RootConfig.model_validate(raw)
    assert "range_start_messages" not in cfg.behavior.summarize.model_dump()
    assert "range_end_messages" not in cfg.behavior.summarize.model_dump()
    assert "min_working_history_tokens" not in cfg.behavior.context.model_dump()
    assert "current_conversation_min_records" not in cfg.behavior.context.model_dump()
    assert "runtime_record_keep_count" not in cfg.behavior.context.model_dump()
    assert "send_receipt_keep_count" not in cfg.behavior.context.model_dump()
    assert "no_action_keep_count" not in cfg.behavior.context.model_dump()

    tmp_paths.CONFIG_FILE.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    loaded = load_config(tmp_paths, set_global=False)
    save_config(tmp_paths, loaded, backup=False)

    saved = yaml.safe_load(tmp_paths.CONFIG_FILE.read_text(encoding="utf-8"))
    assert "range_start_messages" not in saved["behavior"]["summarize"]
    assert "range_end_messages" not in saved["behavior"]["summarize"]
    assert "min_working_history_tokens" not in saved["behavior"]["context"]
    assert "current_conversation_min_records" not in saved["behavior"]["context"]
    assert "runtime_record_keep_count" not in saved["behavior"]["context"]
    assert "send_receipt_keep_count" not in saved["behavior"]["context"]
    assert "no_action_keep_count" not in saved["behavior"]["context"]


def test_deprecated_typing_delay_fields_are_ignored_and_not_saved(tmp_paths):
    raw = _minimal_config().model_dump(mode="json", exclude_none=True)
    raw["behavior"]["typing"] = {
        "chars_per_second": 2.0,
        "english_chars_per_second": 6.0,
        "min_delay_seconds": 0.1,
        "max_delay_seconds": 9.0,
        "clamp_model_delay": False,
    }

    cfg = RootConfig.model_validate(raw)
    typing_dump = cfg.behavior.typing.model_dump()
    assert typing_dump == {
        "chars_per_second": 2.0,
        "english_chars_per_second": 6.0,
    }
    assert "min_delay_seconds" not in type(cfg.behavior.typing).model_fields
    assert "max_delay_seconds" not in type(cfg.behavior.typing).model_fields
    assert "clamp_model_delay" not in type(cfg.behavior.typing).model_fields

    tmp_paths.CONFIG_FILE.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    loaded = load_config(tmp_paths, set_global=False)
    save_config(tmp_paths, loaded, backup=False)

    saved = yaml.safe_load(tmp_paths.CONFIG_FILE.read_text(encoding="utf-8"))
    saved_typing = saved["behavior"]["typing"]
    assert saved_typing == {
        "chars_per_second": 2.0,
        "english_chars_per_second": 6.0,
    }


def test_recommended_context_budget_uses_loaded_config():
    from core.pipeline_context import _recommended_context_budget

    cfg = _minimal_config()
    rec = cfg.behavior.context.recommended_context_budget
    rec.model_name_budget_tokens = {"unit-model": 123_456}
    rec.context_length_rules = [
        ContextLengthBudgetRule(
            min_context_length_tokens=10_000,
            budget_tokens=7_000,
        )
    ]
    rec.context_length_scale_percent = 50
    rec.min_scaled_budget_tokens = 2_048
    rec.fallback_budget_tokens = 6_000
    try:
        set_active_config(cfg)

        assert _recommended_context_budget("unit-model-pro") == 123_456
        assert _recommended_context_budget("other", 12_000) == 7_000
        assert _recommended_context_budget("other", 8_000) == 4_000
        assert _recommended_context_budget("other", 100) == 2_048
        assert _recommended_context_budget("other") == 6_000
    finally:
        set_active_config(_minimal_config())


def test_napcat_adapter_path_normalizes_legacy_values():
    assert NapCatAdapterConfig(path=None).path == "/"  # type: ignore[arg-type]
    assert NapCatAdapterConfig(path="").path == "/"
    assert NapCatAdapterConfig(path="onebot").path == "/onebot"
    assert NapCatAdapterConfig(path="/onebot").path == "/onebot"


def test_tool_result_budget_config_accepts_custom_values():
    cfg = _minimal_config()
    cfg.behavior.context.tool_result_budgets["read_file"] = ToolResultBudgetConfig(
        inline_budget_tokens=1200,
        artifact_threshold_tokens=1800,
        hard_cap_tokens=2400,
    )

    budget = cfg.behavior.context.tool_result_budgets["read_file"]
    assert budget.inline_budget_tokens == 1200
    assert budget.artifact_threshold_tokens == 1800
    assert budget.hard_cap_tokens == 2400


def test_legacy_tool_result_overrides_still_load():
    cfg = RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "ds", "model": "deepseek-chat"}},
            "providers": {"ds": {"preset": "deepseek", "api_key_id": "ds_main"}},
            "behavior": {
                "context": {
                    "tool_result_soft_limit_tokens": 700,
                    "tool_result_hard_cap_tokens": 1600,
                    "tool_result_soft_overrides": {"read_file": 900},
                }
            },
        }
    )

    assert cfg.behavior.context.tool_result_soft_limit_tokens == 700
    assert cfg.behavior.context.tool_result_hard_cap_tokens == 1600
    assert cfg.behavior.context.tool_result_soft_overrides["read_file"] == 900


def test_proactive_context_budget_defaults_to_4k():
    cfg = _minimal_config()
    assert cfg.behavior.proactive_context_token_budget == 4096


def test_persona_management_defaults_disabled():
    cfg = _minimal_config()
    pm = cfg.persona_management

    assert pm.enabled is False
    assert pm.persona_agent.provider == ""
    assert pm.persona_agent.model == ""
    assert pm.persona_agent.timer_interval_minutes == 30
    assert pm.persona_agent.min_interval_seconds == 300
    assert pm.social_agent.enabled is True
    assert pm.social_agent.provider == ""
    assert pm.social_agent.model == ""
    assert pm.social_agent.interval_minutes == 30
    assert pm.subconscious.enabled is True
    assert pm.subconscious.provider == ""
    assert pm.subconscious.model == ""
    assert pm.subconscious.interval_minutes == 30

    assert pm.physiology.energy.mode == "disabled"
    assert pm.physiology.energy.decay_per_hour == 1.5
    assert pm.physiology.energy.recovery_per_hour_sleep == 8.333
    assert pm.physiology.energy.recovery_per_hour_eat == 15.0
    assert pm.physiology.energy.long_sleep_threshold_minutes == 120
    assert pm.physiology.energy.max_sleep_minutes == 720
    assert pm.physiology.energy.collapse.grace_minutes == 60
    assert pm.physiology.energy.collapse.sleep_hours == 12
    assert pm.physiology.energy.collapse.mood_penalty == 20
    assert pm.physiology.satiety.mode == "disabled"
    assert pm.physiology.satiety.decay_per_hour == 1.0
    assert pm.physiology.satiety.recovery_per_minute == 0.5
    assert pm.physiology.satiety.max_eat_minutes == 60
    assert pm.mood.decay_per_hour == 0.5
    assert pm.mood.social_boost == 3.0
    assert pm.consolidation.daily_fallback_hour == 4

    assert pm.age.default_age is None
    assert pm.age.overrides == {}
    assert len(pm.age.brackets) == 6
    for bracket in pm.age.brackets:
        assert bracket.name
        assert bracket.min >= 0
        assert bracket.energy_decay_mult > 0
        assert bracket.energy_recovery_mult > 0
        assert bracket.satiety_decay_mult > 0
        assert bracket.mood_volatility_mult > 0
        assert 0 <= bracket.bedtime_hour < 24
        assert 0 <= bracket.wakeup_hour < 24
        assert bracket.ideal_sleep_hours > 0
        assert bracket.monologue_style
        assert bracket.emotional_hint
        assert bracket.social_hint
    first = pm.age.brackets[0]
    assert first.name == "幼年"
    assert first.min == 0
    assert first.max == 5
    assert first.energy_decay_mult == 1.25
    assert first.energy_recovery_mult == 1.15
    assert first.satiety_decay_mult == 1.25
    assert first.mood_volatility_mult == 1.4
    assert first.bedtime_hour == 20.0
    assert first.wakeup_hour == 7.0
    assert first.ideal_sleep_hours == 11.0
    assert first.monologue_style
    assert first.emotional_hint
    assert first.social_hint
    assert pm.age.brackets[-1].max is None


def test_persona_management_age_brackets_accept_plan_yaml_bounds():
    cfg = RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "ds", "model": "deepseek-chat"}},
            "providers": {"ds": {"preset": "deepseek", "api_key_id": "ds_main"}},
            "persona_management": {
                "age": {
                    "brackets": [
                        {
                            "name": "unit",
                            "min": 1,
                            "max": 2,
                            "energy_decay_mult": 1.2,
                            "energy_recovery_mult": 0.9,
                            "satiety_decay_mult": 1.1,
                            "mood_volatility_mult": 1.3,
                            "bedtime_hour": 21,
                            "wakeup_hour": 6,
                            "ideal_sleep_hours": 9,
                            "monologue_style": "unit-style",
                            "emotional_hint": "unit-emotion",
                            "social_hint": "unit-social",
                        }
                    ]
                }
            },
        }
    )

    bracket = cfg.persona_management.age.brackets[0]
    assert bracket.min == 1
    assert bracket.max == 2


def test_persona_management_background_agents_can_inherit_chat_model():
    cfg = RootConfig.model_validate(
        {
            "agents": {"chat": {"provider": "ds", "model": "deepseek-chat"}},
            "providers": {"ds": {"preset": "deepseek", "api_key_id": "ds_main"}},
            "persona_management": {
                "enabled": True,
                "persona_agent": {"provider": "", "model": ""},
                "social_agent": {"enabled": True, "provider": "", "model": ""},
                "subconscious": {"enabled": True, "provider": "", "model": ""},
            },
        }
    )

    assert cfg.persona_management.persona_agent.provider == ""
    assert cfg.persona_management.persona_agent.model == ""
    assert cfg.persona_management.social_agent.provider == ""
    assert cfg.persona_management.subconscious.model == ""


def test_persona_management_background_agent_provider_must_exist_when_non_empty():
    with pytest.raises(
        ValueError,
        match=r"persona_management\.persona_agent\.provider .*未在 providers 中定义",
    ):
        RootConfig.model_validate(
            {
                "agents": {"chat": {"provider": "ds", "model": "deepseek-chat"}},
                "providers": {"ds": {"preset": "deepseek", "api_key_id": "ds_main"}},
                "persona_management": {
                    "persona_agent": {"provider": "missing", "model": "x"},
                },
            }
        )


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


def test_extra_fields_ignored():
    """未知字段应被容忍，但不能被回写到导出配置。"""
    cfg = RootConfig.model_validate(
        {
            "agents": {
                "chat": {
                    "provider": "ds",
                    "model": "x",
                    "unknown_chat_field": "ignored",
                },
            },
            "providers": {"ds": {"preset": "deepseek"}},
            "behavior": {
                "unknown_behavior_field": "ignored",
            },
            "unknown_field": "ignored",
        }
    )

    dumped = cfg.model_dump()

    assert "unknown_field" not in dumped
    assert "unknown_behavior_field" not in dumped["behavior"]
    assert "unknown_chat_field" not in dumped["agents"]["chat"]


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


def test_agent_tool_loop_reminder_defaults_and_validation():
    cfg = AgentConfig(provider="x", model="y")

    assert cfg.max_loops == 25
    assert cfg.tool_loop_reminder_interval == 8
    assert cfg.tool_loop_final_warning_count == 4
    assert cfg.tool_loop_final_grace_loops == 2
    assert cfg.tool_loop_final_max_tokens == 4096

    with pytest.raises(ValidationError):
        AgentConfig(provider="x", model="y", tool_loop_reminder_interval=0)
    with pytest.raises(ValidationError):
        AgentConfig(provider="x", model="y", tool_loop_final_warning_count=0)
    with pytest.raises(ValidationError):
        AgentConfig(provider="x", model="y", tool_loop_final_grace_loops=-1)
    with pytest.raises(ValidationError):
        AgentConfig(provider="x", model="y", tool_loop_final_max_tokens=511)


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
