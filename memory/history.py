"""对话历史管理 —— 每个人格独立的会话上下文。

历史记录格式（OpenAI 兼容）：
    {"role": "user|assistant|system|tool", "content": "...", "tool_calls": [...], ...}

存储为 JSONL（每行一条 dict），便于增量追加。

订阅机制：
    history.on_append(callback)  # callback: async (records: list[dict]) -> None
    每次有新记录写入时，所有订阅者都会被并发通知（不阻塞主流程）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .store import JsonlStore

logger = logging.getLogger(__name__)


def _on_notify_done(task: asyncio.Task) -> None:
    """记录 on_append 通知任务中的异常。"""
    try:
        exc = task.exception()
        if exc is not None:
            logger.warning(f"history.on_append 回调异常: {exc}")
    except Exception:
        pass


AppendCallback = Callable[[list[dict]], Awaitable[None]]

HISTORY_RECORD_EVENT_TYPE = "history_record_appended"
HISTORY_EVENT_PAYLOAD_SOURCE = "history_jsonl"
HISTORY_JOURNAL_SCHEMA_VERSION = 1


class HistoryManager:
    """对话历史管理器。一个 persona 对应一个实例。"""

    def __init__(self, history_path: Path, event_store: Any = None) -> None:
        self._store = JsonlStore(history_path)
        self._event_store = event_store
        self._mutation_lock = asyncio.Lock()
        self._append_callbacks: list[AppendCallback] = []
        # 保留 notify task 引用避免 GC（订阅者执行完后自动 discard）
        self._notify_tasks: set[asyncio.Task] = set()

    def on_append(self, callback: AppendCallback) -> None:
        """订阅新记录写入事件。用于 RAG 等下游模块异步消费。"""
        self._append_callbacks.append(callback)

    async def _notify(self, records: list[dict]) -> None:
        """并发通知所有订阅者；不阻塞主流程，错误隔离。"""
        if not self._append_callbacks:
            return
        for cb in self._append_callbacks:
            try:
                task = asyncio.create_task(cb(records))
            except Exception as e:
                logger.exception(f"创建 history.on_append 通知任务失败: {e}")
                continue
            task.add_done_callback(_on_notify_done)
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

    async def _append_records_and_mirror(self, records: list[dict]) -> None:
        """先写完整 JSONL，再把完整记录镜像到事件库。"""
        if not records:
            return
        history_start = await self._store.length()
        await self._store.append_many(records)
        if self._event_store is None:
            return

        try:
            await self._event_store.append_events(
                [
                    _history_record_event(
                        record,
                        history_index=history_start + batch_index,
                        batch_index=batch_index,
                    )
                    for batch_index, record in enumerate(records)
                ]
            )
        except Exception as e:
            logger.warning(
                f"history EventStore 镜像失败，已保留完整 JSONL: {e}",
                exc_info=True,
            )

    async def _mirror_truncated(self, cut_point: int, remaining: int) -> None:
        """把 JSONL 截断动作镜像到事件库，失败不影响历史旧语义。"""
        if self._event_store is None or cut_point <= 0:
            return
        try:
            await self._event_store.append_event(
                event_type="history_truncated",
                payload={
                    "cut_point": cut_point,
                    "remaining_count": remaining,
                },
            )
        except Exception as e:
            logger.warning(f"history EventStore 截断镜像失败: {e}", exc_info=True)

    async def _record_system_note_event(self, record: dict) -> None:
        """记录 system note 专用事件，失败不影响 JSONL 历史。"""
        if self._event_store is None:
            return
        try:
            history_length = await self._store.length()
            history_index = max(0, history_length - 1)
            content = str(record.get("content") or "")
            content_length, content_hash = _content_fingerprint(content)
            conversation_id = _history_record_conversation_id(record)
            await self._event_store.append_event(
                event_type="system_note_recorded",
                conversation_id=conversation_id,
                payload={
                    "role": "system",
                    "history_index": history_index,
                    "history_offset": history_index,
                    "conversation_id": conversation_id,
                    "content": content,
                    "record": record,
                    "content_hash": content_hash,
                    "content_length": content_length,
                    "record_keys": [str(key) for key in record.keys()],
                    "source": "history_jsonl",
                },
            )
        except Exception as e:
            logger.warning(f"history system note 事件记录失败: {e}", exc_info=True)

    async def load(self, force_reload: bool = False) -> list[dict]:
        """加载所有历史。"""
        return await self._store.load(force_reload=force_reload)

    async def records(self) -> list[dict]:
        """获取当前所有历史记录（缓存命中时不访问磁盘）。

        语义上等价于 load() —— load() 名字暗示"从磁盘加载"，
        records() 更适合"取当前完整记录"这种调用场景，
        在 message_pipeline 拼装 messages 时用 records() 表达意图更清晰。
        """
        started_at = time.perf_counter()
        records = await self._store.load()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "HistoryManager records 指标 returned_records=%d elapsed_ms=%.3f",
                len(records),
                (time.perf_counter() - started_at) * 1000,
            )
        return records

    async def records_for_conversation(
        self,
        conversation_id: str | None,
        *,
        include_legacy: bool = True,
    ) -> list[dict]:
        """按 conversation_id 取活跃历史记录。

        历史仍是统一流；这里只供 recall/summarize/呈现层按标签筛选，
        不用于构建模型工作窗口。
        """
        records = await self.records()
        if not conversation_id:
            return records
        result: list[dict] = []
        for record in records:
            rid = record.get("conversation_id") or _infer_conversation_id(record)
            if rid == conversation_id:
                result.append(record)
            elif include_legacy and not rid:
                result.append(record)
        return result

    async def add_user_message(
        self,
        content: str,
        metadata: dict | None = None,
        conversation_id: str | None = None,
    ) -> None:
        record = {"role": "user", "content": content}
        if metadata:
            record["metadata"] = metadata
        if conversation_id:
            record["conversation_id"] = conversation_id
        async with self._mutation_lock:
            await self._append_records_and_mirror([record])
            await self._notify([record])

    async def add_assistant_message(
        self,
        content: str,
        tool_calls: list[dict] | None = None,
        reasoning_content: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        record: dict = {"role": "assistant", "content": content or ""}
        if tool_calls:
            record["tool_calls"] = tool_calls
        if reasoning_content is not None:
            record["reasoning_content"] = reasoning_content
        if conversation_id:
            record["conversation_id"] = conversation_id
        async with self._mutation_lock:
            await self._append_records_and_mirror([record])
            await self._notify([record])

    async def add_tool_result(
        self,
        tool_call_id: str,
        content: str,
        conversation_id: str | None = None,
    ) -> None:
        record = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        if conversation_id:
            record["conversation_id"] = conversation_id
        async with self._mutation_lock:
            await self._append_records_and_mirror([record])
            await self._notify([record])

    async def add_system_note(
        self,
        content: str,
        conversation_id: str | None = None,
    ) -> None:
        """记录一条系统注解（如发送结果、撤回事件等）。"""
        if not content:
            return
        record = {"role": "system", "content": content}
        if conversation_id:
            record["conversation_id"] = conversation_id
        async with self._mutation_lock:
            await self._append_records_and_mirror([record])
            await self._record_system_note_event(record)
            await self._notify([record])

    async def add_records(
        self,
        records: list[dict],
        conversation_id: str | None = None,
    ) -> None:
        """批量追加（如 agent 一轮工具循环后的所有 records）。"""
        started_at = time.perf_counter()
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        if not records:
            if debug_enabled:
                logger.debug(
                    "HistoryManager add_records 指标 input_records=0 appended_records=0 "
                    "elapsed_ms=%.3f",
                    (time.perf_counter() - started_at) * 1000,
                )
            return
        if conversation_id:
            records = [
                ({**record, "conversation_id": record.get("conversation_id") or conversation_id})
                for record in records
            ]
        async with self._mutation_lock:
            await self._append_records_and_mirror(records)
            await self._notify(records)
        if debug_enabled:
            logger.debug(
                "HistoryManager add_records 指标 input_records=%d appended_records=%d "
                "elapsed_ms=%.3f",
                len(records),
                len(records),
                (time.perf_counter() - started_at) * 1000,
            )

    async def length(self) -> int:
        return await self._store.length()

    async def get_slice(self, start: int = 0, end: int | None = None) -> list[dict]:
        return await self._store.get_slice(start, end)

    async def truncate_head(self, cut_point: int) -> int:
        """删除最早的 cut_point 条记录。返回剩余长度。"""
        async with self._mutation_lock:
            remaining = await self._store.truncate_head(cut_point)
            await self._mirror_truncated(cut_point, remaining)
        return remaining

    async def clear(self) -> None:
        """清空所有历史（慎用）。"""
        async with self._mutation_lock:
            await self._store.clear()


def _infer_conversation_id(record: dict) -> str | None:
    """从旧记录 metadata 中尽量推导 conversation_id，不回写历史文件。"""
    meta = record.get("metadata")
    if not isinstance(meta, dict):
        return None

    messages = meta.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            scope = last.get("scope")
            target_id = last.get("target_id")
            group_id = last.get("group_id")
            user_id = last.get("user_id")
            if scope == "group" and (group_id or target_id):
                return f"group:{group_id or target_id}"
            if scope == "private" and (target_id or user_id):
                return f"private:{target_id or user_id}"
            if group_id:
                return f"group:{group_id}"
            if user_id:
                return f"private:{user_id}"

    scope = meta.get("scope")
    target_id = meta.get("target_id")
    group_id = meta.get("group_id")
    user_id = meta.get("user_id")
    if scope == "group" and (group_id or target_id):
        return f"group:{group_id or target_id}"
    if scope == "private" and (target_id or user_id):
        return f"private:{target_id or user_id}"
    if group_id:
        return f"group:{group_id}"
    if user_id:
        return f"private:{user_id}"
    return None


def _history_record_event_payload(
    record: dict,
    *,
    history_index: int,
    batch_index: int,
) -> dict:
    content_length, content_hash = _content_fingerprint(record.get("content"))
    conversation_id = _history_record_conversation_id(record)
    payload = {
        "source": HISTORY_EVENT_PAYLOAD_SOURCE,
        "history_index": history_index,
        "history_offset": history_index,
        "batch_index": batch_index,
        "role": _optional_text(record.get("role")),
        "conversation_id": conversation_id,
        "tool_call_id": _optional_text(record.get("tool_call_id")),
        "tool_call_ids": _tool_call_ids(record),
        "record": record,
        "content_hash": content_hash,
        "content_length": content_length,
        "record_keys": [str(key) for key in record.keys()],
    }
    return {key: value for key, value in payload.items() if value is not None}


def _history_record_event(
    record: dict,
    *,
    history_index: int,
    batch_index: int,
) -> dict:
    conversation_id = _history_record_conversation_id(record)
    return {
        "event_type": HISTORY_RECORD_EVENT_TYPE,
        "conversation_id": conversation_id,
        "source": "history_manager",
        "tool_call_id": _optional_text(record.get("tool_call_id")),
        "payload": _history_record_event_payload(
            record,
            history_index=history_index,
            batch_index=batch_index,
        ),
        "schema_version": HISTORY_JOURNAL_SCHEMA_VERSION,
    }


def _history_record_conversation_id(record: dict) -> str | None:
    return _optional_text(record.get("conversation_id")) or _infer_conversation_id(record)


def _tool_call_ids(record: dict) -> list[str] | None:
    tool_call_id = _optional_text(record.get("tool_call_id"))
    if tool_call_id:
        return [tool_call_id]
    tool_calls = record.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    ids = [
        cleaned
        for tool_call in tool_calls
        if isinstance(tool_call, dict)
        for cleaned in [_optional_text(tool_call.get("id"))]
        if cleaned is not None
    ]
    return ids or None


def _content_fingerprint(content: object) -> tuple[int, str]:
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        text = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
