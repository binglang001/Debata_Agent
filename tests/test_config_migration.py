from __future__ import annotations

from copy import deepcopy

from app_config.config_migration import CURRENT_CONFIG_VERSION, migrate_config
from app_config.schema import RootConfig


def _legacy_config() -> dict:
    return {
        "version": 1,
        "app": {"name": "Debata_Agent"},
        "adapters": {
            "default": {
                "type": "napcat",
                "whitelist": {
                    "mode": "all",
                    "qq_ids": [10001],
                    "group_ids": [20002],
                },
            }
        },
        "providers": {
            "ds": {
                "preset": "deepseek",
                "api_key_id": "ds_main",
                "protocol": "qwen",
                "timeout": 45.0,
            },
            "anthropic": {
                "preset": "anthropic",
                "api_key_id": "claude_key",
                "protocol": "anthropic",
                "timeout": 60.0,
            },
        },
        "agents": {
            "chat": {
                "provider": "ds",
                "model": "deepseek-chat",
                "first_token_timeout": 12.0,
            },
            "summary": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "first_token_timeout": 15.0,
            },
        },
        "behavior": {
            "merge_window": 1.5,
            "recall_merge_window": 2.5,
            "greeting_interval": 30.0,
            "summarize": {"chat_history_count": 222},
            "typing": {"max_delay": 6.0},
            "rate_limit": {"window": 120, "max_messages": 3},
        },
    }


def test_legacy_config_maps_all_phase_45_keys_and_validates_as_root_config():
    migrated, report = migrate_config(_legacy_config())

    assert report.changed is True
    assert report.from_version == 1
    assert report.to_version == CURRENT_CONFIG_VERSION
    assert report.applied_migrations == ("config.v1_to_v2",)
    assert migrated["version"] == CURRENT_CONFIG_VERSION

    behavior = migrated["behavior"]
    assert behavior["merge_window_seconds"] == 1.5
    assert behavior["recall_merge_window_seconds"] == 2.5
    assert behavior["proactive_think_interval_seconds"] == 30.0
    assert behavior["default_history_fetch_count"] == 222
    assert behavior["rate_limit"]["window_seconds"] == 120
    assert "max_delay_seconds" not in behavior["typing"]
    assert migrated["providers"]["ds"]["timeout_seconds"] == 45.0
    assert migrated["agents"]["chat"]["first_token_timeout_seconds"] == 12.0
    assert migrated["adapters"]["default"]["whitelist"]["mode"] == "open"
    assert migrated["providers"]["ds"]["protocol"] == "openai_compat"
    assert migrated["providers"]["anthropic"]["protocol"] == "anthropic"

    assert "merge_window" not in behavior
    assert "recall_merge_window" not in behavior
    assert "greeting_interval" not in behavior
    assert "chat_history_count" not in behavior["summarize"]
    assert "max_delay" not in behavior["typing"]
    assert "window" not in behavior["rate_limit"]
    assert "timeout" not in migrated["providers"]["ds"]
    assert "first_token_timeout" not in migrated["agents"]["chat"]

    renamed = {(item.old_path, item.new_path, item.status) for item in report.renamed_paths}
    assert (
        "behavior.summarize.chat_history_count",
        "behavior.default_history_fetch_count",
        "renamed",
    ) in renamed
    assert (
        "agents.summary.first_token_timeout",
        "agents.summary.first_token_timeout_seconds",
        "renamed",
    ) in renamed
    assert ("behavior.typing.max_delay", "", "removed") in renamed
    assert any("behavior.typing.max_delay 已删除" in warning for warning in report.warnings)
    value_mappings = {(item.path, item.old_value, item.new_value) for item in report.value_mappings}
    assert ("adapters.default.whitelist.mode", "all", "open") in value_mappings
    assert ("providers.ds.protocol", "qwen", "openai_compat") in value_mappings

    RootConfig.model_validate(migrated)


def test_current_version_with_phase_45_legacy_keys_is_normalized():
    raw = _legacy_config()
    raw["version"] = CURRENT_CONFIG_VERSION

    migrated, report = migrate_config(raw)

    assert report.changed is True
    assert report.from_version == CURRENT_CONFIG_VERSION
    assert report.to_version == CURRENT_CONFIG_VERSION
    assert report.applied_migrations == ()
    assert migrated["version"] == CURRENT_CONFIG_VERSION
    assert migrated["behavior"]["merge_window_seconds"] == 1.5
    assert migrated["providers"]["ds"]["timeout_seconds"] == 45.0
    assert migrated["agents"]["chat"]["first_token_timeout_seconds"] == 12.0
    assert migrated["adapters"]["default"]["whitelist"]["mode"] == "open"
    assert migrated["providers"]["ds"]["protocol"] == "openai_compat"
    assert "merge_window" not in migrated["behavior"]
    assert "timeout" not in migrated["providers"]["ds"]
    assert "first_token_timeout" not in migrated["agents"]["chat"]
    RootConfig.model_validate(migrated)


def test_current_version_without_phase_45_legacy_keys_is_unchanged():
    raw, _report = migrate_config(_legacy_config())

    migrated, report = migrate_config(raw)

    assert migrated == raw
    assert report.changed is False
    assert report.from_version == CURRENT_CONFIG_VERSION
    assert report.to_version == CURRENT_CONFIG_VERSION
    assert report.applied_migrations == ()


def test_existing_new_field_wins_and_old_field_is_removed():
    raw = _legacy_config()
    raw["behavior"]["merge_window_seconds"] = 9.0
    raw["behavior"]["summarize"]["chat_history_count"] = 333
    raw["behavior"]["default_history_fetch_count"] = 444
    raw["providers"]["ds"]["timeout_seconds"] = 99.0
    raw["agents"]["chat"]["first_token_timeout_seconds"] = 88.0

    migrated, report = migrate_config(raw)

    assert migrated["behavior"]["merge_window_seconds"] == 9.0
    assert migrated["behavior"]["default_history_fetch_count"] == 444
    assert migrated["providers"]["ds"]["timeout_seconds"] == 99.0
    assert migrated["agents"]["chat"]["first_token_timeout_seconds"] == 88.0
    assert "merge_window" not in migrated["behavior"]
    assert "chat_history_count" not in migrated["behavior"]["summarize"]
    assert "timeout" not in migrated["providers"]["ds"]
    assert "first_token_timeout" not in migrated["agents"]["chat"]
    assert any(item.status == "conflict" for item in report.renamed_paths)


def test_migrate_config_does_not_modify_input():
    raw = _legacy_config()
    before = deepcopy(raw)

    migrated, _report = migrate_config(raw)

    assert raw == before
    assert migrated is not raw
    assert migrated["behavior"] is not raw["behavior"]
    assert "merge_window" in raw["behavior"]


def test_future_version_is_preserved_and_warned_without_crashing():
    raw = _legacy_config()
    raw["version"] = CURRENT_CONFIG_VERSION + 7

    migrated, report = migrate_config(raw)

    assert migrated["version"] == CURRENT_CONFIG_VERSION + 7
    assert report.changed is False
    assert report.future_version is True
    assert report.from_version == CURRENT_CONFIG_VERSION + 7
    assert report.to_version == CURRENT_CONFIG_VERSION + 7
    assert report.applied_migrations == ()
    assert any("高于当前支持版本" in warning for warning in report.warnings)


def test_missing_version_and_old_version_are_upgraded_to_current():
    missing_version = _legacy_config()
    missing_version.pop("version")
    migrated_missing, report_missing = migrate_config(missing_version)

    old_version = _legacy_config()
    old_version["version"] = 1
    migrated_old, report_old = migrate_config(old_version)

    assert migrated_missing["version"] == CURRENT_CONFIG_VERSION
    assert report_missing.from_version == 1
    assert report_missing.to_version == CURRENT_CONFIG_VERSION
    assert migrated_old["version"] == CURRENT_CONFIG_VERSION
    assert report_old.from_version == 1
    assert report_old.to_version == CURRENT_CONFIG_VERSION


def test_provider_protocol_known_legacy_values_change_but_current_values_stay():
    raw = _legacy_config()
    raw["providers"] = {
        "gemini": {
            "protocol": "gemini",
            "base_url": "https://gemini.example.com/v1",
            "api_key_id": "gemini_key",
        },
        "volc": {
            "protocol": "volcengine",
            "base_url": "https://volc.example.com/v1",
            "api_key_id": "volc_key",
        },
        "qwen": {
            "protocol": "qwen",
            "base_url": "https://qwen.example.com/v1",
            "api_key_id": "qwen_key",
        },
        "glm": {
            "protocol": "glm",
            "base_url": "https://glm.example.com/v1",
            "api_key_id": "glm_key",
        },
        "openai": {
            "protocol": "openai_compat",
            "base_url": "https://openai.example.com/v1",
            "api_key_id": "openai_key",
        },
        "anthropic": {
            "protocol": "anthropic",
            "base_url": "https://anthropic.example.com",
            "api_key_id": "anthropic_key",
        },
    }
    raw["agents"]["chat"]["provider"] = "gemini"
    raw["agents"]["summary"]["provider"] = "anthropic"

    migrated, report = migrate_config(raw)

    assert migrated["providers"]["gemini"]["protocol"] == "openai_compat"
    assert migrated["providers"]["volc"]["protocol"] == "openai_compat"
    assert migrated["providers"]["qwen"]["protocol"] == "openai_compat"
    assert migrated["providers"]["glm"]["protocol"] == "openai_compat"
    assert migrated["providers"]["openai"]["protocol"] == "openai_compat"
    assert migrated["providers"]["anthropic"]["protocol"] == "anthropic"
    mapped_paths = {item.path for item in report.value_mappings}
    assert "providers.gemini.protocol" in mapped_paths
    assert "providers.volc.protocol" in mapped_paths
    assert "providers.qwen.protocol" in mapped_paths
    assert "providers.glm.protocol" in mapped_paths
    assert "providers.openai.protocol" not in mapped_paths
    assert "providers.anthropic.protocol" not in mapped_paths
    RootConfig.model_validate(migrated)


def test_unknown_protocol_is_preserved_with_warning():
    raw = _legacy_config()
    raw["providers"]["ds"]["protocol"] = "experimental_proto"

    migrated, report = migrate_config(raw)

    assert migrated["providers"]["ds"]["protocol"] == "experimental_proto"
    assert any("不是已知历史协议" in warning for warning in report.warnings)


def test_wrong_nested_types_warn_and_keep_original_shape():
    raw = _legacy_config()
    raw["behavior"]["typing"] = "fast"
    raw["providers"]["broken"] = "not-a-provider"
    raw["adapters"]["default"]["whitelist"] = "all"

    migrated, report = migrate_config(raw)

    assert migrated["behavior"]["typing"] == "fast"
    assert migrated["providers"]["broken"] == "not-a-provider"
    assert migrated["adapters"]["default"]["whitelist"] == "all"
    assert any("behavior.typing 不是对象" in warning for warning in report.warnings)
    assert any("providers.broken 不是对象" in warning for warning in report.warnings)
    assert any("adapters.default.whitelist 不是对象" in warning for warning in report.warnings)
