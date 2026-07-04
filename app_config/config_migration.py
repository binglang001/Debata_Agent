"""配置纯数据迁移与历史键规范化。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from app_config.schema import RootConfig

_root_version = RootConfig.model_fields["version"].default
CURRENT_CONFIG_VERSION = int(_root_version)

_CONFIG_PLANE = "config"
_MISSING = object()

PathStatus = Literal["renamed", "conflict", "removed"]
MigrationHandler = Callable[[dict[str, Any], "_ReportBuilder"], None]


@dataclass(frozen=True, slots=True)
class PathRename:
    """一次旧字段路径到新字段路径的处理记录。"""

    old_path: str
    new_path: str
    status: PathStatus


@dataclass(frozen=True, slots=True)
class ValueMapping:
    """一次历史枚举值到当前枚举值的处理记录。"""

    path: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True, slots=True)
class ConfigMigrationReport:
    """配置迁移报告，供后续 loader 写日志或展示给用户。"""

    changed: bool
    from_version: int
    to_version: int
    renamed_paths: tuple[PathRename, ...]
    warnings: tuple[str, ...]
    value_mappings: tuple[ValueMapping, ...] = ()
    applied_migrations: tuple[str, ...] = ()
    future_version: bool = False


class ConfigMigrationError(RuntimeError):
    """配置迁移链不完整或定义错误。"""


@dataclass(frozen=True, slots=True)
class ConfigMigrationStep:
    """配置平面中的单步迁移。"""

    from_version: int
    to_version: int
    handler: MigrationHandler

    @property
    def migration_id(self) -> str:
        return f"{_CONFIG_PLANE}.v{self.from_version}_to_v{self.to_version}"


@dataclass(slots=True)
class ConfigMigrationRegistry:
    """注册式配置迁移链。"""

    current_version: int
    _steps: dict[int, ConfigMigrationStep] = field(default_factory=dict)

    def register(
        self,
        from_version: int,
        to_version: int,
    ) -> Callable[[MigrationHandler], MigrationHandler]:
        if to_version <= from_version:
            raise ConfigMigrationError(f"配置迁移步骤必须升序：v{from_version} -> v{to_version}")
        if to_version > self.current_version:
            raise ConfigMigrationError(f"配置迁移目标 v{to_version} 高于当前支持版本 v{self.current_version}")
        if from_version in self._steps:
            raise ConfigMigrationError(f"配置 v{from_version} 迁移步骤已注册")

        def decorator(handler: MigrationHandler) -> MigrationHandler:
            self._steps[from_version] = ConfigMigrationStep(
                from_version=from_version,
                to_version=to_version,
                handler=handler,
            )
            return handler

        return decorator

    def plan(self, from_version: int, to_version: int | None = None) -> tuple[ConfigMigrationStep, ...]:
        target = self.current_version if to_version is None else to_version
        if from_version > target:
            raise ConfigMigrationError(f"配置不支持从 v{from_version} 降级到 v{target}")
        steps: list[ConfigMigrationStep] = []
        version = from_version
        while version < target:
            step = self._steps.get(version)
            if step is None:
                raise ConfigMigrationError(f"缺少配置 v{version} -> v{version + 1} 迁移步骤")
            if step.to_version > target:
                raise ConfigMigrationError(
                    f"配置 v{step.from_version} -> v{step.to_version} 超出目标 v{target}"
                )
            steps.append(step)
            version = step.to_version
        return tuple(steps)


@dataclass(slots=True)
class _ReportBuilder:
    from_version: int
    to_version: int
    changed: bool = False
    future_version: bool = False
    renamed_paths: list[PathRename] = field(default_factory=list)
    value_mappings: list[ValueMapping] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied_migrations: list[str] = field(default_factory=list)
    _seen_warnings: set[str] = field(default_factory=set)

    def mark_changed(self) -> None:
        self.changed = True

    def add_rename(self, old_path: str, new_path: str, status: PathStatus) -> None:
        self.renamed_paths.append(PathRename(old_path, new_path, status))
        self.mark_changed()

    def add_value_mapping(self, path: str, old_value: Any, new_value: Any) -> None:
        self.value_mappings.append(ValueMapping(path, old_value, new_value))
        self.mark_changed()

    def warn(self, message: str) -> None:
        if message in self._seen_warnings:
            return
        self._seen_warnings.add(message)
        self.warnings.append(message)

    def build(self) -> ConfigMigrationReport:
        return ConfigMigrationReport(
            changed=self.changed,
            from_version=self.from_version,
            to_version=self.to_version,
            renamed_paths=tuple(self.renamed_paths),
            warnings=tuple(self.warnings),
            value_mappings=tuple(self.value_mappings),
            applied_migrations=tuple(self.applied_migrations),
            future_version=self.future_version,
        )


CONFIG_MIGRATION_REGISTRY = ConfigMigrationRegistry(CURRENT_CONFIG_VERSION)


def normalize_legacy_config(raw: dict[str, Any]) -> tuple[dict[str, Any], ConfigMigrationReport]:
    """返回已规范化的配置副本和迁移报告，不原地修改输入。"""

    return migrate_config(raw)


def migrate_config(raw: dict[str, Any]) -> tuple[dict[str, Any], ConfigMigrationReport]:
    """按配置版本链迁移纯 dict 数据，不负责文件读写或 loader 接线。"""

    if not isinstance(raw, dict):
        raise TypeError("raw 必须是 dict")

    migrated = deepcopy(raw)
    from_version, version_warnings = _read_effective_version(migrated)
    if from_version > CURRENT_CONFIG_VERSION:
        builder = _ReportBuilder(
            from_version=from_version,
            to_version=from_version,
            future_version=True,
        )
        for warning in version_warnings:
            builder.warn(warning)
        builder.warn(
            f"配置版本 v{from_version} 高于当前支持版本 v{CURRENT_CONFIG_VERSION}，保留原版本并跳过迁移。"
        )
        return migrated, builder.build()

    builder = _ReportBuilder(
        from_version=from_version,
        to_version=CURRENT_CONFIG_VERSION,
    )
    for warning in version_warnings:
        builder.warn(warning)

    for step in CONFIG_MIGRATION_REGISTRY.plan(from_version, CURRENT_CONFIG_VERSION):
        step.handler(migrated, builder)
        builder.applied_migrations.append(step.migration_id)

    _normalize_phase_45_legacy_keys(migrated, builder)

    if migrated.get("version") != CURRENT_CONFIG_VERSION:
        migrated["version"] = CURRENT_CONFIG_VERSION
        builder.mark_changed()

    return migrated, builder.build()


def _read_effective_version(raw: dict[str, Any]) -> tuple[int, tuple[str, ...]]:
    version = raw.get("version", _MISSING)
    if version is _MISSING:
        return 1, ()
    if isinstance(version, bool) or not isinstance(version, int):
        return 1, (f"配置 version={version!r} 不是整数，按 v1 迁移。",)
    if version < 1:
        return 1, (f"配置 version={version!r} 小于 v1，按 v1 迁移。",)
    return version, ()


@CONFIG_MIGRATION_REGISTRY.register(1, 2)
def _migrate_v1_to_v2(config: dict[str, Any], report: _ReportBuilder) -> None:
    _ = config, report


def _normalize_phase_45_legacy_keys(config: dict[str, Any], report: _ReportBuilder) -> None:
    _rename_known_behavior_paths(config, report)
    _rename_provider_timeout(config, report)
    _rename_agent_first_token_timeout(config, report)
    _normalize_adapter_whitelist_mode(config, report)
    _normalize_provider_protocol(config, report)


def _rename_known_behavior_paths(config: dict[str, Any], report: _ReportBuilder) -> None:
    for old_path, new_path in (
        (("behavior", "merge_window"), ("behavior", "merge_window_seconds")),
        (("behavior", "recall_merge_window"), ("behavior", "recall_merge_window_seconds")),
        (("behavior", "greeting_interval"), ("behavior", "proactive_think_interval_seconds")),
        (
            ("behavior", "summarize", "chat_history_count"),
            ("behavior", "default_history_fetch_count"),
        ),
        (("behavior", "rate_limit", "window"), ("behavior", "rate_limit", "window_seconds")),
    ):
        _rename_path(config, old_path, new_path, report)
    _remove_deprecated_path(
        config,
        ("behavior", "typing", "max_delay"),
        "该字段已废弃，当前发送延迟由模型逐条填写 target.delay，不再有等价配置。",
        report,
    )


def _rename_provider_timeout(config: dict[str, Any], report: _ReportBuilder) -> None:
    for provider_id, provider in _iter_named_sections(config, ("providers",), report):
        _rename_child_key(
            provider,
            "timeout",
            "timeout_seconds",
            f"providers.{provider_id}.timeout",
            f"providers.{provider_id}.timeout_seconds",
            report,
        )


def _rename_agent_first_token_timeout(config: dict[str, Any], report: _ReportBuilder) -> None:
    for agent_id, agent in _iter_named_sections(config, ("agents",), report):
        _rename_child_key(
            agent,
            "first_token_timeout",
            "first_token_timeout_seconds",
            f"agents.{agent_id}.first_token_timeout",
            f"agents.{agent_id}.first_token_timeout_seconds",
            report,
        )


def _normalize_adapter_whitelist_mode(config: dict[str, Any], report: _ReportBuilder) -> None:
    for adapter_id, adapter in _iter_named_sections(config, ("adapters",), report):
        whitelist = adapter.get("whitelist")
        if whitelist is None:
            continue
        path = f"adapters.{adapter_id}.whitelist"
        if not isinstance(whitelist, dict):
            report.warn(f"{path} 不是对象，无法规范化白名单模式。")
            continue
        if whitelist.get("mode") == "all":
            whitelist["mode"] = "open"
            report.add_value_mapping(f"{path}.mode", "all", "open")


_CURRENT_PROTOCOLS = frozenset({"openai_compat", "anthropic"})
_LEGACY_OPENAI_COMPAT_PROTOCOLS = frozenset(
    {
        "ark",
        "baichuan",
        "deepseek",
        "doubao",
        "gemini",
        "glm",
        "groq",
        "minimax",
        "moonshot",
        "openai",
        "openrouter",
        "qwen",
        "siliconflow",
        "stepfun",
        "together",
        "volcengine",
        "xai",
        "yi",
        "zhipu",
    }
)


def _normalize_provider_protocol(config: dict[str, Any], report: _ReportBuilder) -> None:
    for provider_id, provider in _iter_named_sections(config, ("providers",), report):
        if "protocol" not in provider:
            continue
        protocol = provider["protocol"]
        path = f"providers.{provider_id}.protocol"
        if protocol is None or protocol in _CURRENT_PROTOCOLS:
            continue
        if not isinstance(protocol, str):
            report.warn(f"{path}={protocol!r} 不是字符串，无法规范化协议。")
            continue
        normalized = protocol.strip().lower()
        if normalized in _LEGACY_OPENAI_COMPAT_PROTOCOLS:
            provider["protocol"] = "openai_compat"
            report.add_value_mapping(path, protocol, "openai_compat")
            continue
        report.warn(f"{path}={protocol!r} 不是已知历史协议，已保留原值。")


def _rename_path(
    config: dict[str, Any],
    old_path: tuple[str, ...],
    new_path: tuple[str, ...],
    report: _ReportBuilder,
) -> None:
    old_parent = _get_parent_dict(config, old_path, report)
    if old_parent is None or old_path[-1] not in old_parent:
        return
    new_parent = _get_parent_dict(config, new_path, report)
    if new_parent is None:
        return
    _rename_child_key(
        old_parent,
        old_path[-1],
        new_path[-1],
        _format_path(old_path),
        _format_path(new_path),
        report,
        target_parent=new_parent,
    )


def _rename_child_key(
    source_parent: dict[str, Any],
    old_key: str,
    new_key: str,
    old_path: str,
    new_path: str,
    report: _ReportBuilder,
    *,
    target_parent: dict[str, Any] | None = None,
) -> None:
    if old_key not in source_parent:
        return
    target = source_parent if target_parent is None else target_parent
    value = source_parent[old_key]
    if new_key in target:
        source_parent.pop(old_key)
        report.add_rename(old_path, new_path, "conflict")
        return
    target[new_key] = value
    source_parent.pop(old_key)
    report.add_rename(old_path, new_path, "renamed")


def _remove_deprecated_path(
    config: dict[str, Any],
    path: tuple[str, ...],
    reason: str,
    report: _ReportBuilder,
) -> None:
    parent = _get_parent_dict(config, path, report)
    if parent is None or path[-1] not in parent:
        return
    parent.pop(path[-1])
    formatted_path = _format_path(path)
    report.add_rename(formatted_path, "", "removed")
    report.warn(f"{formatted_path} 已删除：{reason}")


def _iter_named_sections(
    config: dict[str, Any],
    path: tuple[str, ...],
    report: _ReportBuilder,
) -> Iterator[tuple[str, dict[str, Any]]]:
    section = _get_node_dict(config, path, report)
    if section is None:
        return
    for name, value in list(section.items()):
        item_path = f"{_format_path(path)}.{name}"
        if isinstance(value, dict):
            yield str(name), value
        elif value is not None:
            report.warn(f"{item_path} 不是对象，无法迁移该配置段。")


def _get_parent_dict(
    config: dict[str, Any],
    path: tuple[str, ...],
    report: _ReportBuilder,
) -> dict[str, Any] | None:
    if not path:
        return config
    parent_path = path[:-1]
    if not parent_path:
        return config
    return _get_node_dict(config, parent_path, report)


def _get_node_dict(
    config: dict[str, Any],
    path: tuple[str, ...],
    report: _ReportBuilder,
) -> dict[str, Any] | None:
    node: Any = config
    traversed: list[str] = []
    for part in path:
        if not isinstance(node, dict):
            prefix = _format_path(tuple(traversed)) or "<root>"
            report.warn(f"{prefix} 不是对象，无法处理 {_format_path(path)}。")
            return None
        if part not in node:
            return None
        node = node[part]
        traversed.append(part)
    if not isinstance(node, dict):
        report.warn(f"{_format_path(path)} 不是对象，无法处理该配置段。")
        return None
    return node


def _format_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


__all__ = [
    "CONFIG_MIGRATION_REGISTRY",
    "CURRENT_CONFIG_VERSION",
    "ConfigMigrationError",
    "ConfigMigrationReport",
    "ConfigMigrationRegistry",
    "PathRename",
    "ValueMapping",
    "migrate_config",
    "normalize_legacy_config",
]
