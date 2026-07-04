"""启动期数据根初始化编排。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .data_migration import ensure_data_root_initialized
from .versioning import get_application_version

logger = logging.getLogger(__name__)


def initialize_runtime_data(
    paths: object,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """初始化运行时数据根，并执行阶段 0 旧 data 入根迁移。"""

    resolved_project_root: Path | None = None
    try:
        if project_root is not None:
            resolved_project_root = Path(project_root)
        else:
            resolved_project_root = Path(paths.PROJECT_ROOT)
        app_version = get_application_version(project_root=resolved_project_root)
        report = ensure_data_root_initialized(paths, app_version)
    except Exception:
        logger.exception(
            "阶段 0 数据根初始化失败：project_root=%s data_root=%s data_dir=%s config=%s",
            resolved_project_root,
            getattr(paths, "ROOT_DATA_DIR", getattr(paths, "data_root", None)),
            getattr(paths, "DATA_DIR", None),
            getattr(paths, "CONFIG_FILE", None),
        )
        raise

    if report.get("migrated"):
        logger.info(
            "阶段 0 数据根初始化完成：%s source=%s target=%s backup=%s",
            report.get("message"),
            report.get("source_data_dir"),
            report.get("target_instance_dir"),
            report.get("backup_path"),
        )
    else:
        logger.debug(
            "阶段 0 数据根初始化跳过：%s target=%s",
            report.get("message"),
            report.get("target_instance_dir"),
        )
    return report
