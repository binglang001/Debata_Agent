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
import heapq
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# 唤醒到时的回调签名（由 MessagePipeline 提供）
# reminder: 唤醒时的任务说明；mode=send_message 时不会传给 LLM
WakeFireCallback = Callable[
    [str, dict[str, Any] | None, str, str | None],
    Awaitable[None],
]


@dataclass(order=True)
class _ScheduledWakeup:
    due_at: float
    seq: int
    reminder: str = field(compare=False)
    target: dict[str, Any] | None = field(compare=False)
    mode: str = field(compare=False)
    message_text: str | None = field(compare=False)


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
        self._queue: list[_ScheduledWakeup] = []
        self._seq = itertools.count()
        self._runner_task: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()
        self._closed = False

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

        if self._closed:
            logger.warning("WakeupScheduler 已关闭，跳过新增定时任务")
            return
        item = _ScheduledWakeup(
            due_at=time.monotonic() + delay_seconds,
            seq=next(self._seq),
            reminder=reminder,
            target=target,
            mode=mode,
            message_text=message_text,
        )
        heapq.heappush(self._queue, item)
        self._ensure_runner()
        self._changed.set()
        logger.info(f"已注册定时任务 +{delay_seconds}s mode={mode}: {reminder!r}")

    def pending_count(self) -> int:
        return len(self._queue)

    async def cancel_all(self) -> None:
        """关闭所有未触发的任务（用于 shutdown）。"""
        count = len(self._queue)
        self._queue.clear()
        self._closed = True
        self._changed.set()
        task = self._runner_task
        self._runner_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"WakeupScheduler 取消任务异常: {e}")
        logger.info(f"WakeupScheduler 已取消 {count} 个待触发任务")

    def _ensure_runner(self) -> None:
        if self._runner_task is None or self._runner_task.done():
            self._runner_task = asyncio.create_task(self._runner(), name="wakeup-scheduler")

    async def _runner(self) -> None:
        while not self._closed:
            if not self._queue:
                self._changed.clear()
                await self._changed.wait()
                continue

            next_item = self._queue[0]
            delay = max(0.0, next_item.due_at - time.monotonic())
            if delay > 0:
                self._changed.clear()
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=delay)
                    continue
                except asyncio.TimeoutError:
                    pass

            now = time.monotonic()
            due: list[_ScheduledWakeup] = []
            while self._queue and self._queue[0].due_at <= now:
                due.append(heapq.heappop(self._queue))
            for item in due:
                try:
                    logger.info(f"定时唤醒触发: {item.reminder!r}")
                    await self._on_fire(
                        item.reminder,
                        item.target,
                        item.mode,
                        item.message_text,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception(f"定时唤醒执行失败: {e}")
