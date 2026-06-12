"""Working-history filtering helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change filtering logic while moving helpers here.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

_WORKING_HISTORY_RECENT_RUNTIME_RECORDS = 12
_WORKING_HISTORY_SEND_RECEIPT_KEEP = 4
_WORKING_HISTORY_NO_ACTION_KEEP = 8
_T = TypeVar("_T")


def _recent_items(items: list[_T], count: int) -> list[_T]:
    if count <= 0:
        return []
    return items[-count:]


def _record_timestamp(record: dict[str, Any]) -> Any:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        if meta.get("timestamp") is not None:
            return meta.get("timestamp")
        messages = meta.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                return last.get("timestamp")
    return None


def _record_conversation_id(record: dict[str, Any]) -> str | None:
    """从记录里读取会话标签；兼容未迁移的旧 metadata，不回写历史。"""
    if record.get("conversation_id"):
        return str(record.get("conversation_id"))
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


def _runtime_context_kind(record: dict[str, Any]) -> str | None:
    """Return the runtime-context category for records that are not real chat."""
    if record.get("role") != "user":
        return None

    meta = record.get("metadata")
    if isinstance(meta, dict):
        kind = str(meta.get("kind") or "")
        if kind in {"task_context_snapshot", "send_done_snapshot"}:
            return kind

    content = str(record.get("content") or "")
    if "<task_context" in content:
        return "task_context_snapshot"
    if "<send_status" in content:
        return "send_done_snapshot"
    if "<send_receipt" in content:
        return "send_receipt"
    return None


def _assistant_tool_call_names(record: dict[str, Any]) -> dict[str, str]:
    if record.get("role") != "assistant":
        return {}
    result: dict[str, str] = {}
    for tool_call in record.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        call_id = str(tool_call.get("id") or "")
        function = tool_call.get("function")
        name = ""
        if isinstance(function, dict):
            name = str(function.get("name") or "")
        if call_id and name:
            result[call_id] = name
    return result


def _tool_result_is_no_action(record: dict[str, Any], tool_call_id: str) -> bool:
    if record.get("role") != "tool":
        return False
    if str(record.get("tool_call_id") or "") != tool_call_id:
        return False
    try:
        payload = json.loads(str(record.get("content") or "{}"))
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and bool(payload.get("no_action"))


def _no_action_pair_indices(records: list[dict[str, Any]], start: int) -> set[int]:
    """Return a complete assistant/tool no_action block, or empty set."""
    if start >= len(records):
        return set()
    assistant_record = records[start]
    if str(assistant_record.get("content") or "").strip():
        return set()
    tool_names = _assistant_tool_call_names(assistant_record)
    if not tool_names or any(name != "no_action" for name in tool_names.values()):
        return set()
    tool_call_ids = list(tool_names)
    end = start + len(tool_call_ids)
    if end >= len(records):
        return set()
    for offset, tool_call_id in enumerate(tool_call_ids, start=1):
        if not _tool_result_is_no_action(records[start + offset], tool_call_id):
            return set()
    return set(range(start, end + 1))


def _working_history_noise_indices(
    records: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    ensure_current_records: int,
    runtime_record_keep_count: int,
    send_receipt_keep_count: int,
    no_action_keep_count: int,
) -> set[int]:
    """Indices of stale runtime records that can be skipped while filling budget."""
    if not records:
        return set()

    force_keep = _working_history_force_keep_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    runtime_indices = [
        idx for idx, record in enumerate(records)
        if _runtime_context_kind(record) is not None
    ]
    keep_runtime = set(_recent_items(runtime_indices, runtime_record_keep_count))
    send_receipt_indices = [
        idx for idx in runtime_indices
        if _runtime_context_kind(records[idx]) == "send_receipt"
    ]
    keep_runtime.update(_recent_items(send_receipt_indices, send_receipt_keep_count))

    drop_indices: set[int] = set()
    for idx in runtime_indices:
        if idx not in force_keep and idx not in keep_runtime:
            drop_indices.add(idx)

    no_action_pairs: list[set[int]] = []
    idx = 0
    while idx < len(records):
        pair = _no_action_pair_indices(records, idx)
        if not pair:
            idx += 1
            continue
        no_action_pairs.append(pair)
        idx = max(pair) + 1

    kept_no_action_indices: set[int] = set()
    for pair in _recent_items(no_action_pairs, no_action_keep_count):
        kept_no_action_indices.update(pair)
    for pair in no_action_pairs:
        if pair & force_keep:
            continue
        if pair & kept_no_action_indices:
            continue
        drop_indices.update(pair)

    return drop_indices


def _working_history_force_keep_indices(
    records: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    ensure_current_records: int,
) -> set[int]:
    force_keep: set[int] = set()
    if not conversation_id or ensure_current_records <= 0:
        return force_keep
    for idx in range(len(records) - 1, -1, -1):
        if _record_conversation_id(records[idx]) != conversation_id:
            continue
        force_keep.add(idx)
        if len(force_keep) >= ensure_current_records:
            break
    return force_keep


def _working_history_optional_runtime_indices(
    records: list[dict[str, Any]],
    *,
    conversation_id: str | None,
    ensure_current_records: int,
    runtime_record_keep_count: int,
    send_receipt_keep_count: int,
    no_action_keep_count: int,
) -> set[int]:
    """Recent runtime/no_action records that may use leftover budget."""
    drop_indices = _working_history_noise_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
        runtime_record_keep_count=runtime_record_keep_count,
        send_receipt_keep_count=send_receipt_keep_count,
        no_action_keep_count=no_action_keep_count,
    )
    force_keep = _working_history_force_keep_indices(
        records,
        conversation_id=conversation_id,
        ensure_current_records=ensure_current_records,
    )
    optional = {
        idx for idx, record in enumerate(records)
        if _runtime_context_kind(record) is not None
    }
    idx = 0
    while idx < len(records):
        pair = _no_action_pair_indices(records, idx)
        if not pair:
            idx += 1
            continue
        optional.update(pair)
        idx = max(pair) + 1
    return optional - drop_indices - force_keep
