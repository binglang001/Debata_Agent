"""适配器注册中心 —— 管理所有已配置的适配器实例。

职责：
    - 从配置实例化适配器（NapCat / 未来 Discord / ...）
    - 统一收口所有适配器的事件，分发给业务层
    - 提供按名查找、批量启停

业务层用法：
    registry = AdapterRegistry()
    registry.on_event(my_handler)
    for adapter_name, adapter_cfg in config.adapters.items():
        adapter = build_adapter(adapter_name, adapter_cfg, secrets)
        registry.register(adapter)
    await registry.start_all()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .base import EventCallback, IAdapter
from .types import AnyEvent

logger = logging.getLogger(__name__)


# 适配器工厂签名：根据配置返回 IAdapter 实例
AdapterFactory = Callable[..., IAdapter]


class AdapterRegistry:
    """所有适配器实例的中心化管理器。"""

    def __init__(self) -> None:
        self._adapters: dict[str, IAdapter] = {}
        self._event_callback: EventCallback | None = None

    # ============================================================
    # 注册 / 查询
    # ============================================================

    def register(self, adapter: IAdapter) -> None:
        """注册一个适配器实例。重名会报错。"""
        if adapter.name in self._adapters:
            raise ValueError(f"适配器名重复: {adapter.name}")
        self._adapters[adapter.name] = adapter
        adapter.subscribe(self._on_adapter_event)
        logger.info(f"适配器已注册: {adapter.name} ({type(adapter).__name__})")

    def get(self, name: str) -> IAdapter:
        """按名取适配器。不存在则抛 KeyError。"""
        if name not in self._adapters:
            raise KeyError(f"未知适配器: {name}。已注册的: {list(self._adapters.keys())}")
        return self._adapters[name]

    def has(self, name: str) -> bool:
        return name in self._adapters

    def list_names(self) -> list[str]:
        return list(self._adapters.keys())

    def all(self) -> list[IAdapter]:
        return list(self._adapters.values())

    def default(self) -> IAdapter:
        """返回唯一的或名为 'default' 的适配器，便于单适配器场景使用。"""
        if not self._adapters:
            raise RuntimeError("尚未注册任何适配器")
        if "default" in self._adapters:
            return self._adapters["default"]
        if len(self._adapters) == 1:
            return next(iter(self._adapters.values()))
        raise RuntimeError(
            f"存在多个适配器（{list(self._adapters.keys())}），"
            f"必须指定 adapter name 或重命名一个为 'default'"
        )

    # ============================================================
    # 事件订阅
    # ============================================================

    def on_event(self, callback: EventCallback) -> None:
        """注册全局事件回调。所有适配器的事件都会经过这里。"""
        self._event_callback = callback

    async def _on_adapter_event(self, event: AnyEvent) -> None:
        """适配器投递事件 → 转发给业务层回调。"""
        if self._event_callback is None:
            logger.warning(f"丢弃事件（无业务回调）: {event.event_type} from {event.adapter}")
            return
        try:
            await self._event_callback(event)
        except Exception as e:
            logger.exception(f"业务回调处理事件失败: {e}")

    # ============================================================
    # 批量生命周期
    # ============================================================

    async def start_all(self) -> None:
        """并发启动所有适配器。"""
        if not self._adapters:
            logger.warning("没有适配器需要启动")
            return
        results = await asyncio.gather(
            *(a.start() for a in self._adapters.values()),
            return_exceptions=True,
        )
        for adapter, result in zip(self._adapters.values(), results, strict=True):
            if isinstance(result, BaseException):
                logger.error(f"适配器 {adapter.name} 启动失败: {result}")
        logger.info(f"已启动 {sum(1 for r in results if not isinstance(r, BaseException))} 个适配器")

    async def stop_all(self) -> None:
        """并发停止所有适配器。"""
        if not self._adapters:
            return
        await asyncio.gather(
            *(a.stop() for a in self._adapters.values()),
            return_exceptions=True,
        )
        logger.info(f"已停止 {len(self._adapters)} 个适配器")


# ============================================================
# 适配器类型注册（工厂）
# ============================================================


_factories: dict[str, AdapterFactory] = {}


def register_adapter_type(type_name: str, factory: AdapterFactory) -> None:
    """注册一个适配器工厂。配置中 type=<type_name> 时会调用该工厂。

    用法（在 adapters/napcat/__init__.py 中）：
        from adapters.registry import register_adapter_type
        from .napcat_adapter import NapCatAdapter
        register_adapter_type("napcat", NapCatAdapter.from_config)
    """
    if type_name in _factories:
        raise ValueError(f"适配器类型已注册: {type_name}")
    _factories[type_name] = factory
    logger.debug(f"适配器类型已注册: {type_name}")


def build_adapter(name: str, type_name: str, **kwargs) -> IAdapter:
    """按类型构建适配器实例。"""
    factory = _factories.get(type_name)
    if factory is None:
        raise ValueError(
            f"未知适配器类型: {type_name}。已注册的: {list(_factories.keys())}"
        )
    return factory(name=name, **kwargs)


def known_adapter_types() -> list[str]:
    return list(_factories.keys())
