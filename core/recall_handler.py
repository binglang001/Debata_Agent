"""撤回事件处理 —— 合并窗口聚合 + 触发 Agent 重新评估。

迁移自旧 handler._process_recall_batch。设计与旧版一致：
    - 撤回事件先暂存到 _pending 队列
    - 第一条触发时启动 _flush_task，等 RECALL_MERGE_WINDOW 秒
    - 到期一次性把所有撤回组装成 system_note 写入历史
    - 然后调 Agent 让它决定是否反应
"""

from __future__ import annotations

import asyncio
import logging

from adapters.types import IncomingNotice, NoticeType
from app_config.schema import BehaviorConfig
from utils import get_time

from .message_pipeline import MessagePipeline

logger = logging.getLogger(__name__)


class RecallHandler:
    """撤回事件合并处理器。

    用法：
        recall = RecallHandler(pipeline=pipeline, behavior_cfg=cfg.behavior)
        event_bus.on_notice(recall.on_notice)
    """

    def __init__(
        self,
        *,
        pipeline: MessagePipeline,
        behavior_cfg: BehaviorConfig,
    ) -> None:
        self.pipeline = pipeline
        self.behavior_cfg = behavior_cfg
        self._pending: list[str] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def on_notice(self, event: IncomingNotice) -> None:
        """接收 notice 事件，过滤出撤回类。"""
        if event.notice_type not in (NoticeType.GROUP_RECALL, NoticeType.FRIEND_RECALL):
            return

        note = self._describe(event)
        if not note:
            return

        async with self._lock:
            self._pending.append(note)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        """等合并窗口结束后一次性处理。"""
        await asyncio.sleep(self.behavior_cfg.recall_merge_window)

        async with self._lock:
            notes = self._pending[:]
            self._pending.clear()

        if not notes:
            return

        combined = "\n".join(notes)
        logger.info(f"合并处理 {len(notes)} 条撤回")

        # 走 pipeline.run_one_turn 让 Agent 决定是否反应
        # 通常规则里写明"撤回一般不需要回复"，让 Agent 自己用 no_action
        now = get_time()
        task_context = (
            f"现在是{now}。最近有 {len(notes)} 条消息被撤回。"
            f"根据规则，通常不需要回复撤回。"
        )
        await self.pipeline.run_one_turn(
            task_context,
            as_system_note=combined,
        )

    @staticmethod
    def _describe(event: IncomingNotice) -> str:
        """把 notice 事件渲染成一行说明。"""
        if event.notice_type == NoticeType.GROUP_RECALL:
            note = (
                f"群 {event.group_id} 中 QQ {event.user_id} "
                f"撤回了消息 msg_id={event.message_id}"
            )
            if event.operator_id and event.operator_id != event.user_id:
                note += f"（由 QQ {event.operator_id} 撤回）"
            return note
        if event.notice_type == NoticeType.FRIEND_RECALL:
            return f"私聊中 QQ {event.user_id} 撤回了消息 msg_id={event.message_id}"
        return ""

    async def shutdown(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
