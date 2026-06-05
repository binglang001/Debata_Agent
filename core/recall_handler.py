"""撤回事件处理 —— 合并窗口聚合 + 写入会话状态。

迁移自旧 handler._process_recall_batch。设计与旧版一致：
    - 撤回事件先暂存到 _pending 队列
    - 第一条触发时启动 _flush_task，等 RECALL_MERGE_WINDOW 秒
    - 到期按会话把撤回组装成 system_note 写入历史
    - 已在合并窗口内待处理的同 msg_id 消息会被移除
    - 撤回本身不单独触发 Agent；后续真实消息会带着该状态进入上下文
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from adapters.types import IncomingNotice, NoticeType
from app_config.schema import BehaviorConfig
from utils import get_time

from .message_pipeline import MessagePipeline

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _RecallEvent:
    conversation_id: str
    message_id: str
    note: str


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
        self._pending: list[_RecallEvent] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def on_notice(self, event: IncomingNotice) -> None:
        """接收 notice 事件，过滤出撤回类。"""
        if event.notice_type not in (NoticeType.GROUP_RECALL, NoticeType.FRIEND_RECALL):
            return

        recalled = self._describe(event)
        if recalled is None:
            return
        await self.pipeline.batch.remove_by_message_ids(
            {recalled.message_id},
            conversation_id=recalled.conversation_id,
        )
        self.pipeline._send_manager.notify_recall(
            recalled.conversation_id,
            message_id=recalled.message_id,
            note=recalled.note,
        )

        async with self._lock:
            self._pending.append(recalled)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        """等合并窗口结束后一次性处理。"""
        await asyncio.sleep(self.behavior_cfg.recall_merge_window_seconds)

        async with self._lock:
            notes = self._pending[:]
            self._pending.clear()

        if not notes:
            return

        grouped: dict[str, list[_RecallEvent]] = {}
        for item in notes:
            grouped.setdefault(item.conversation_id, []).append(item)
        logger.info(f"合并处理 {len(notes)} 条撤回")

        now = get_time()
        for conversation_id, items in grouped.items():
            lines = [f"{now} 撤回事件：当前会话最近有 {len(items)} 条消息被撤回。"]
            lines.extend(item.note for item in items)
            lines.append("这只是 QQ 可见状态记录，不是新的用户消息。")
            await self.pipeline.history.add_system_note(
                "\n".join(lines),
                conversation_id=conversation_id,
            )

    @staticmethod
    def _describe(event: IncomingNotice) -> _RecallEvent | None:
        """把 notice 事件渲染成一行说明。"""
        if not event.message_id:
            return None
        if event.notice_type == NoticeType.GROUP_RECALL:
            if not event.group_id:
                return None
            note = (
                f"群 {event.group_id} 中 QQ {event.user_id} "
                f"撤回了消息 msg_id={event.message_id}"
            )
            if event.operator_id and event.operator_id != event.user_id:
                note += f"（由 QQ {event.operator_id} 撤回）"
            return _RecallEvent(
                conversation_id=f"group:{event.group_id}",
                message_id=str(event.message_id),
                note=note,
            )
        if event.notice_type == NoticeType.FRIEND_RECALL:
            if not event.user_id:
                return None
            return _RecallEvent(
                conversation_id=f"private:{event.user_id}",
                message_id=str(event.message_id),
                note=f"私聊中 QQ {event.user_id} 撤回了消息 msg_id={event.message_id}",
            )
        return None

    async def shutdown(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"RecallHandler._flush_task 取消异常: {e}")
