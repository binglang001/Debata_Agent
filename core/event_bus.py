"""事件总线 —— 把 IAdapter 上报的事件分发到对应 handler。

替代旧 NoneBot 的 on_message / on_notice / on_request 装饰器机制。

设计：
    - EventBus 是个简单的多路复用器：订阅 adapter.subscribe → 收到 event → 按类型分发
    - handler 是 async 函数，签名 `async (event) -> None`
    - 异常会被捕获并记录，不让单个 handler 失败拖垮整个总线
    - 多个 handler 顺序触发（不是并发），避免 race condition；
      并发处理留给 handler 内部决定（如 MessagePipeline.enqueue 是 nonblocking 的）
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from adapters.base import IAdapter
from adapters.types import (
    AnyEvent,
    EventType,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MetaEvent,
)

logger = logging.getLogger(__name__)


# 各类事件回调签名
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
"""消息事件回调：收到消息时触发。"""
NoticeHandler = Callable[[IncomingNotice], Awaitable[None]]
"""通知事件回调：撤回/戳一戳等。"""
RequestHandler = Callable[[IncomingRequest], Awaitable[None]]
"""请求事件回调：好友/群申请。"""
MetaHandler = Callable[[MetaEvent], Awaitable[None]]
"""元事件回调：心跳/生命周期。"""


class EventBus:
    """事件总线。

    用法：
        bus = EventBus()
        bus.on_message(pipeline.enqueue)
        bus.on_notice(recall_handler.on_notice)
        bus.on_request(request_handler.on_request)
        adapter.subscribe(bus.dispatch)
        await adapter.start()
    """

    def __init__(self) -> None:
        self._message_handlers: list[MessageHandler] = []
        self._notice_handlers: list[NoticeHandler] = []
        self._request_handlers: list[RequestHandler] = []
        self._meta_handlers: list[MetaHandler] = []

    # ============================================================
    # 订阅 API
    # ============================================================

    def on_message(self, handler: MessageHandler) -> None:
        """注册消息事件回调。"""
        self._message_handlers.append(handler)

    def on_notice(self, handler: NoticeHandler) -> None:
        """注册通知事件回调（撤回/戳一戳等）。"""
        self._notice_handlers.append(handler)

    def on_request(self, handler: RequestHandler) -> None:
        """注册请求事件回调（好友/群申请）。"""
        self._request_handlers.append(handler)

    def on_meta(self, handler: MetaHandler) -> None:
        """注册元事件回调（心跳/生命周期）。"""
        self._meta_handlers.append(handler)

    # ============================================================
    # 分发
    # ============================================================

    async def dispatch(self, event: AnyEvent) -> None:
        """单一入口：按 event_type 分发到对应 handler 列表。"""
        try:
            if event.event_type == EventType.MESSAGE and isinstance(event, IncomingMessage):
                await self._fire(self._message_handlers, event, "message")
            elif event.event_type == EventType.NOTICE and isinstance(event, IncomingNotice):
                await self._fire(self._notice_handlers, event, "notice")
            elif event.event_type == EventType.REQUEST and isinstance(event, IncomingRequest):
                await self._fire(self._request_handlers, event, "request")
            elif event.event_type == EventType.META and isinstance(event, MetaEvent):
                await self._fire(self._meta_handlers, event, "meta")
            else:
                logger.debug(f"未知事件类型，忽略: {event!r}")
        except Exception as e:
            logger.exception(f"EventBus 分发失败: {e}")

    @staticmethod
    async def _fire(handlers: list, event: AnyEvent, kind: str) -> None:
        """顺序触发所有 handler，单个失败不影响其它。"""
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.exception(
                    f"{kind} handler {handler.__name__} 抛出异常: {e}"
                )

    # ============================================================
    # 适配 IAdapter.subscribe（便利方法）
    # ============================================================

    def bind_adapter(self, adapter: IAdapter) -> None:
        """把 dispatch 注册给适配器。"""
        adapter.subscribe(self.dispatch)
        logger.info(f"EventBus 绑定到适配器 {adapter.name}")
