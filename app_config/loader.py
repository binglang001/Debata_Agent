"""配置加载与保存 —— YAML ↔ Pydantic RootConfig。

读：
    cfg = load_config(paths)            # 校验 schema
    cfg = get_config()                  # 后续模块通过全局单例访问

写：
    save_config(paths, cfg)             # 原子写入，自动备份上一版本

注意：
    不在这里做密钥解析。密钥通过 SecretsManager 单独管理，
    配置文件中只保存 *_key_id 引用。
"""

from __future__ import annotations

import logging
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import yaml
from pydantic import ValidationError

from .config_migration import CURRENT_CONFIG_VERSION, ConfigMigrationReport, migrate_config
from .paths import AppPaths
from .schema import RootConfig

logger = logging.getLogger(__name__)

_active_config: RootConfig | None = None
_MAX_FUTURE_CONFIG_PRUNE_PASSES = 20
_FUTURE_PRUNE_VALUE_ERROR_TYPES = frozenset({"enum", "literal_error"})


def _user_error_text(e: Exception) -> str:
    """Pydantic ValidationError → 带字段详情；其它异常 → 原样截断 500 字。"""
    if isinstance(e, ValidationError):
        lines = ["配置校验未通过："]
        for err in e.errors():
            loc = " → ".join(str(p) for p in err["loc"]) if err.get("loc") else "顶层"
            lines.append(f"  · {loc}：{err.get('msg', '未知错误')}")
        return "\n".join(lines[:20])  # 最多展示 20 条
    msg = str(e).strip()
    if len(msg) > 500:
        msg = msg[:500] + "..."
    return msg


class ConfigError(Exception):
    """配置相关错误。"""


def _config_to_data(cfg: RootConfig) -> dict[str, Any]:
    """把 Pydantic 配置转成适合 YAML 写回的普通 dict。"""
    return orjson.loads(cfg.model_dump_json(exclude_none=True))


def _dump_config_data(data: dict[str, Any]) -> str:
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
    )


def dump_config_preserving_comments(existing_text: str, data: dict[str, Any]) -> str:
    """写回配置时保留原文件中的纯注释行和空行。

    PyYAML 无法 round-trip 注释；这里采用保守策略：配置数据完全以当前
    schema dump 为准，只把原文件中的纯注释行和空行提升到文件头部。
    """
    dumped = _dump_config_data(data)
    preserved_lines: list[str] = []
    has_comment = False

    for line in existing_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            preserved_lines.append(line.rstrip())
            has_comment = True
        elif stripped == "":
            preserved_lines.append("")

    if not has_comment:
        return dumped

    while preserved_lines and preserved_lines[-1] == "":
        preserved_lines.pop()
    if not preserved_lines:
        return dumped
    return "\n".join(preserved_lines) + "\n" + dumped


def _read_existing_version_label(existing_text: str) -> str:
    try:
        raw = yaml.safe_load(existing_text)
    except yaml.YAMLError:
        return "unknown"
    if not isinstance(raw, dict):
        return "unknown"
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return "unknown"
    return str(version)


def _backup_dir(paths: AppPaths) -> Path:
    data_dir = paths.DATA_DIR.resolve()
    backup_dir = (paths.DATA_DIR / "config_backups").resolve()
    if not backup_dir.is_relative_to(data_dir):
        raise ConfigError(f"配置备份目录不在 DATA_DIR 下: {backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


def _with_collision_suffix(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ConfigError(f"无法创建不覆盖旧文件的配置备份: {path}")


def _create_versioned_backup(paths: AppPaths, existing_text: str) -> Path:
    old_version = _read_existing_version_label(existing_text)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = _with_collision_suffix(
        _backup_dir(paths) / f"config-{timestamp}-v{old_version}.yaml"
    )
    shutil.copy2(paths.CONFIG_FILE, backup_path)
    logger.info("配置备份已创建: %s", backup_path)
    return backup_path


def _save_config_data(
    paths: AppPaths,
    data: dict[str, Any],
    *,
    backup: bool,
    existing_text: str | None = None,
) -> None:
    paths.ensure_data_dirs()
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if existing_text is None and paths.CONFIG_FILE.exists():
        existing_text = paths.CONFIG_FILE.read_text(encoding="utf-8")

    if backup and paths.CONFIG_FILE.exists():
        _create_versioned_backup(paths, existing_text or "")

    text = (
        _dump_config_data(data)
        if existing_text is None
        else dump_config_preserving_comments(existing_text, data)
    )
    tmp_path = paths.CONFIG_FILE.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    tmp_path.replace(paths.CONFIG_FILE)


def _log_migration_report(report: ConfigMigrationReport) -> None:
    for warning in report.warnings:
        logger.warning("%s", warning)

    if not report.changed:
        return

    logger.info(
        "配置迁移已应用: v%s -> v%s, steps=%s, renamed=%s, value_mappings=%s",
        report.from_version,
        report.to_version,
        list(report.applied_migrations),
        len(report.renamed_paths),
        len(report.value_mappings),
    )


def _format_error_path(loc: tuple[Any, ...]) -> str:
    if not loc:
        return "<root>"
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            if parts:
                parts[-1] += f"[{item}]"
            else:
                parts.append(f"[{item}]")
        else:
            parts.append(str(item))
    return ".".join(parts)


def _remove_config_path(data: Any, loc: tuple[Any, ...]) -> bool:
    if not loc:
        return False

    node = data
    for part in loc[:-1]:
        if isinstance(part, int):
            if not isinstance(node, list) or part < 0 or part >= len(node):
                return False
            node = node[part]
            continue
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]

    leaf = loc[-1]
    if isinstance(leaf, int):
        if isinstance(node, list) and 0 <= leaf < len(node):
            node.pop(leaf)
            return True
        return False
    if isinstance(node, dict) and leaf in node:
        node.pop(leaf)
        return True
    return False


def _is_safe_future_prune_error(err: dict[str, Any], candidate: dict[str, Any]) -> bool:
    loc = tuple(err.get("loc") or ())
    if err.get("type") not in _FUTURE_PRUNE_VALUE_ERROR_TYPES:
        return False

    if loc == ("app", "theme"):
        return True

    if len(loc) == 3 and loc[0] == "providers" and loc[2] == "protocol":
        providers = candidate.get("providers")
        if not isinstance(providers, dict):
            return False
        provider = providers.get(loc[1])
        return isinstance(provider, dict) and bool(provider.get("preset"))

    return False


def _validate_future_config_subset(raw: dict[str, Any]) -> RootConfig:
    candidate = deepcopy(raw)

    for _ in range(_MAX_FUTURE_CONFIG_PRUNE_PASSES):
        try:
            return RootConfig.model_validate(candidate)
        except ValidationError as e:
            prunable_errors: list[dict[str, Any]] = []
            unsafe_errors: list[dict[str, Any]] = []
            for err in e.errors():
                if _is_safe_future_prune_error(err, candidate):
                    prunable_errors.append(err)
                else:
                    unsafe_errors.append(err)

            if unsafe_errors or not prunable_errors:
                raise ConfigError(
                    "未来版本配置无法提取当前兼容子集，存在当前版本不能安全忽略的配置段或校验错误：\n"
                    + _user_error_text(e)
                ) from e

            removed_any = False
            for err in prunable_errors:
                loc = tuple(err.get("loc") or ())
                if not _remove_config_path(candidate, loc):
                    continue
                removed_any = True
                logger.warning(
                    "未来版本配置字段 %s 当前版本无法校验，已在本次加载中忽略: %s",
                    _format_error_path(loc),
                    err.get("msg", "未知错误"),
                )
            if not removed_any:
                raise ConfigError(
                    "未来版本配置无法提取当前兼容子集，无法定位可忽略的叶子字段：\n"
                    + _user_error_text(e)
                ) from e

    raise ConfigError("未来版本配置无法在有限次数内提取当前兼容子集")


def load_config(paths: AppPaths, set_global: bool = True) -> RootConfig:
    """从磁盘加载并校验配置。

    Args:
        paths: AppPaths 实例。
        set_global: 是否同时设为全局单例（供 get_config 访问）。
    """
    global _active_config

    if not paths.CONFIG_FILE.exists():
        raise ConfigError(
            f"配置文件不存在: {paths.CONFIG_FILE}\n"
            f"请先运行首次配置向导（python main.py --setup）"
        )

    try:
        existing_text = paths.CONFIG_FILE.read_text(encoding="utf-8")
        raw: Any = yaml.safe_load(existing_text)
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败: {paths.CONFIG_FILE}\n{e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点应为字典，得到 {type(raw).__name__}")

    migrated_raw, migration_report = migrate_config(raw)
    _log_migration_report(migration_report)

    if migration_report.future_version:
        cfg = _validate_future_config_subset(migrated_raw)
    else:
        try:
            cfg = RootConfig.model_validate(migrated_raw)
        except ValidationError as e:
            raise ConfigError(_user_error_text(e)) from e

    if migration_report.changed and not migration_report.future_version:
        _save_config_data(
            paths,
            _config_to_data(cfg),
            backup=True,
            existing_text=existing_text,
        )
        logger.info("配置迁移已写回: %s", paths.CONFIG_FILE)

    if set_global:
        _active_config = cfg

    logger.info(
        f"配置已加载: persona={cfg.persona.active}, "
        f"adapters={list(cfg.adapters.keys())}, "
        f"providers={list(cfg.providers.keys())}"
    )
    return cfg


def get_config() -> RootConfig:
    """获取全局单例配置。"""
    if _active_config is None:
        raise ConfigError("配置尚未加载，请先调用 load_config()")
    return _active_config


def save_config(paths: AppPaths, cfg: RootConfig, backup: bool = True) -> None:
    """原子保存配置到磁盘。

    Args:
        backup: 是否在覆盖前创建版本化备份。
    """
    if cfg.version > CURRENT_CONFIG_VERSION:
        raise ConfigError(
            f"配置版本 v{cfg.version} 高于当前支持版本 v{CURRENT_CONFIG_VERSION}，"
            "禁止用当前 schema 子集覆盖未来版本配置文件。"
        )
    _save_config_data(paths, _config_to_data(cfg), backup=backup)
    logger.info(f"配置已保存: {paths.CONFIG_FILE}")


def set_active_config(cfg: RootConfig) -> None:
    """供测试或迁移场景设置全局单例。"""
    global _active_config
    _active_config = cfg
