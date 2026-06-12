"""定时唤醒调度中心。

承担 schedule_wakeup 工具的执行：当 LLM 调用该工具时，注册一个延时任务，
到时按任务模式直接发送消息，或唤醒 ChatAgent 并把"提醒内容"传给它。

设计：
    - 用 asyncio.Task 实现，重启程序后任务丢失（与旧版一致；持久化留到 P2）
    - 提供 callback 闭包给 ToolContext.wakeup_cb，工具调用时塞任务
    - 真正触发时调用 MessagePipeline.run_wakeup_turn()，由它按模式处理
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


# 唤醒到时的回调签名（由 MessagePipeline 提供）
# reminder: 唤醒时的任务说明；mode=send_message 时不会传给 LLM
WakeFireCallback = Callable[
    [str, dict[str, Any] | None, str, str | None],
    Awaitable[None],
]


class WakeupScheduler:
    """延时唤醒调度器。

    使用方式：
        scheduler = WakeupScheduler(on_fire=pipeline.run_wakeup_turn)
        # 在 ToolContext 注入：
        ctx = ToolContext(wakeup_cb=scheduler.schedule, ...)
        # 关闭时清理：
        await scheduler.cancel_all()
    """

    def __init__(self, on_fire: WakeFireCallback) -> None:
        self._on_fire = on_fire
        self._tasks: set[asyncio.Task] = set()

    async def schedule(
        self,
        delay_seconds: int,
        reminder: str,
        target: dict[str, Any] | None = None,
        mode: str = "wakeup",
        message_text: str | None = None,
    ) -> None:
        """注册一个延时唤醒任务。

        与 ToolContext.wakeup_cb 签名兼容。
        """
        if delay_seconds <= 0:
            logger.warning(
                f"schedule_wakeup 收到非法 delay_seconds={delay_seconds}，跳过"
            )
            return

        async def _runner():
            try:
                await asyncio.sleep(delay_seconds)
                logger.info(f"定时唤醒触发: {reminder!r}")
                await self._on_fire(reminder, target, mode, message_text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"定时唤醒执行失败: {e}")

        task = asyncio.create_task(_runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(f"已注册定时任务 +{delay_seconds}s mode={mode}: {reminder!r}")

    def pending_count(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    async def cancel_all(self) -> None:
        """关闭所有未触发的任务（用于 shutdown）。"""
        tasks = list(self._tasks)
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"WakeupScheduler 取消任务异常: {e}")
        self._tasks.clear()
        logger.info(f"WakeupScheduler 已取消 {len(tasks)} 个待触发任务")
