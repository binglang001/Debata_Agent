"""Inbound enqueue and batch scheduling helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change whitelist, rate-limit, keyword-save, batching, or
user-record formatting while moving methods.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from adapters.types import IncomingMessage, Target
from tools import try_save_from_user
from utils import get_time

from .state import PendingMessageItem

logger = logging.getLogger(__name__)

# 速率超限时的提示模板（占位符运行时替换）
_RATE_LIMIT_REPLY_TEMPLATE = "已超出速率限制（{window_seconds} 秒内最多 {max_messages} 条），请添加机器人为好友后继续使用"


def _message_pipeline_global(name: str, fallback):
    module = sys.modules.get("core.message_pipeline")
    return getattr(module, name, fallback) if module is not None else fallback


class PipelineInboundMixin:
    async def enqueue(self, event: IncomingMessage) -> None:
        """接收一条入站消息，做前置检查后加入批处理队列。"""
        enqueue_t0 = time.monotonic()
        if not event.text and not event.media:
            # 空消息（无文本无媒体）忽略
            return
        self.mark_activity()

        # 白名单拦截（仅 mode=whitelist 时严格按名单过滤；open/verify 都放行）
        if self.whitelist.mode == "whitelist":
            if event.is_group():
                try:
                    gid = int(event.group_id or 0)
                except (TypeError, ValueError):
                    gid = 0
                if gid not in self._whitelist_group_ids:
                    logger.debug(
                        f"群消息被白名单拦截：group_id={event.group_id} 不在 {self._whitelist_group_ids}"
                    )
                    return
            else:
                try:
                    uid = int(event.user_id or 0)
                except (TypeError, ValueError):
                    uid = 0
                if uid not in self._whitelist_qq_ids:
                    logger.debug(
                        f"私聊消息被白名单拦截：user_id={event.user_id} 不在 {self._whitelist_qq_ids}"
                    )
                    return

        # 速率限制只针对私聊陌生人。群聊本身由群白名单/审核控制，不按群成员逐个限速。
        if (
            self.rate_limiter
            and not event.is_group()
            and await self.rate_limiter.check_and_log(event.user_id)
        ):
            await self._send_rate_limit_reply(event)
            return

        conversation_id = self._conversation_id_from_event(event)

        # 关键词强制保存（命中即写入重要记忆）
        keyword_saved = False
        if event.text:
            keyword_result = await try_save_from_user(
                event.text,
                self.important,
                enabled=self.features_cfg.long_term_memory.keyword_trigger_save,
                scope=conversation_id,
            )
            keyword_saved = bool(keyword_result and keyword_result.get("saved"))

        # 重建可读文本（CQ 码 + 媒体）
        text = await self._build_readable_text(event)
        self._inbound_seq += 1
        inbound_seq = self._inbound_seq
        received_at = time.monotonic()
        logger.debug(
            "入站消息 received_at_ms=%s conversation_id=%s msg_id=%s user_id=%s",
            int(time.time() * 1000),
            conversation_id,
            event.message_id,
            event.user_id,
        )
        self._self_id_by_conversation[conversation_id] = str(getattr(event, "self_id", "") or "")

        item = PendingMessageItem(
            message_id=event.message_id,
            user_id=event.user_id,
            nickname=event.nickname,
            location=f"群聊 {event.group_id}" if event.is_group() else "私聊",
            text=text,
            conversation_id=conversation_id,
            inbound_seq=inbound_seq,
            received_at=received_at,
            raw_event=event,
            keyword_saved=keyword_saved,
        )

        self.chat_timeline.append_inbound_event(
            event,
            conversation_id=conversation_id,
            text=text,
            timestamp=getattr(event, "timestamp", None),
        )
        await self.batch.append(item)
        self._send_manager.notify_inbound(item)
        logger.debug(
            "入站消息预处理完成 conversation_id=%s msg_id=%s text_len=%s keyword_saved=%s elapsed=%.3fs",
            conversation_id,
            event.message_id,
            len(text),
            keyword_saved,
            time.monotonic() - enqueue_t0,
        )
        if self._send_manager.should_defer_batch(conversation_id):
            return
        # 启动批处理任务（如未运行）
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_loop(event.source_target))

    @staticmethod
    def _conversation_id_from_event(event: IncomingMessage) -> str:
        """把平台事件映射成统一历史流里的会话标签。"""
        if event.group_id:
            return f"group:{event.group_id}"
        target = getattr(event, "source_target", None)
        if target is not None and getattr(target, "scope", None) == "private":
            return f"private:{target.target_id}"
        return f"private:{event.user_id}"

    async def _batch_loop(self, return_target: Target) -> None:
        """合并窗口循环：等待 → 取一批 → 处理 → 若被中断则重循环。

        return_target 用于"如果发送被打断，剩余 actions 失败的回执发去哪"等场景。
        """
        await asyncio.sleep(self.behavior_cfg.merge_window_seconds)

        while True:
            items = await self.batch.drain()
            if not items:
                break

            interrupted = False
            grouped: dict[str, list[PendingMessageItem]] = {}
            for item in items:
                grouped.setdefault(item.conversation_id, []).append(item)

            for group_items in grouped.values():
                try:
                    interrupted = await self._process_batch(group_items) or interrupted
                except Exception as e:
                    logger.exception(f"处理批次失败: {e}")
                    break

            if not interrupted:
                break

            # 被中断说明有新消息插入，再等一个窗口
            await asyncio.sleep(self.behavior_cfg.merge_window_seconds)

        # 退出前异步回查：处理期间可能有新消息正好入队
        async def _requeue_check():
            await asyncio.sleep(0)
            if not self.batch.is_empty_unsafe() and (
                self._batch_task is None or self._batch_task.done()
            ):
                self._batch_task = asyncio.create_task(self._batch_loop(return_target))

        # 保留引用，避免 task 在 await 跨边界时被 GC
        self._requeue_task = asyncio.create_task(_requeue_check())

    def _schedule_deferred_batch(self, conversation_id: str) -> None:
        """发送收尾竞态解除后，恢复处理此前被 defer 的入站消息。"""

        async def _start_if_pending() -> None:
            async with self.batch.lock:
                items = [
                    item
                    for item in await self.batch.peek_locked()
                    if item.conversation_id == conversation_id
                ]
            if not items:
                return
            if self._batch_task is not None and not self._batch_task.done():
                return
            self._batch_task = asyncio.create_task(
                self._batch_loop(items[-1].raw_event.source_target)
            )

        self._requeue_task = asyncio.create_task(_start_if_pending())

    def _build_user_record(
        self,
        items: list[PendingMessageItem],
        now: str | None = None,
    ) -> dict[str, Any]:
        """把同一会话的一批入站消息合成一条 user 历史记录。"""
        now = now or get_time()
        conversation_id = items[-1].conversation_id if items else "legacy:unknown"
        lines: list[str] = []
        meta_messages: list[dict[str, Any]] = []
        for item in items:
            lines.append(
                f"【{now} {item.location} {item.nickname}({item.user_id}) "
                f"msg_id={item.message_id}】{item.text}"
            )
            raw = item.raw_event
            target = getattr(raw, "source_target", None)
            scope = getattr(raw, "scope", None) or getattr(target, "scope", None)
            target_id = getattr(target, "target_id", "")
            group_id = getattr(raw, "group_id", None)
            meta_messages.append(
                {
                    "scope": scope or ("group" if group_id else "private"),
                    "target_id": target_id or group_id or item.user_id,
                    "group_id": group_id,
                    "user_id": item.user_id,
                    "nickname": item.nickname,
                    "message_id": item.message_id,
                    "timestamp": getattr(raw, "timestamp", None),
                    "location": item.location,
                    "text": item.text,
                    "inbound_seq": item.inbound_seq,
                    "received_at": item.received_at,
                }
            )
        return {
            "role": "user",
            "content": "\n".join(lines),
            "metadata": {"timestamp": now, "messages": meta_messages},
            "conversation_id": conversation_id,
        }

    async def _send_rate_limit_reply(self, event: IncomingMessage) -> None:
        """非好友超限时发一条限速提示（不入历史）。文案根据当前 rate_limit 配置渲染。"""
        rl = self.behavior_cfg.rate_limit
        template = _message_pipeline_global(
            "_RATE_LIMIT_REPLY_TEMPLATE",
            _RATE_LIMIT_REPLY_TEMPLATE,
        )
        text = template.format(
            window_seconds=rl.window_seconds, max_messages=rl.max_messages
        )
        try:
            msg_id = await self.adapter.send_text(event.source_target, text)
            if msg_id is not None:
                conversation_id = self._conversation_id_from_event(event)
                self._self_id_by_conversation[conversation_id] = str(
                    getattr(event, "self_id", "") or ""
                )
                self._record_successful_outbound(
                    {
                        "target_scope": event.source_target.scope,
                        "target_id": event.source_target.target_id,
                        "content": text,
                        "label": text,
                        "kind": "text",
                    },
                    conversation_id=conversation_id,
                    msg_id=str(msg_id),
                )
        except Exception:
            logger.debug("发送速率超限提示失败（adapter 可能未连接）")
