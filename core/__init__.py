"""Debata_Agent 核心运行时。

替代旧 NoneBot2 + handler.py 的整套机制。每个模块职责单一，依赖通过构造器注入。

模块构成（按消息流向）：
    event_bus           —— 接收 IAdapter 上报的事件，按类型分发
    message_pipeline    —— 消息合并/批处理/调用 ChatAgent/即时发送
    recall_handler      —— 撤回事件合并 + 触发 Agent 重新评估
    request_handler     —— 好友/群请求的暂存 + Agent 决策
    proactive_loop      —— 主动思考定时循环
    wakeup              —— schedule_wakeup 工具的任务调度中心
    runtime             —— 全局生命周期管理（启动/停止）+ 依赖装配

每个模块都遵循以下原则：
    1. 不直接依赖 NoneBot / NapCat / 任何具体平台——只通过 IAdapter / IProvider 等接口
    2. 状态封装在类里，不写全局变量
    3. 可被单元测试（fake adapter / fake provider 注入即可）
"""

from .event_bus import EventBus
from .message_pipeline import MessagePipeline
from .proactive_loop import ProactiveLoop
from .recall_handler import RecallHandler
from .request_handler import RequestHandler
from .runtime import Runtime
from .state import (
    MessageBatch,
    PendingMessageItem,
    PendingRequestInfo,
    PendingRequestStore,
    RateLimiter,
)
from .wakeup import WakeupScheduler

__all__ = [
    "EventBus",
    "MessagePipeline",
    "ProactiveLoop",
    "RecallHandler",
    "RequestHandler",
    "Runtime",
    "MessageBatch",
    "PendingMessageItem",
    "PendingRequestInfo",
    "PendingRequestStore",
    "RateLimiter",
    "WakeupScheduler",
]
