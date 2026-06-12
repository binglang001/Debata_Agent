"""Inbound enqueue and batch scheduling helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change whitelist, rate-limit, batching, or user-record
formatting while moving methods.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from adapters.types import IncomingMessage, Target
from utils import get_time

from .state import PendingMessageItem

logger = logging.getLogger(__name__)

# 速率超限时的提示模板（占位符运行时替换）
_RATE_LIMIT_REPLY_TEMPLATE = "已超出速率限制（{window_seconds} 秒内最多 {max_messages} 条），请添加机器人为好友后继续使用"
_SAFE_PAYLOAD_MAX_STRING_LENGTH = 2000
_SAFE_PAYLOAD_MAX_DEPTH = 3
_SAFE_PAYLOAD_MAX_ITEMS = 30


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
        )

        self.chat_timeline.append_inbound_event(
            event,
            conversation_id=conversation_id,
            text=text,
            timestamp=getattr(event, "timestamp", None),
        )
        await self._append_qq_message_received_event(
            event,
            conversation_id=conversation_id,
            text=text,
            received_at=received_at,
        )
        await self.batch.append(item)
        self._send_manager.notify_inbound(item)
        logger.debug(
            "入站消息预处理完成 conversation_id=%s msg_id=%s text_len=%s elapsed=%.3fs",
            conversation_id,
            event.message_id,
            len(text),
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

    async def _append_qq_message_received_event(
        self,
        event: IncomingMessage,
        *,
        conversation_id: str,
        text: str,
        received_at: float,
    ) -> None:
        """把 QQ 可见入站消息追加到磁盘事件库。"""
        event_store = getattr(self, "event_store", None) or getattr(self, "_event_store", None)
        if event_store is None:
            return

        target = getattr(event, "source_target", None)
        source = _optional_text(getattr(event, "adapter", None)) or _optional_text(
            getattr(target, "adapter", None)
        )
        external_id = _optional_text(getattr(event, "message_id", None))
        target_id = (
            _optional_text(getattr(target, "target_id", None))
            or _optional_text(getattr(event, "group_id", None))
            or _optional_text(getattr(event, "user_id", None))
        )
        timestamp_unix = _incoming_timestamp_unix(event, received_at)
        payload: dict[str, Any] = {
            "direction": "inbound",
            "conversation_id": conversation_id,
            "source": source,
            "msg_id": external_id,
            "message_id": external_id,
            "user_id": _optional_text(getattr(event, "user_id", None)),
            "nickname": _optional_text(getattr(event, "nickname", None)),
            "sender_name": _optional_text(getattr(event, "nickname", None))
            or _optional_text(getattr(event, "user_id", None)),
            "group_id": _optional_text(getattr(event, "group_id", None)),
            "target_id": target_id,
            "target_scope": _optional_text(getattr(target, "scope", None))
            or _optional_text(getattr(event, "scope", None)),
            "text": text,
            "content": text,
            "timestamp_unix": timestamp_unix,
            "received_at": received_at,
            "self_id": _optional_text(getattr(event, "self_id", None)),
            "reply_to": _optional_text(getattr(event, "reply_to", None)),
            "raw_event": _incoming_raw_event_payload(event),
        }
        media_payload = _incoming_media_payload(event)
        if media_payload:
            payload["media"] = media_payload

        idempotency_key = None
        if external_id:
            idempotency_key = f"qq_message_received:{source or ''}:{conversation_id}:{external_id}"

        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        write_started_at = time.perf_counter() if debug_enabled else 0.0
        await event_store.append_event(
            event_type="qq_message_received",
            conversation_id=conversation_id,
            source=source,
            external_id=external_id,
            idempotency_key=idempotency_key,
            timestamp_unix=timestamp_unix,
            payload=payload,
        )
        if debug_enabled:
            logger.debug(
                "QQ 入站消息 EventStore 写入指标 conversation_id=%s msg_id=%s "
                "elapsed_ms=%.3f",
                conversation_id,
                external_id,
                (time.perf_counter() - write_started_at) * 1000,
            )

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
        except Exception:
            logger.debug("发送速率超限提示失败（adapter 可能未连接）")
            return
        if msg_id is None:
            return
        conversation_id = self._conversation_id_from_event(event)
        self._self_id_by_conversation[conversation_id] = str(
            getattr(event, "self_id", "") or ""
        )
        await self._record_successful_outbound(
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


def _incoming_timestamp_unix(event: IncomingMessage, received_at: float) -> float:
    timestamp = _positive_float_or_none(getattr(event, "timestamp", None))
    if timestamp is not None:
        return timestamp
    received = _positive_float_or_none(received_at)
    if received is not None and received > 1_000_000_000:
        return received
    return time.time()


def _incoming_raw_event_payload(event: IncomingMessage) -> dict[str, Any]:
    raw_event: dict[str, Any] = {
        "adapter": _optional_text(getattr(event, "adapter", None)),
        "event_type": _enum_or_text(getattr(event, "event_type", None)),
        "timestamp": _positive_float_or_none(getattr(event, "timestamp", None)),
        "self_id": _optional_text(getattr(event, "self_id", None)),
        "message_id": _optional_text(getattr(event, "message_id", None)),
        "scope": _optional_text(getattr(event, "scope", None)),
        "user_id": _optional_text(getattr(event, "user_id", None)),
        "nickname": _optional_text(getattr(event, "nickname", None)),
        "group_id": _optional_text(getattr(event, "group_id", None)),
        "text": _optional_text(getattr(event, "text", None)),
        "raw_message": _optional_text(getattr(event, "raw_message", None)),
        "reply_to": _optional_text(getattr(event, "reply_to", None)),
    }
    raw = getattr(event, "raw", None)
    if isinstance(raw, dict) and raw:
        raw_event["raw"] = _safe_basic_payload(raw)
    return raw_event


def _incoming_media_payload(event: IncomingMessage) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for segment in list(getattr(event, "media", []) or [])[:20]:
        item: dict[str, Any] = {
            "type": _enum_or_text(getattr(segment, "type", None)),
            "file_id": _optional_text(getattr(segment, "file_id", None)),
            "url": _optional_text(getattr(segment, "url", None)),
            "name": _optional_text(getattr(segment, "name", None)),
        }
        extra = getattr(segment, "extra", None)
        if isinstance(extra, dict) and extra:
            item["extra"] = _safe_basic_payload(extra)
        payload.append({key: value for key, value in item.items() if value is not None})
    return payload


def _safe_basic_payload(value: Any, *, depth: int = 0) -> Any:
    """限制 raw 中的基础字段规模，避免把平台原始大对象整包塞进事件库。"""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _truncate_safe_payload_text(value)
    if depth >= _SAFE_PAYLOAD_MAX_DEPTH:
        return _safe_payload_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= _SAFE_PAYLOAD_MAX_ITEMS:
                break
            result[_safe_payload_key(key)] = _safe_basic_payload(child, depth=depth + 1)
        return result
    if isinstance(value, list | tuple):
        return [
            _safe_basic_payload(child, depth=depth + 1)
            for child in value[:_SAFE_PAYLOAD_MAX_ITEMS]
        ]
    return _safe_payload_text(value)


def _truncate_safe_payload_text(text: str) -> str:
    if len(text) <= _SAFE_PAYLOAD_MAX_STRING_LENGTH:
        return text
    return text[:_SAFE_PAYLOAD_MAX_STRING_LENGTH]


def _safe_payload_text(value: Any) -> str | None:
    if value is None:
        return None
    return _truncate_safe_payload_text(str(value).strip()) or None


def _safe_payload_key(value: Any) -> str:
    return _truncate_safe_payload_text(str(value))


def _positive_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _enum_or_text(value: Any) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        value = enum_value
    return _optional_text(value)
