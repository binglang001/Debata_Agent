"""对话页记录加载与去重 helper。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .display_items import _parse_float_value, _record_timestamp_sort_value
from .grouping import _LEGACY_HEADER_RE, _conversation_info, _record_role
from .models import DisplaySeverity
from .text_format import (
    _compact_inline_tokens,
    _extract_tag_json,
    _first_nonempty_line,
    _format_send_receipt_summary,
    _format_send_status_summary,
    _format_task_context_summary,
    _parse_json_object,
    _send_status_info,
)

logger = logging.getLogger("ui.dashboard.chats_page")

ARCHIVE_FETCH_PAGE_SIZE = 500
EVENT_STORE_QQ_FETCH_LIMIT = 500
QQ_VISIBLE_EVENT_TYPES = ("qq_message_received", "qq_message_sent")
RUNTIME_EVENT_TYPES = (
    "tool_call_started",
    "tool_result_received",
    "system_note_recorded",
    "history_truncated",
    "send_attempt_recorded",
    "send_batch_accepted",
    "send_message_started",
    "send_message_succeeded",
    "send_receipt_recorded",
)
EVENT_STORE_CHAT_PAGE_EVENT_TYPES = (*QQ_VISIBLE_EVENT_TYPES, *RUNTIME_EVENT_TYPES)


def _record_conversation_id_for_display(rec: dict[str, Any]) -> str:
    display = rec.get("_display_conversation_id")
    if isinstance(display, str) and display:
        return display
    direct = rec.get("conversation_id")
    if isinstance(direct, str) and direct:
        return direct
    if _record_role(rec) == "user":
        return _conversation_info(rec)["key"]
    return "system:global" if _record_role(rec) in {"system", "tool"} else "unknown:history"


def _record_timestamp(rec: dict[str, Any]) -> str | None:
    for key in ("timestamp", "time", "created_at"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        date = meta.get("date")
        if isinstance(date, str) and date.strip():
            return date.strip()
    content = str(rec.get("content") or "")
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        return match.group("timestamp")
    return None


def _record_display_base_id(rec: dict[str, Any], *, conversation_id: str, role: str) -> str:
    for key in ("archive_id", "id", "message_id", "msg_id", "tool_call_id"):
        value = rec.get(key)
        if value:
            return str(value)
    content = str(rec.get("content") or "")
    return f"{conversation_id}:{role}:{_record_timestamp(rec) or ''}:{len(content)}"


def _record_message_id(rec: dict[str, Any]) -> str | None:
    for key in ("message_id", "msg_id", "original_msg_id"):
        value = rec.get(key)
        if value:
            return str(value)
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        for key in ("original_msg_id", "message_id", "msg_id"):
            value = meta.get(key)
            if value:
                return str(value)
    content = str(rec.get("content") or "")
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        return match.group("message_id")
    return None


def _record_is_qq_visible_outbound(rec: dict[str, Any]) -> bool:
    if str(rec.get("direction") or "") == "outbound":
        return rec.get("qq_visible") is not False
    if rec.get("qq_visible") is True:
        return True
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        if str(meta.get("direction") or "") == "outbound":
            return meta.get("qq_visible") is not False
        if meta.get("qq_visible") is True:
            return True
    return False


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    func = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(func, dict):
        return ""
    return str(func.get("name") or "")


def _runtime_tool_call_for_record(rec: dict[str, Any], *, base_id: str) -> dict[str, Any]:
    for tool_call in rec.get("tool_calls") or []:
        if isinstance(tool_call, dict):
            return tool_call
    payload = rec.get("metadata", {}).get("event_payload") if isinstance(rec.get("metadata"), dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    tool_call_id = str(rec.get("tool_call_id") or f"{base_id}:tool").strip()
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": _runtime_event_tool_name(payload),
            "arguments": json.dumps(
                _runtime_tool_call_arguments(payload),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        },
    }


def _runtime_record_severity(rec: dict[str, Any]) -> DisplaySeverity:
    payload = rec.get("metadata", {}).get("event_payload") if isinstance(rec.get("metadata"), dict) else {}
    blob = json.dumps(payload if isinstance(payload, dict) else rec, ensure_ascii=False, default=str).casefold()
    if any(token in blob for token in ("failed", "failure", "error", "stale", "失败", "错误", "过期")):
        return "warning"
    if isinstance(payload, dict) and payload.get("ok") is False:
        return "warning"
    return "info"


async def _load_chat_page_records(runtime: Any) -> list[dict]:
    started_at = time.perf_counter()
    step_started_at = started_at
    history = getattr(runtime, "history", None)
    history_records = list(await history.records() or []) if history is not None else []
    history_elapsed_ms = (time.perf_counter() - step_started_at) * 1000

    step_started_at = time.perf_counter()
    event_records = await _load_event_store_records(runtime)
    event_elapsed_ms = (time.perf_counter() - step_started_at) * 1000

    step_started_at = time.perf_counter()
    timeline_records = _load_chat_timeline_records(runtime)
    timeline_elapsed_ms = (time.perf_counter() - step_started_at) * 1000

    archive = getattr(runtime, "archive", None)
    archive_records: list[dict] = []
    step_started_at = time.perf_counter()
    if archive is None:
        archive_elapsed_ms = (time.perf_counter() - step_started_at) * 1000
    else:
        try:
            archive_records = list(await _load_archive_records_paged(archive) or [])
        except Exception as e:
            logger.warning(f"加载 archive 失败，仅显示实时聊天和运行时事件: {e}")
            archive_records = []
        archive_elapsed_ms = (time.perf_counter() - step_started_at) * 1000

    step_started_at = time.perf_counter()
    records = _tag_record_order(
        _merge_chat_page_records(
            archive_records=archive_records,
            event_records=event_records,
            timeline_records=timeline_records,
            history_records=history_records,
        )
    )
    merge_elapsed_ms = (time.perf_counter() - step_started_at) * 1000
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "对话页记录加载指标 history_ms=%.3f event_store_ms=%.3f timeline_ms=%.3f "
            "archive_ms=%.3f merge_tag_ms=%.3f total_ms=%.3f history_records=%d "
            "event_store_records=%d timeline_records=%d archive_records=%d total_records=%d",
            history_elapsed_ms,
            event_elapsed_ms,
            timeline_elapsed_ms,
            archive_elapsed_ms,
            merge_elapsed_ms,
            (time.perf_counter() - started_at) * 1000,
            len(history_records),
            len(event_records),
            len(timeline_records),
            len(archive_records),
            len(records),
        )
    return records


async def _load_event_store_records(runtime: Any) -> list[dict[str, Any]]:
    event_store = getattr(runtime, "event_store", None)
    if event_store is None:
        return []
    try:
        events = await _recent_chat_page_events(event_store)
    except Exception as e:
        logger.warning(f"加载 EventStore 对话页事件失败，回退到实时/归档/历史记录: {e}")
        return []

    records: list[dict[str, Any]] = []
    for event in events:
        record = _event_store_event_to_record(event)
        if record is not None:
            records.append(record)
    return records


async def _recent_chat_page_events(event_store: Any) -> list[dict[str, Any]]:
    events_by_type = getattr(event_store, "events_by_type", None)
    if callable(events_by_type):
        events: list[dict[str, Any]] = []
        for event_type in EVENT_STORE_CHAT_PAGE_EVENT_TYPES:
            page = await events_by_type(
                event_type,
                limit=EVENT_STORE_QQ_FETCH_LIMIT,
                order="desc",
            )
            if isinstance(page, list):
                events.extend(item for item in page if isinstance(item, dict))
        iter_events = getattr(event_store, "iter_events", None)
        if callable(iter_events):
            try:
                page = await iter_events(limit=EVENT_STORE_QQ_FETCH_LIMIT, order="desc")
            except Exception as e:
                logger.debug(f"补充扫描 EventStore 最近事件失败，已使用按类型查询结果: {e}")
            else:
                if isinstance(page, list):
                    events.extend(item for item in page if isinstance(item, dict))
        return _sort_unique_chat_page_events(events)

    iter_events = getattr(event_store, "iter_events", None)
    if not callable(iter_events):
        return []
    page = await iter_events(limit=EVENT_STORE_QQ_FETCH_LIMIT, order="desc")
    if not isinstance(page, list):
        return []
    return _sort_unique_chat_page_events(item for item in page if isinstance(item, dict))


def _sort_unique_chat_page_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_event_id: dict[int, dict[str, Any]] = {}
    for event in events:
        if not _event_type_visible_on_chat_page(str(event.get("event_type") or "")):
            continue
        event_id = _event_id(event)
        if event_id is None or event_id in by_event_id:
            continue
        by_event_id[event_id] = event
    return sorted(by_event_id.values(), key=_event_sort_id)


def _event_type_visible_on_chat_page(event_type: str) -> bool:
    return event_type in QQ_VISIBLE_EVENT_TYPES or _event_type_is_runtime(event_type)


def _event_type_is_runtime(event_type: str) -> bool:
    return event_type in RUNTIME_EVENT_TYPES or event_type.startswith("send_")


def _event_store_event_to_record(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    if event_type in QQ_VISIBLE_EVENT_TYPES:
        return _qq_visible_event_to_record(event)
    if _event_type_is_runtime(event_type):
        return _runtime_event_to_record(event)
    return None


def _qq_visible_event_to_record(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("event_type")
    if event_type not in QQ_VISIBLE_EVENT_TYPES:
        return None
    event_id = _event_id(event)
    if event_id is None:
        return None
    payload_value = event.get("payload")
    payload = payload_value if isinstance(payload_value, dict) else {}
    direction = "outbound" if event_type == "qq_message_sent" else "inbound"
    conversation_id = _qq_event_conversation_id(event, payload)
    if not conversation_id:
        return None
    content = _first_payload_text(payload, ("content", "text", "label", "raw_message"))
    msg_id = _first_payload_text(payload, ("msg_id", "message_id")) or _optional_record_text(
        event.get("external_id")
    )
    timestamp = _qq_event_timestamp(event, payload)
    user_id = _optional_record_text(payload.get("user_id"))
    self_id = _optional_record_text(payload.get("self_id"))
    sender_id = (
        _first_payload_text(payload, ("sender_id",))
        or (self_id if direction == "outbound" else user_id)
    )
    sender_name = _first_payload_text(payload, ("sender_name", "nickname"))

    return {
        "id": f"event:{event_id}",
        "event_id": event_id,
        "event_type": event_type,
        "role": "assistant" if direction == "outbound" else "user",
        "direction": direction,
        "qq_visible": True,
        "conversation_id": conversation_id,
        "content": content,
        "msg_id": msg_id,
        "message_id": msg_id,
        "timestamp": timestamp,
        "_sort_layer": "event_store",
        "_sort_value": float(event_id),
        "_sort_kind": "event_id",
        "_source": "event_store",
        "source": _optional_record_text(payload.get("source"))
        or _optional_record_text(event.get("source")),
        "sender_name": sender_name,
        "sender_id": sender_id,
        "user_id": user_id,
        "target_id": _optional_record_text(payload.get("target_id")),
        "target_scope": _optional_record_text(payload.get("target_scope")),
        "group_id": _optional_record_text(payload.get("group_id")),
        "self_id": self_id,
        "raw_message": _first_payload_text(payload, ("raw_message", "text", "content")),
        "reply_to": _optional_record_text(payload.get("reply_to")),
        "attachments": _payload_list(payload, "attachments", "media"),
        "cq_segments": _payload_list(payload, "cq_segments"),
    }


def _runtime_event_to_record(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    if not _event_type_is_runtime(event_type):
        return None
    event_id = _event_id(event)
    if event_id is None:
        return None
    payload_value = event.get("payload")
    payload = payload_value if isinstance(payload_value, dict) else {}
    conversation_id = _runtime_event_conversation_id(event, payload)
    timestamp = _qq_event_timestamp(event, payload)
    title = _runtime_event_title(event_type, payload)
    summary = _runtime_event_summary_text(event_type, payload, event)
    detail = _runtime_event_detail(event_type, payload, event)
    base = {
        "id": f"event:{event_id}",
        "event_id": event_id,
        "event_type": event_type,
        "_runtime_event_type": event_type,
        "_runtime_title": title,
        "_runtime_summary": summary,
        "_runtime_detail": detail,
        "_source": "event_store",
        "source": _optional_record_text(payload.get("source"))
        or _optional_record_text(event.get("source")),
        "conversation_id": conversation_id,
        "timestamp": timestamp,
        "_sort_layer": "event_store",
        "_sort_value": float(event_id),
        "_sort_kind": "event_id",
        "metadata": {"event_payload": payload},
    }

    if event_type == "tool_call_started":
        tool_call_id = _runtime_event_tool_call_id(event, payload, fallback=f"event:{event_id}")
        tool_name = _runtime_event_tool_name(payload)
        tool_call = {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(
                    _runtime_tool_call_arguments(payload),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        }
        return {
            **base,
            "role": "assistant",
            "content": "",
            "tool_call_id": tool_call_id,
            "tool_calls": [tool_call],
        }

    if event_type == "tool_result_received":
        tool_call_id = _runtime_event_tool_call_id(event, payload, fallback="")
        return {
            **base,
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(
                _runtime_tool_result_payload(payload, event),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        }

    return {
        **base,
        "role": "system",
        "content": detail,
    }


def _runtime_event_conversation_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
    direct = (
        _optional_record_text(payload.get("conversation_id"))
        or _optional_record_text(event.get("conversation_id"))
        or _optional_record_text(payload.get("target_conversation_id"))
    )
    if direct:
        return direct
    conversation_ids = payload.get("conversation_ids")
    if isinstance(conversation_ids, list) and len(conversation_ids) == 1:
        value = _optional_record_text(conversation_ids[0])
        if value:
            return value
    target_scope = _optional_record_text(payload.get("target_scope"))
    target_id = _optional_record_text(payload.get("target_id"))
    if target_scope and target_id:
        return f"{target_scope}:{target_id}"
    return "system:global"


def _runtime_event_tool_call_id(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    fallback: str,
) -> str:
    return (
        _optional_record_text(payload.get("tool_call_id"))
        or _optional_record_text(event.get("tool_call_id"))
        or fallback
    )


def _runtime_event_tool_name(payload: dict[str, Any]) -> str:
    return (
        _optional_record_text(payload.get("tool_name"))
        or _optional_record_text(payload.get("name"))
        or "未知工具"
    )


def _runtime_tool_call_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    return _runtime_payload_subset(
        payload,
        (
            "tool_name",
            "args_keys",
            "args_length",
            "args_preview",
            "loop",
            "step",
            "status",
            "error_type",
        ),
    )


def _runtime_tool_result_payload(
    payload: dict[str, Any],
    event: dict[str, Any],
) -> dict[str, Any]:
    result = _runtime_payload_subset(
        payload,
        (
            "tool_name",
            "tool_call_id",
            "ok",
            "status",
            "error_type",
            "args_keys",
            "args_length",
            "result_keys",
            "result_length",
            "result_hash",
            "result_preview",
            "loop",
            "step",
        ),
    )
    if "tool_call_id" not in result:
        tool_call_id = _optional_record_text(event.get("tool_call_id"))
        if tool_call_id:
            result["tool_call_id"] = tool_call_id
    return result or {"event_type": "tool_result_received"}


def _runtime_payload_subset(payload: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        if key in payload and payload.get(key) is not None:
            result[key] = payload.get(key)
    return result


def _runtime_event_title(event_type: str, payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    if event_type == "tool_call_started":
        return f"工具调用：{_runtime_event_tool_name(payload)}"
    if event_type == "tool_result_received":
        tool_name = _runtime_event_tool_name(payload)
        return f"工具返回：{tool_name}" if tool_name != "未知工具" else "工具返回"
    labels = {
        "system_note_recorded": "系统消息记录",
        "history_truncated": "历史截断",
        "send_attempt_recorded": "发送尝试",
        "send_batch_accepted": "发送批次已接受",
        "send_message_started": "发送消息开始",
        "send_message_succeeded": "发送消息成功",
        "send_receipt_recorded": "发送回执记录",
    }
    return labels.get(event_type, "发送状态" if event_type.startswith("send_") else event_type)


def _runtime_event_summary_text(
    event_type: str,
    payload: dict[str, Any],
    event: dict[str, Any],
) -> str:
    parts = _runtime_event_summary_parts(payload)
    if not parts:
        external_id = _optional_record_text(event.get("external_id"))
        if external_id:
            parts.append(f"id={external_id}")
    if not parts:
        parts.append(event_type)
    return "；".join(parts)


def _runtime_event_detail(
    event_type: str,
    payload: dict[str, Any],
    event: dict[str, Any],
) -> str:
    parts = [f"{_runtime_event_title(event_type, payload)}"]
    event_id = _event_id(event)
    if event_id is not None:
        parts.append(f"event_id={event_id}")
    tool_call_id = _runtime_event_tool_call_id(event, payload, fallback="")
    if tool_call_id:
        parts.append(f"tool_call_id={tool_call_id}")
    parts.extend(_runtime_event_summary_parts(payload, include_preview=True))
    if not payload:
        parts.append("payload 为空")
    return "；".join(_unique_text_parts(parts))


def _runtime_event_summary_parts(
    payload: dict[str, Any],
    *,
    include_preview: bool = False,
) -> list[str]:
    parts: list[str] = []
    for key, label in (
        ("tool_name", "工具"),
        ("status", "状态"),
        ("send_id", "send_id"),
        ("send_attempt_id", "send_attempt_id"),
        ("attempt_id", "attempt_id"),
        ("msg_id", "msg_id"),
        ("delivery", "投递"),
        ("source_tool", "来源工具"),
        ("kind", "类型"),
        ("target_conversation_id", "目标会话"),
        ("conversation_id", "会话"),
        ("error_type", "错误类型"),
    ):
        value = _optional_record_text(payload.get(key))
        if value:
            parts.append(f"{label}={value}" if label.endswith("_id") else f"{label} {value}")
    if "ok" in payload:
        parts.append("成功" if payload.get("ok") is True else "失败")
    for key, label in (
        ("count", "数量"),
        ("order", "顺序"),
        ("loop", "loop"),
        ("step", "step"),
        ("args_length", "参数长度"),
        ("result_length", "结果长度"),
        ("content_length", "内容长度"),
        ("cut_point", "截断点"),
        ("remaining_count", "剩余"),
    ):
        value = payload.get(key)
        if isinstance(value, int | float | str) and str(value).strip():
            parts.append(f"{label} {value}")
    counts = payload.get("counts")
    if isinstance(counts, dict) and counts:
        count_parts = [
            f"{key}={value}"
            for key, value in counts.items()
            if isinstance(value, int | float | str) and str(value).strip()
        ]
        if count_parts:
            parts.append("counts " + ", ".join(count_parts[:8]))
    for key, label in (
        ("args_keys", "参数键"),
        ("result_keys", "结果键"),
        ("conversation_ids", "会话"),
    ):
        value = payload.get(key)
        if isinstance(value, list) and value:
            shown = ", ".join(str(item) for item in value[:6])
            if len(value) > 6:
                shown = f"{shown}, +{len(value) - 6}"
            parts.append(f"{label} [{shown}]")
    for key, label in (
        ("content_hash", "内容hash"),
        ("result_hash", "结果hash"),
    ):
        value = _optional_record_text(payload.get(key))
        if value:
            parts.append(f"{label}={value}")
    if include_preview:
        for key, label in (
            ("preview", "预览"),
            ("args_preview", "参数预览"),
            ("result_preview", "结果预览"),
        ):
            value = _optional_record_text(payload.get(key))
            if value:
                parts.append(f"{label}：{_compact_inline_tokens(value)}")
    return parts


def _unique_text_parts(parts: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _event_id(event: dict[str, Any]) -> int | None:
    for key in ("event_id", "id"):
        value = event.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _event_sort_id(event: dict[str, Any]) -> int:
    return _event_id(event) or 0


def _qq_event_conversation_id(event: dict[str, Any], payload: dict[str, Any]) -> str | None:
    direct = _optional_record_text(payload.get("conversation_id")) or _optional_record_text(
        event.get("conversation_id")
    )
    if direct:
        return direct
    group_id = _optional_record_text(payload.get("group_id"))
    if group_id:
        return f"group:{group_id}"
    target_id = _optional_record_text(payload.get("target_id"))
    target_scope = _optional_record_text(payload.get("target_scope"))
    if target_id:
        return f"group:{target_id}" if target_scope == "group" else f"private:{target_id}"
    user_id = _optional_record_text(payload.get("user_id"))
    if user_id:
        return f"private:{user_id}"
    return None


def _qq_event_timestamp(event: dict[str, Any], payload: dict[str, Any]) -> str | None:
    text = _first_payload_text(payload, ("time_text", "timestamp", "time", "created_at"))
    if text:
        return text
    for value in (payload.get("timestamp_unix"), event.get("timestamp_unix")):
        try:
            return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            pass
    return None


def _first_payload_text(payload: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        text = _optional_record_text(payload.get(key))
        if text:
            return text
    return ""


def _payload_list(payload: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _optional_record_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_chat_timeline_records(runtime: Any) -> list[dict[str, Any]]:
    pipeline = getattr(runtime, "pipeline", None)
    timeline = getattr(pipeline, "chat_timeline", None)
    snapshot = getattr(timeline, "snapshot", None)
    if not callable(snapshot):
        return []
    try:
        conversations = snapshot()
    except Exception as e:
        logger.warning(f"加载实时聊天时间线失败: {e}")
        return []
    records: list[dict[str, Any]] = []
    if not isinstance(conversations, dict):
        return records
    for conversation_id, messages in conversations.items():
        if not isinstance(messages, list):
            continue
        for message in messages:
            records.append(_chat_timeline_message_to_record(str(conversation_id), message))
    return records


def _chat_timeline_message_to_record(conversation_id: str, message: Any) -> dict[str, Any]:
    direction = str(getattr(message, "direction", "") or "")
    text = str(getattr(message, "text", "") or "")
    raw_message = str(getattr(message, "raw_message", "") or "")
    content = text or raw_message
    timestamp = getattr(message, "timestamp", None)
    try:
        sort_ts = float(timestamp)
    except (TypeError, ValueError):
        sort_ts = None
    return {
        "role": "assistant" if direction == "outbound" else "user",
        "direction": direction,
        "content": content,
        "conversation_id": conversation_id,
        "timestamp": str(getattr(message, "time_text", "") or "") or None,
        "_sort_ts": sort_ts,
        "_sort_layer": "timeline",
        "_sort_value": sort_ts,
        "_sort_kind": "timestamp",
        "_source": "chat_timeline",
        "qq_visible": True,
        "sender_name": getattr(message, "sender_name", None),
        "sender_id": getattr(message, "sender_id", None),
        "target_id": getattr(message, "target_id", None),
        "group_id": getattr(message, "group_id", None),
        "msg_id": getattr(message, "msg_id", None),
        "raw_message": raw_message,
        "reply_to": getattr(message, "reply_to", None),
        "attachments": list(getattr(message, "attachments", []) or []),
        "cq_segments": list(getattr(message, "cq_segments", []) or []),
    }


def _merge_chat_page_records(
    *,
    archive_records: list[dict],
    event_records: list[dict],
    timeline_records: list[dict],
    history_records: list[dict],
) -> list[dict]:
    event_unique = _dedupe_event_store_records(event_records)
    event_real_ids = {
        identity
        for record in event_unique
        if (identity := _real_record_identity(record)) is not None
    }
    event_runtime_identities = _event_store_runtime_duplicate_identities(event_unique)
    timeline_unique = [
        record
        for record in timeline_records
        if _real_record_identity(record) not in event_real_ids
    ]
    visible_real_ids = {
        identity
        for record in [*event_records, *timeline_unique]
        if (identity := _real_record_identity(record)) is not None
    }
    archive_unique = [
        record
        for record in archive_records
        if _real_record_identity(record) not in visible_real_ids
    ]
    return [
        *[_record_with_sort_layer(record, "archive") for record in archive_unique],
        *[_record_with_sort_layer(record, "event_store") for record in event_unique],
        *[_record_with_sort_layer(record, "timeline") for record in timeline_unique],
        *[
            _record_with_sort_layer(record, "history")
            for record in _history_runtime_event_records(
                history_records,
                skip_runtime_identities=event_runtime_identities,
            )
        ],
    ]


def _dedupe_event_store_records(records: list[dict]) -> list[dict]:
    seen_event_ids: set[int] = set()
    seen_real: set[tuple[str, str, str]] = set()
    seen_runtime: set[tuple[str, ...]] = set()
    result: list[dict] = []
    for record in records:
        event_id = _record_event_id(record)
        if event_id is not None:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        real_identity = _real_record_identity(record)
        if real_identity is not None:
            if real_identity in seen_real:
                continue
            seen_real.add(real_identity)
        runtime_identity = _event_store_record_duplicate_identity(record)
        if runtime_identity is not None:
            if runtime_identity in seen_runtime:
                continue
            seen_runtime.add(runtime_identity)
        result.append(record)
    return result


def _dedupe_records_by_real_identity(records: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict] = []
    for record in records:
        identity = _real_record_identity(record)
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        result.append(record)
    return result


def _record_event_id(record: dict[str, Any]) -> int | None:
    value = record.get("event_id")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _event_store_record_duplicate_identity(record: dict[str, Any]) -> tuple[str, ...] | None:
    if record.get("_source") != "event_store":
        return None
    event_type = str(record.get("_runtime_event_type") or record.get("event_type") or "")
    if event_type == "tool_call_started":
        tool_call_id = _runtime_record_tool_call_identity(record)
        return ("tool_call_started", tool_call_id) if tool_call_id else None
    if event_type == "tool_result_received":
        tool_call_id = str(record.get("tool_call_id") or "").strip()
        return ("tool_result_received", tool_call_id) if tool_call_id else None
    if event_type == "system_note_recorded":
        identity = _system_note_duplicate_identity(record)
        return ("system_note_recorded", *identity) if identity else None
    if event_type == "history_truncated":
        payload = _record_event_payload(record)
        return (
            "history_truncated",
            _duplicate_identity_text(payload.get("cut_point")),
            _duplicate_identity_text(payload.get("remaining_count")),
        )
    if event_type.startswith("send_"):
        send_id = _send_runtime_primary_duplicate_id(record)
        if not send_id:
            return None
        if event_type in {"send_message_started", "send_message_succeeded"}:
            payload = _record_event_payload(record)
            return (
                event_type,
                send_id,
                _duplicate_identity_text(payload.get("order"), record.get("order")),
                _duplicate_identity_text(
                    payload.get("target_conversation_id"),
                    record.get("conversation_id"),
                ),
                _duplicate_identity_text(payload.get("msg_id"), record.get("msg_id")),
            )
        return (event_type, send_id)
    return None


def _duplicate_identity_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _runtime_record_tool_call_identity(record: dict[str, Any]) -> str | None:
    tool_call_id = str(record.get("tool_call_id") or "").strip()
    if tool_call_id:
        return tool_call_id
    for tool_call in record.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = str(tool_call.get("id") or "").strip()
        if tool_call_id:
            return tool_call_id
    return None


def _send_runtime_primary_duplicate_id(record: dict[str, Any]) -> str | None:
    payload = _record_event_payload(record)
    for source in (payload, record):
        for key in ("send_id", "send_attempt_id", "attempt_id"):
            value = _optional_record_text(source.get(key))
            if value:
                return value
    return None


def _record_event_payload(record: dict[str, Any]) -> dict[str, Any]:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        payload = meta.get("event_payload")
        if isinstance(payload, dict):
            return payload
    return {}


def _record_with_sort_layer(record: dict[str, Any], layer: str) -> dict[str, Any]:
    tagged = dict(record)
    tagged["_sort_layer"] = layer
    if layer == "event_store":
        tagged.pop("_sort_ts", None)
        tagged["_sort_kind"] = "event_id"
        if tagged.get("_sort_value") is None:
            event_id = _parse_float_value(tagged.get("event_id"))
            if event_id is not None:
                tagged["_sort_value"] = event_id
    else:
        tagged["_sort_kind"] = "timestamp"
        if tagged.get("_sort_value") is None:
            timestamp = _record_timestamp_sort_value(tagged)
            if timestamp is not None:
                tagged["_sort_value"] = timestamp
    return tagged


def _history_runtime_event_records(
    records: list[dict],
    *,
    skip_runtime_identities: set[tuple[str, ...]] | None = None,
) -> list[dict]:
    skip_runtime_identities = skip_runtime_identities or set()
    result: list[dict] = []
    for record in records:
        if _history_runtime_record_has_duplicate(record, skip_runtime_identities):
            continue
        role = _record_role(record)
        content = str(record.get("content") or "")
        if role in {"system", "tool"} or _runtime_event_summary(content) is not None:
            result.append(record)
            continue
        if role == "assistant" and not _record_is_qq_visible_outbound(record):
            if content.strip() or record.get("tool_calls") or record.get("reasoning_content"):
                result.append(record)
    return result


def _event_store_runtime_duplicate_identities(records: list[dict]) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for record in records:
        if record.get("_source") != "event_store":
            continue
        event_type = str(record.get("_runtime_event_type") or "")
        if not event_type:
            continue
        result.update(_runtime_record_duplicate_identities(record))
    return result


def _history_runtime_record_has_duplicate(
    record: dict[str, Any],
    skip_runtime_identities: set[tuple[str, ...]],
) -> bool:
    if not skip_runtime_identities:
        return False
    role = _record_role(record)
    if role == "assistant" and record.get("tool_calls"):
        call_ids = [
            str(tool_call.get("id") or "").strip()
            for tool_call in record.get("tool_calls") or []
            if isinstance(tool_call, dict) and str(tool_call.get("id") or "").strip()
        ]
        return bool(call_ids) and all(("tool_call", call_id) in skip_runtime_identities for call_id in call_ids)
    return bool(_runtime_record_duplicate_identities(record) & skip_runtime_identities)


def _runtime_record_duplicate_identities(record: dict[str, Any]) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    event_type = str(record.get("_runtime_event_type") or "")
    role = _record_role(record)
    if event_type == "tool_call_started" or (role == "assistant" and record.get("tool_calls")):
        for tool_call in record.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = str(tool_call.get("id") or "").strip()
            if tool_call_id:
                result.add(("tool_call", tool_call_id))
    if event_type == "tool_result_received" or role == "tool":
        tool_call_id = str(record.get("tool_call_id") or "").strip()
        if tool_call_id:
            result.add(("tool_result", tool_call_id))
    if event_type == "system_note_recorded" or (not event_type and role == "system"):
        identity = _system_note_duplicate_identity(record)
        if identity:
            result.add(identity)
    for send_id in _send_runtime_duplicate_ids(record):
        result.add(("send", send_id))
    return result


def _system_note_duplicate_identity(record: dict[str, Any]) -> tuple[str, ...] | None:
    payload = record.get("metadata", {}).get("event_payload") if isinstance(record.get("metadata"), dict) else {}
    conversation_id = _record_conversation_id_for_display(record)
    if isinstance(payload, dict):
        content_hash = _optional_record_text(payload.get("content_hash"))
        content_length = _optional_record_text(payload.get("content_length"))
        payload_conversation_id = _optional_record_text(payload.get("conversation_id"))
        if content_hash and content_length:
            return (
                "system_note",
                payload_conversation_id or conversation_id,
                content_hash,
                content_length,
            )
    if _record_role(record) != "system":
        return None
    content = str(record.get("content") or "")
    if not content:
        return None
    return (
        "system_note",
        conversation_id,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        str(len(content)),
    )


def _send_runtime_duplicate_ids(record: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    payload = record.get("metadata", {}).get("event_payload") if isinstance(record.get("metadata"), dict) else {}
    if isinstance(payload, dict):
        for key in ("send_id", "send_attempt_id", "attempt_id"):
            value = _optional_record_text(payload.get(key))
            if value:
                ids.add(value)
    for key in ("send_id", "send_attempt_id", "attempt_id"):
        value = _optional_record_text(record.get(key))
        if value:
            ids.add(value)
    content = str(record.get("content") or "")
    send_status = _send_status_info(content)
    if send_status and send_status.get("send_id"):
        ids.add(str(send_status["send_id"]))
    for tag in ("send_receipt", "send_status"):
        tag_payload = _extract_tag_json(content, tag)
        if not tag_payload:
            continue
        for key in ("send_id", "send_attempt_id", "attempt_id"):
            value = _optional_record_text(tag_payload.get(key))
            if value:
                ids.add(value)
    for pattern in (r"\bsend_id=([^\s,;，。]+)", r"\bsend_attempt_id=([^\s,;，。]+)", r"\battempt_id=([^\s,;，。]+)"):
        for match in re.finditer(pattern, content):
            ids.add(match.group(1).strip())
    payload_content = _parse_json_object(content)
    if payload_content:
        for key in ("send_id", "send_attempt_id", "attempt_id"):
            value = _optional_record_text(payload_content.get(key))
            if value:
                ids.add(value)
    return ids


def _tag_record_order(records: list[dict]) -> list[dict]:
    return [{**record, "_record_order": index} for index, record in enumerate(records)]


def _real_record_identity(record: dict[str, Any]) -> tuple[str, str, str] | None:
    msg_id = _record_message_id(record)
    if not msg_id:
        return None
    conversation_id = _record_conversation_id_for_display(record)
    if not conversation_id:
        return None
    direction = str(record.get("direction") or "")
    if not direction:
        direction = "outbound" if _record_role(record) == "assistant" else "inbound"
    return conversation_id, direction, msg_id


async def _load_archive_records_paged(archive: Any) -> list[dict]:
    filter_records = getattr(archive, "filter_records", None)
    if not callable(filter_records):
        return list(await archive.records() or [])

    records: list[dict] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        page = await filter_records(
            {
                "limit": ARCHIVE_FETCH_PAGE_SIZE,
                "offset": offset,
                "order": "asc",
            }
        )
        if not isinstance(page, dict):
            break
        raw_results = page.get("results") or []
        results = [item for item in raw_results if isinstance(item, dict)]
        if not results:
            break
        records.extend(await _full_archive_records_for_page(archive, results))
        offset += len(results)
        total_value = page.get("total")
        total = int(total_value) if isinstance(total_value, int | str) and str(total_value).isdigit() else offset
    return records


async def _full_archive_records_for_page(archive: Any, results: list[dict]) -> list[dict]:
    ids = [str(item.get("id") or "").strip() for item in results if item.get("id")]
    get_by_ids = getattr(archive, "get_by_ids", None)
    if ids and callable(get_by_ids):
        try:
            full_records = await get_by_ids(ids)
        except Exception as e:
            logger.warning(f"按归档 ID 还原完整记录失败，使用轻量记录: {e}")
        else:
            by_id = {
                str(item.get("archive_id") or item.get("id") or ""): item
                for item in full_records or []
                if isinstance(item, dict)
            }
            ordered = [by_id[archive_id] for archive_id in ids if archive_id in by_id]
            if len(ordered) == len(ids):
                return ordered
    return [_light_archive_result_to_record(item) for item in results]


def _light_archive_result_to_record(item: dict[str, Any]) -> dict[str, Any]:
    direction = str(item.get("direction") or "")
    role = "assistant" if direction == "outbound" else "user"
    conversation_id = item.get("conversation_id")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "role": role,
        "content": str(item.get("content") or ""),
        "conversation_id": str(conversation_id) if conversation_id else None,
        "archive_id": item.get("id"),
        "timestamp": item.get("time"),
        "direction": direction,
        "sender_id": item.get("sender_id"),
        "sender_name": item.get("sender_name"),
        "metadata": {
            **metadata,
            "direction": direction,
            "sender_id": item.get("sender_id"),
            "sender_name": item.get("sender_name"),
        },
    }


def _runtime_event_summary(content: str) -> tuple[str, str] | None:
    if not content:
        return None
    if "<send_receipt_task" in content:
        return "发送回执任务", _compact_inline_tokens(_first_nonempty_line(content))
    if "<send_status" in content:
        return "发送状态", _format_send_status_summary(content)
    if "<send_receipt" in content:
        return "发送回执", _format_send_receipt_summary(content)
    if "<task_context" in content:
        return "运行时上下文", _format_task_context_summary(content)
    return None
