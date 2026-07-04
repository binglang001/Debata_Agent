from __future__ import annotations

import logging

import pytest
import yaml

from app_config.config_migration import CURRENT_CONFIG_VERSION
from app_config.loader import ConfigError, load_config, save_config


def _legacy_config() -> dict:
    return {
        "version": 1,
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
            }
        },
        "agents": {
            "chat": {
                "provider": "ds",
                "model": "deepseek-chat",
                "first_token_timeout": 12.0,
            }
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


def _future_config() -> dict:
    raw = _legacy_config()
    raw["version"] = CURRENT_CONFIG_VERSION + 1
    raw["adapters"]["default"]["whitelist"]["mode"] = "open"
    raw["providers"]["ds"]["protocol"] = "openai_compat"
    raw["providers"]["ds"]["timeout_seconds"] = raw["providers"]["ds"].pop("timeout")
    raw["agents"]["chat"]["first_token_timeout_seconds"] = raw["agents"]["chat"].pop(
        "first_token_timeout"
    )
    behavior = raw["behavior"]
    behavior["merge_window_seconds"] = behavior.pop("merge_window")
    behavior["recall_merge_window_seconds"] = behavior.pop("recall_merge_window")
    behavior["proactive_think_interval_seconds"] = behavior.pop("greeting_interval")
    behavior["default_history_fetch_count"] = behavior["summarize"].pop("chat_history_count")
    behavior["typing"].pop("max_delay")
    behavior["rate_limit"]["window_seconds"] = behavior["rate_limit"].pop("window")
    return raw


def _write_yaml(tmp_paths, raw: dict, *, header: str = "") -> str:
    text = header + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    tmp_paths.CONFIG_FILE.write_text(text, encoding="utf-8")
    return text


def _backup_files(tmp_paths):
    return list((tmp_paths.DATA_DIR / "config_backups").glob("config-*.yaml"))


def test_load_config_migrates_legacy_yaml_and_creates_versioned_backup(tmp_paths):
    original_text = _write_yaml(tmp_paths, _legacy_config())

    cfg = load_config(tmp_paths, set_global=False)

    assert cfg.version == CURRENT_CONFIG_VERSION
    assert cfg.behavior.merge_window_seconds == 1.5
    assert cfg.behavior.recall_merge_window_seconds == 2.5
    assert cfg.behavior.proactive_think_interval_seconds == 30.0
    assert cfg.behavior.default_history_fetch_count == 222
    assert cfg.providers["ds"].timeout_seconds == 45.0
    assert cfg.agents.chat.first_token_timeout_seconds == 12.0
    assert cfg.adapters["default"].whitelist.mode == "open"
    assert cfg.providers["ds"].protocol == "openai_compat"

    saved = yaml.safe_load(tmp_paths.CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["version"] == CURRENT_CONFIG_VERSION
    assert saved["behavior"]["merge_window_seconds"] == 1.5
    assert saved["behavior"]["recall_merge_window_seconds"] == 2.5
    assert saved["behavior"]["proactive_think_interval_seconds"] == 30.0
    assert saved["behavior"]["default_history_fetch_count"] == 222
    assert saved["providers"]["ds"]["timeout_seconds"] == 45.0
    assert saved["agents"]["chat"]["first_token_timeout_seconds"] == 12.0
    assert saved["adapters"]["default"]["whitelist"]["mode"] == "open"
    assert saved["providers"]["ds"]["protocol"] == "openai_compat"

    assert "merge_window" not in saved["behavior"]
    assert "recall_merge_window" not in saved["behavior"]
    assert "greeting_interval" not in saved["behavior"]
    assert "chat_history_count" not in saved["behavior"]["summarize"]
    assert "max_delay" not in saved["behavior"]["typing"]
    assert "window" not in saved["behavior"]["rate_limit"]
    assert "timeout" not in saved["providers"]["ds"]
    assert "first_token_timeout" not in saved["agents"]["chat"]

    backups = _backup_files(tmp_paths)
    assert len(backups) == 1
    assert backups[0].parent == tmp_paths.DATA_DIR / "config_backups"
    assert backups[0].name.endswith("-v1.yaml")
    assert backups[0].read_text(encoding="utf-8") == original_text


def test_load_config_normalizes_current_version_legacy_keys_and_writes_backup(tmp_paths):
    raw = _legacy_config()
    raw["version"] = CURRENT_CONFIG_VERSION
    original_text = _write_yaml(tmp_paths, raw)

    cfg = load_config(tmp_paths, set_global=False)

    assert cfg.version == CURRENT_CONFIG_VERSION
    assert cfg.behavior.merge_window_seconds == 1.5
    assert cfg.providers["ds"].timeout_seconds == 45.0
    assert cfg.agents.chat.first_token_timeout_seconds == 12.0
    assert cfg.adapters["default"].whitelist.mode == "open"
    assert cfg.providers["ds"].protocol == "openai_compat"

    saved = yaml.safe_load(tmp_paths.CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["version"] == CURRENT_CONFIG_VERSION
    assert saved["behavior"]["merge_window_seconds"] == 1.5
    assert "merge_window" not in saved["behavior"]
    assert "timeout" not in saved["providers"]["ds"]
    assert "first_token_timeout" not in saved["agents"]["chat"]

    backups = _backup_files(tmp_paths)
    assert len(backups) == 1
    assert backups[0].name.endswith(f"-v{CURRENT_CONFIG_VERSION}.yaml")
    assert backups[0].read_text(encoding="utf-8") == original_text


def test_load_config_migration_writeback_preserves_header_comments(tmp_paths):
    text = """# Debata 配置

version: 1
adapters:
  # 段内适配器说明
  default:
    type: napcat
    whitelist:
      mode: all
      qq_ids:
      - 10001
      group_ids:
      - 20002
providers:
  ds:
    preset: deepseek
    api_key_id: ds_main
    protocol: qwen # inline 注释不保证保留
    timeout: 45.0
agents:
  chat:
    provider: ds
    model: deepseek-chat
    first_token_timeout: 12.0
behavior:
  merge_window: 1.5
  recall_merge_window: 2.5
  greeting_interval: 30.0
  summarize:
    chat_history_count: 222
  typing:
    max_delay: 6.0
  rate_limit:
    window: 120
    max_messages: 3
"""
    tmp_paths.CONFIG_FILE.write_text(text, encoding="utf-8")

    load_config(tmp_paths, set_global=False)

    saved_text = tmp_paths.CONFIG_FILE.read_text(encoding="utf-8")
    assert "# Debata 配置" in saved_text
    assert "# 段内适配器说明" in saved_text
    saved = yaml.safe_load(saved_text)
    assert saved["version"] == CURRENT_CONFIG_VERSION
    assert "merge_window" not in saved["behavior"]


def test_load_config_future_version_warns_without_writeback_or_backup(tmp_paths, caplog):
    raw = _future_config()
    raw["app"] = {"theme": "future_theme"}
    raw["providers"]["ds"]["protocol"] = "future_protocol"
    original_text = _write_yaml(tmp_paths, raw)

    with caplog.at_level(logging.WARNING, logger="app_config.loader"):
        cfg = load_config(tmp_paths, set_global=False)

    assert cfg.version == CURRENT_CONFIG_VERSION + 1
    assert cfg.app.theme == "auto"
    assert cfg.providers["ds"].protocol is None
    assert tmp_paths.CONFIG_FILE.read_text(encoding="utf-8") == original_text
    assert _backup_files(tmp_paths) == []
    assert any("高于当前支持版本" in record.message for record in caplog.records)
    assert any("app.theme" in record.message for record in caplog.records)
    assert any("providers.ds.protocol" in record.message for record in caplog.records)


def test_load_config_invalid_migrated_schema_does_not_writeback_or_backup(tmp_paths):
    raw = _legacy_config()
    raw["agents"]["chat"]["provider"] = "missing"
    original_text = _write_yaml(tmp_paths, raw)

    with pytest.raises(ConfigError, match="配置校验未通过"):
        load_config(tmp_paths, set_global=False)

    assert tmp_paths.CONFIG_FILE.read_text(encoding="utf-8") == original_text
    assert _backup_files(tmp_paths) == []


def test_load_config_future_version_with_old_keys_is_not_written_back(tmp_paths):
    raw = _future_config()
    raw["version"] = CURRENT_CONFIG_VERSION + 5
    original_text = _write_yaml(tmp_paths, raw)

    cfg = load_config(tmp_paths, set_global=False)

    assert cfg.version == CURRENT_CONFIG_VERSION + 5
    assert tmp_paths.CONFIG_FILE.read_text(encoding="utf-8") == original_text
    assert _backup_files(tmp_paths) == []


def test_save_config_rejects_loaded_future_version_without_writing_or_backup(tmp_paths):
    raw = _future_config()
    raw["version"] = CURRENT_CONFIG_VERSION + 5
    raw["app"] = {"theme": "future_theme"}
    original_text = _write_yaml(tmp_paths, raw)

    cfg = load_config(tmp_paths, set_global=False)

    with pytest.raises(ConfigError, match="禁止用当前 schema 子集覆盖未来版本配置文件"):
        save_config(tmp_paths, cfg)

    assert tmp_paths.CONFIG_FILE.read_text(encoding="utf-8") == original_text
    assert _backup_files(tmp_paths) == []


def test_future_config_unsafe_provider_subset_raises_without_deleting_provider(tmp_paths):
    raw = _future_config()
    raw["version"] = CURRENT_CONFIG_VERSION + 5
    raw["providers"]["ds"].pop("preset", None)
    raw["providers"]["ds"]["protocol"] = "future_protocol"
    raw["providers"]["ds"]["base_url"] = "https://future.example.com/v1"
    original_text = _write_yaml(tmp_paths, raw)

    with pytest.raises(ConfigError, match="无法提取当前兼容子集"):
        load_config(tmp_paths, set_global=False)

    assert tmp_paths.CONFIG_FILE.read_text(encoding="utf-8") == original_text
    assert _backup_files(tmp_paths) == []


def test_future_config_adapter_type_raises_without_pruning_or_backup(tmp_paths):
    raw = _future_config()
    raw["version"] = CURRENT_CONFIG_VERSION + 5
    raw["adapters"]["default"]["type"] = "discord"
    original_text = _write_yaml(tmp_paths, raw)

    with pytest.raises(ConfigError, match="无法提取当前兼容子集"):
        load_config(tmp_paths, set_global=False)

    assert tmp_paths.CONFIG_FILE.read_text(encoding="utf-8") == original_text
    assert _backup_files(tmp_paths) == []


def test_future_config_adapter_mode_raises_without_pruning_or_backup(tmp_paths):
    raw = _future_config()
    raw["version"] = CURRENT_CONFIG_VERSION + 5
    raw["adapters"]["default"]["mode"] = "proxy"
    original_text = _write_yaml(tmp_paths, raw)

    with pytest.raises(ConfigError, match="无法提取当前兼容子集"):
        load_config(tmp_paths, set_global=False)

    assert tmp_paths.CONFIG_FILE.read_text(encoding="utf-8") == original_text
    assert _backup_files(tmp_paths) == []
