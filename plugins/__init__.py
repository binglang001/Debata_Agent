"""插件系统 —— 给 Debata 接本地模型 / 重资源依赖的可选能力。

设计目标：
    - 主程序只依赖 features/ 的轻量接口（ITTSService / IEmbeddingService）
    - 插件是「实现 + 重依赖（torch、CUDA、模型文件）」的组合
    - 用户可选装/启停，不影响主程序最小依赖
    - 模型文件统一存 data/models/{name}/，不入仓库

详见 plugins/PLUGIN_SPEC.md。
"""

from __future__ import annotations

from .base import (
    DownloadProgressCallback,
    DownloadSource,
    PluginError,
    PluginManager,
    PluginMeta,
    PluginRecord,
    PluginStatus,
)

__all__ = [
    "DownloadProgressCallback",
    "DownloadSource",
    "PluginError",
    "PluginManager",
    "PluginMeta",
    "PluginRecord",
    "PluginStatus",
]
