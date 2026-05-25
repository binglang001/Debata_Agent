"""插件机制核心抽象。

插件契约：
    plugins/{name}/
        __plugin__.py       —— 必须，导出 PLUGIN_META 与 build(...) 工厂
        adapter.py / impl/  —— 可选，具体实现

PluginManager 负责：
    - 启动时扫描 plugins/ 找出所有插件
    - 检测模型目录是否就绪（model_dir 是否存在 + 非空）
    - 按需 build() 实例（由 Runtime 在装配 features 时调用）
    - 提供 UI 查询接口（列出全部插件 + 状态 + 安装/启停 hook）

实际实现（Whisper / VoxCPM2 / sentence-transformers）由 DeepSeek 在 Phase 3 完成，
详见 docs/deepseek_tasks.md 任务 2/3/4。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PluginError(Exception):
    """插件加载或调用异常。"""


class PluginStatus(str, Enum):
    """插件当前状态。"""

    NOT_INSTALLED = "not_installed"
    """模型目录不存在 / 为空，需要先下载。"""

    INSTALLED = "installed"
    """模型目录就位，未启用。"""

    ENABLED = "enabled"
    """已 build 出实例并接入 Runtime。"""

    ERROR = "error"
    """加载失败（Python 依赖缺失 / 模型损坏 等）。"""


@dataclass(slots=True)
class PluginMeta:
    """插件元数据。每个 plugins/{name}/__plugin__.py 必须定义 `PLUGIN_META = PluginMeta(...)`。

    字段约定：
        name              —— 唯一标识，小写英文（whisper / voxcpm2 / bge-zh）
        display_name      —— UI 显示名
        kind              —— 'asr' | 'tts' | 'embedding'
        model_dir         —— 模型文件目录（绝对路径或相对 F:/.models 的子目录）
        size_mb           —— 模型大致体积（MB），UI 提示用
        description       —— 中文简述，给 UI 详情页用
        python_deps       —— 需要 pip install 的依赖列表（UI 提示用，不自动装）
        download_url      —— 模型下载页/教程链接（UI 跳转用）
        config_schema     —— 该插件支持的配置项（UI 详情页表单用）
                              结构：{key: {"type": str, "default": Any, "label": str, "help": str}}
                              支持 type: "string" | "int" | "float" | "bool" | "select"
                              select 类型再加 "options": [...]
        auto_download     —— UI 上按"下载"按钮时是否能自动拉模型；False 表示只能跳转教程让用户手动放
    """

    name: str
    display_name: str
    kind: str
    model_dir: str
    size_mb: int = 0
    description: str = ""
    python_deps: list[str] = field(default_factory=list)
    download_url: str = ""
    config_schema: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_download: bool = False


@dataclass(slots=True)
class PluginRecord:
    """运行时一条插件记录。"""

    meta: PluginMeta
    status: PluginStatus
    error: str = ""
    """status == ERROR 时填错误简述（UI 显示用）。"""
    instance: Any = None
    """status == ENABLED 时持有 build() 出来的服务实例。"""
    module_path: Path | None = None
    """__plugin__.py 实际位置。"""


# build 工厂函数签名：(config_dict) -> service_instance
PluginBuildFunc = Callable[[dict[str, Any]], Any]


class PluginManager:
    """插件扫描与加载中心。

    用法（Runtime 在装配 features 时调用）：
        pm = PluginManager(plugins_dir=Path("plugins"))
        pm.scan()
        # 按配置启用某个插件：
        if cfg.features.asr.enabled and cfg.features.asr.type == "local":
            asr_service = pm.build("whisper", cfg.features.asr.plugin_config)

    UI 用法：
        for record in pm.list_all():
            ...  # 渲染列表
    """

    def __init__(self, plugins_dir: Path) -> None:
        self.plugins_dir = plugins_dir
        self._records: dict[str, PluginRecord] = {}

    # ============================================================
    # 扫描
    # ============================================================

    def scan(self) -> None:
        """扫描 plugins/ 下所有 `__plugin__.py`，填充 _records。

        失败的插件记为 ERROR 状态，不影响其它插件。
        """
        self._records.clear()
        if not self.plugins_dir.exists():
            logger.debug(f"plugins 目录不存在：{self.plugins_dir}")
            return

        for plugin_pkg in sorted(self.plugins_dir.iterdir()):
            if not plugin_pkg.is_dir() or plugin_pkg.name.startswith("_"):
                continue
            plugin_file = plugin_pkg / "__plugin__.py"
            if not plugin_file.exists():
                continue
            try:
                meta = self._load_meta(plugin_file)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"插件 {plugin_pkg.name} 加载元数据失败：{e}")
                # 用目录名占位，状态 ERROR
                fallback = PluginMeta(
                    name=plugin_pkg.name,
                    display_name=plugin_pkg.name,
                    kind="unknown",
                    model_dir="",
                )
                self._records[plugin_pkg.name] = PluginRecord(
                    meta=fallback,
                    status=PluginStatus.ERROR,
                    error=f"元数据加载失败：{e}",
                    module_path=plugin_file,
                )
                continue

            status = self._detect_install_status(meta)
            self._records[meta.name] = PluginRecord(
                meta=meta,
                status=status,
                module_path=plugin_file,
            )

    def _load_meta(self, plugin_file: Path) -> PluginMeta:
        """从 __plugin__.py 加载 PLUGIN_META 常量。"""
        spec = importlib.util.spec_from_file_location(
            f"plugins._loaded_{plugin_file.parent.name}", plugin_file
        )
        if spec is None or spec.loader is None:
            raise PluginError(f"无法读取 {plugin_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        meta = getattr(module, "PLUGIN_META", None)
        if not isinstance(meta, PluginMeta):
            raise PluginError(f"{plugin_file} 未导出 PLUGIN_META = PluginMeta(...)")
        return meta

    def _detect_install_status(self, meta: PluginMeta) -> PluginStatus:
        """看 model_dir 是否就绪。"""
        if not meta.model_dir:
            return PluginStatus.INSTALLED  # 不需要模型文件的插件
        p = Path(meta.model_dir)
        if not p.is_absolute():
            # 相对路径 → 默认放 F:/.models（Windows）或 ~/.models
            p = Path("F:/.models") / p
        if not p.exists():
            return PluginStatus.NOT_INSTALLED
        # 非空（至少有一个文件）
        try:
            has_file = any(p.rglob("*"))
        except OSError:
            return PluginStatus.NOT_INSTALLED
        return PluginStatus.INSTALLED if has_file else PluginStatus.NOT_INSTALLED

    # ============================================================
    # build / 启用
    # ============================================================

    def build(self, name: str, config: dict[str, Any] | None = None) -> Any:
        """build 一个插件实例。

        会调用 plugins/{name}/__plugin__.py 的 `build(config)` 工厂函数。
        成功后 status 变 ENABLED 并缓存 instance。

        Raises:
            PluginError: 插件不存在 / 未安装 / build 失败。
        """
        record = self._records.get(name)
        if record is None:
            raise PluginError(f"插件 {name!r} 未找到")
        if record.status == PluginStatus.NOT_INSTALLED:
            raise PluginError(
                f"插件 {name!r} 模型目录未就绪：{record.meta.model_dir}"
            )
        if record.status == PluginStatus.ENABLED and record.instance is not None:
            return record.instance

        try:
            spec = importlib.util.spec_from_file_location(
                f"plugins._loaded_{name}", record.module_path  # type: ignore[arg-type]
            )
            if spec is None or spec.loader is None:
                raise PluginError(f"无法重新加载 {record.module_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            build_func: PluginBuildFunc | None = getattr(module, "build", None)
            if build_func is None:
                raise PluginError(f"插件 {name!r} 的 __plugin__.py 未定义 build(config)")
            instance = build_func(config or {})
        except Exception as e:  # noqa: BLE001
            record.status = PluginStatus.ERROR
            record.error = str(e)
            raise PluginError(f"插件 {name!r} build 失败：{e}") from e

        record.status = PluginStatus.ENABLED
        record.error = ""
        record.instance = instance
        logger.info(f"插件 {name!r} 已启用")
        return instance

    async def shutdown(self, name: str) -> None:
        """关闭一个已启用插件（如果它有 aclose）。"""
        record = self._records.get(name)
        if record is None or record.instance is None:
            return
        inst = record.instance
        aclose = getattr(inst, "aclose", None)
        if aclose is not None and callable(aclose):
            try:
                result = aclose()
                if hasattr(result, "__await__"):
                    await result
            except Exception as e:  # noqa: BLE001
                logger.warning(f"插件 {name!r} aclose 失败：{e}")
        record.instance = None
        record.status = PluginStatus.INSTALLED

    async def shutdown_all(self) -> None:
        """关闭所有已启用插件。"""
        for name, record in list(self._records.items()):
            if record.status == PluginStatus.ENABLED:
                await self.shutdown(name)

    # ============================================================
    # 查询
    # ============================================================

    def list_all(self) -> list[PluginRecord]:
        """列出全部已扫描插件（按 name 排序）。"""
        return [self._records[k] for k in sorted(self._records.keys())]

    def get(self, name: str) -> PluginRecord | None:
        return self._records.get(name)

    def by_kind(self, kind: str) -> list[PluginRecord]:
        """按类型筛（'asr' | 'tts' | 'embedding'）。"""
        return [r for r in self._records.values() if r.meta.kind == kind]
