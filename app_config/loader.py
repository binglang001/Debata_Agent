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
from typing import Any

import orjson
import yaml
from pydantic import ValidationError

from .paths import AppPaths
from .schema import RootConfig

logger = logging.getLogger(__name__)

_active_config: RootConfig | None = None


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
        with open(paths.CONFIG_FILE, encoding="utf-8") as f:
            raw: Any = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败: {paths.CONFIG_FILE}\n{e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点应为字典，得到 {type(raw).__name__}")

    try:
        cfg = RootConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_user_error_text(e)) from e

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
        backup: 是否在覆盖前备份当前配置到 config.yaml.bak。
    """
    paths.ensure_data_dirs()

    if backup and paths.CONFIG_FILE.exists():
        backup_path = paths.CONFIG_FILE.with_suffix(".yaml.bak")
        shutil.copy2(paths.CONFIG_FILE, backup_path)

    # 通过 JSON 中转获得干净的可序列化结构（剔除 None、保留枚举字符串值）
    data = orjson.loads(cfg.model_dump_json(exclude_none=True))

    tmp_path = paths.CONFIG_FILE.with_suffix(".yaml.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            indent=2,
        )
    tmp_path.replace(paths.CONFIG_FILE)
    logger.info(f"配置已保存: {paths.CONFIG_FILE}")


def set_active_config(cfg: RootConfig) -> None:
    """供测试或迁移场景设置全局单例。"""
    global _active_config
    _active_config = cfg
