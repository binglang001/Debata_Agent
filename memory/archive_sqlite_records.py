"""sqlite archive 记录归一化与序列化 helper。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archive_sqlite_models import _NormalizedRecord


def _normalize_archive_path(path: Path) -> Path:
    if path.name == "archive.jsonl":
        return path.with_name("archive.sqlite3")
    if path.suffix.lower() == ".jsonl":
        return path.with_suffix(".sqlite3")
    return path


def real_chat_archive_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    """把运行时记录转换成可入永久归档的真实 QQ 聊天记录。"""
    outbound = _outbound_records_from_tool_result(record)
    if outbound:
        return outbound
    copied = dict(record)
    if is_real_chat_record(copied):
        return [copied]
    return []


def is_real_chat_record(record: dict[str, Any]) -> bool:
    normalized = _normalize_record(record)
    return _normalized_is_real_chat(normalized)


def _normalize_record(record: dict[str, Any]) -> _NormalizedRecord:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    metadata = dict(metadata or {})
    role = str(record.get("role") or "").strip() or "user"
    content = _clean_display_content(_record_content_text(record.get("content")))
    content_search = _clean_search_content(content)
    timestamp = _extract_timestamp(record, metadata)
    timestamp_unix, date_key, month_key = _timestamp_parts(timestamp)
    conversation_id = _extract_conversation_id(record, metadata)
    conversation_type, target_id = _conversation_parts(conversation_id, metadata)
    sender_id, sender_name = _sender_parts(record, metadata, role)
    direction = _direction_for(record, metadata, role)
    message_kind = _message_kind_for(content, record, metadata, role)
    if direction == "runtime":
        message_kind = "runtime"
    normalized_record = dict(record)
    normalized_record["content"] = content
    if conversation_id:
        normalized_record["conversation_id"] = conversation_id
    normalized_record["metadata"] = metadata
    return _NormalizedRecord(
        role=role,
        timestamp=timestamp,
        timestamp_unix=timestamp_unix,
        date_key=date_key,
        month_key=month_key,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        target_id=target_id,
        sender_id=sender_id,
        sender_name=sender_name,
        sender_role=role,
        direction=direction,
        message_kind=message_kind,
        content=content,
        content_search=content_search,
        original_msg_id=_extract_original_msg_id(record, metadata),
        reply_to_msg_id=_clean_optional(record.get("reply_to_msg_id"))
        or _clean_optional(metadata.get("reply_to_message_id")),
        metadata=metadata,
        record=normalized_record,
    )


def _normalized_is_real_chat(record: _NormalizedRecord) -> bool:
    if record.role not in _REAL_CHAT_ROLES:
        return False
    if record.direction not in _REAL_CHAT_DIRECTIONS:
        return False
    if record.message_kind not in _REAL_CHAT_MESSAGE_KINDS:
        return False
    if not record.content:
        return False
    if _conversation_is_runtime(record.conversation_id):
        return False
    if _metadata_is_runtime(record.metadata) or _text_is_runtime(record.content):
        return False
    raw = record.record
    if raw.get("tool_calls"):
        return False
    if raw.get("reasoning_content") or raw.get("reasoning_blocks"):
        return False
    if record.role == "assistant" and not _assistant_has_outbound_proof(
        raw,
        record.metadata,
    ):
        return False
    return True


def _row_is_real_chat(row: sqlite3.Row) -> bool:
    if str(row["sender_role"] or "") not in _REAL_CHAT_ROLES:
        return False
    if str(row["direction"] or "") not in _REAL_CHAT_DIRECTIONS:
        return False
    if str(row["message_kind"] or "") not in _REAL_CHAT_MESSAGE_KINDS:
        return False
    if not str(row["content"] or "").strip():
        return False
    if _conversation_is_runtime(_clean_optional(row["conversation_id"])):
        return False
    if _text_is_runtime(str(row["content"] or "")):
        return False
    metadata = _json_loads(row["metadata_json"], default={})
    if isinstance(metadata, dict) and _metadata_is_runtime(metadata):
        return False
    record = _json_loads(row["record_json"], default={})
    if isinstance(record, dict):
        role = str(record.get("role") or row["sender_role"] or "")
        if role not in _REAL_CHAT_ROLES:
            return False
        if record.get("tool_calls"):
            return False
        if record.get("reasoning_content") or record.get("reasoning_blocks"):
            return False
        if role == "assistant" and not _assistant_has_outbound_proof(record, metadata):
            return False
    elif str(row["sender_role"] or "") == "assistant":
        if not _assistant_has_outbound_proof({}, metadata):
            return False
    return True


def _outbound_records_from_tool_result(record: dict[str, Any]) -> list[dict[str, Any]]:
    if record.get("role") != "tool":
        return []
    try:
        payload = json.loads(str(record.get("content") or "{}"))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    sent_items = payload.get("sent")
    if not isinstance(sent_items, list):
        return []

    result: list[dict[str, Any]] = []
    for item in sent_items:
        if not isinstance(item, dict) or item.get("qq_visible") is not True:
            continue
        content = _clean_optional(item.get("content"))
        if not content:
            continue
        conversation_id = _clean_optional(item.get("conversation_id"))
        if conversation_id is None:
            target_type = _clean_optional(item.get("target_type"))
            target_id = _clean_optional(item.get("target_id"))
            if target_type and target_id:
                conversation_id = f"{target_type}:{target_id}"
        if _conversation_is_runtime(conversation_id):
            continue
        msg_id = _clean_optional(item.get("msg_id"))
        metadata = {
            "timestamp": _clean_optional(item.get("time")),
            "qq_visible": True,
            "source": "send_result",
            "tool_call_id": _clean_optional(record.get("tool_call_id")),
            "send_id": _clean_optional(payload.get("send_id")),
        }
        result.append(
            {
                "role": "assistant",
                "content": content,
                "conversation_id": conversation_id,
                "original_msg_id": msg_id,
                "metadata": {
                    key: value for key, value in metadata.items() if value is not None
                },
            }
        )
    return result


def _assistant_has_outbound_proof(
    record: dict[str, Any],
    metadata: Any,
) -> bool:
    if not isinstance(metadata, dict):
        metadata = {}
    record_source = _clean_optional(record.get("source"))
    metadata_source = _clean_optional(metadata.get("source"))
    record_visible = record.get("qq_visible") is True
    metadata_visible = metadata.get("qq_visible") is True
    return (
        record_visible and record_source == "send_result"
    ) or (
        metadata_visible and metadata_source == "send_result"
    )


def _row_to_record(row: sqlite3.Row) -> dict:
    record = _json_loads(row["record_json"], default={})
    if not isinstance(record, dict):
        record = {}
    metadata = _json_loads(row["metadata_json"], default={})
    if not isinstance(metadata, dict):
        metadata = {}
    record["role"] = record.get("role") or row["sender_role"] or "user"
    record["content"] = row["content"] or ""
    record["conversation_id"] = row["conversation_id"]
    record["metadata"] = metadata
    record["archive_id"] = row["archive_id"]
    return record


def _row_to_light_result(row: sqlite3.Row) -> dict[str, Any]:
    sender = str(row["sender_name"] or row["sender_id"] or row["sender_role"] or "-")
    if row["sender_name"] and row["sender_id"]:
        sender = f"{row['sender_name']}({row['sender_id']})"
    return {
        "id": row["archive_id"],
        "time": row["timestamp"],
        "conversation_id": row["conversation_id"],
        "sender": sender,
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "direction": row["direction"],
        "kind": row["message_kind"],
        "content": row["content"],
        "metadata": {
            "date": row["date_key"],
            "month": row["month_key"],
            "conversation_type": row["conversation_type"],
            "target_id": row["target_id"],
            "original_msg_id": row["original_msg_id"],
        },
    }


def _legacy_search_text(row: sqlite3.Row) -> str:
    return "\n".join(
        str(value or "")
        for value in (
            row["timestamp"],
            row["date_key"],
            row["month_key"],
            row["conversation_id"],
            row["sender_id"],
            row["sender_name"],
            row["content_search"],
            row["original_msg_id"],
            row["reply_to_msg_id"],
        )
    )


def _record_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False, default=str).strip()
    except TypeError:
        return str(content).strip()


def _clean_display_content(text: str) -> str:
    text = _normalize_media_placeholders(text)
    text = _MEDIA_URL_ATTR_PATTERN.sub("", text)
    text = re.sub(r"\s+\]", "]", text)
    return text.strip()


def _clean_search_content(text: str) -> str:
    text = _URL_PATTERN.sub("[链接]", text)
    text = _WINDOWS_PATH_PATTERN.sub("[本地路径]", text)
    text = _SECRET_QUERY_PATTERN.sub("", text)
    text = re.sub(r"\[链接\](?:\s*\[链接\])+", "[链接]", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _normalize_media_placeholders(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        kind = match.group(1)
        attrs = match.group(2) or ""
        workspace = _extract_attr(attrs, "workspace")
        if kind == "音频消息":
            transcript = _MEDIA_RUNTIME_ATTR_PATTERN.sub("", attrs).strip()
            transcript = re.sub(r"^[:：]\s*", "", transcript).strip()
            if transcript:
                return (
                    f"[音频消息: {transcript} workspace={workspace}]"
                    if workspace
                    else f"[音频消息: {transcript}]"
                )
            return f"[音频消息 workspace={workspace}]" if workspace else "[音频消息]"
        return f"[{kind} workspace={workspace}]" if workspace else f"[{kind}]"

    return _MEDIA_PLACEHOLDER_PATTERN.sub(repl, text)


def _extract_media(content: Any) -> list[dict[str, Any]]:
    text = _record_content_text(content)
    result: list[dict[str, Any]] = []
    for match in _MEDIA_PLACEHOLDER_PATTERN.finditer(text):
        attrs = match.group(2) or ""
        workspace = _extract_attr(attrs, "workspace")
        if not workspace:
            continue
        media_type = {
            "图片": "image",
            "文件": "file",
            "音频消息": "audio",
        }.get(match.group(1), "media")
        result.append(
            {
                "media_type": media_type,
                "workspace_path": workspace,
                "original_name": _extract_attr(attrs, "name")
                or _extract_attr(attrs, "file_name"),
                "metadata": {"raw": match.group(0)},
            }
        )
    return result


def _extract_attr(attrs: str, name: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(name)}=([^\]\s]+)", attrs or "")
    return match.group(1).strip() if match else ""


def _extract_timestamp(record: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    for value in (record.get("timestamp"), metadata.get("timestamp")):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned
    messages = metadata.get("messages")
    if isinstance(messages, list) and messages:
        for item in messages:
            if isinstance(item, dict):
                cleaned = _clean_optional(item.get("timestamp"))
                if cleaned:
                    return cleaned
    return None


def _timestamp_parts(timestamp: str | None) -> tuple[int | None, str | None, str | None]:
    if not timestamp:
        return None, None, None
    dt = _parse_datetime(timestamp)
    if dt is None:
        return None, None, None
    return int(dt.timestamp()), dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")


def _parse_datetime(value: str) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError):
            return None
    normalized = text.replace("T", " ").replace("Z", "+00:00")
    candidates = [
        normalized,
        normalized[:19],
        normalized[:16],
        normalized[:10],
    ]
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ]
    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                pass
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _extract_conversation_id(record: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    direct = _clean_optional(record.get("conversation_id"))
    if direct:
        return direct
    message = _first_meta_message(metadata)
    if message:
        scope = _clean_optional(message.get("scope"))
        target_id = _clean_optional(message.get("target_id"))
        group_id = _clean_optional(message.get("group_id"))
        user_id = _clean_optional(message.get("user_id"))
        if scope == "group" and (group_id or target_id):
            return f"group:{group_id or target_id}"
        if scope == "private" and (target_id or user_id):
            return f"private:{target_id or user_id}"
        if group_id:
            return f"group:{group_id}"
        if user_id:
            return f"private:{user_id}"
    scope = _clean_optional(metadata.get("scope"))
    target_id = _clean_optional(metadata.get("target_id"))
    group_id = _clean_optional(metadata.get("group_id"))
    user_id = _clean_optional(metadata.get("user_id"))
    if scope == "group" and (group_id or target_id):
        return f"group:{group_id or target_id}"
    if scope == "private" and (target_id or user_id):
        return f"private:{target_id or user_id}"
    if group_id:
        return f"group:{group_id}"
    if user_id:
        return f"private:{user_id}"
    return None


def _conversation_parts(
    conversation_id: str | None,
    metadata: dict[str, Any],
) -> tuple[str, str | None]:
    if conversation_id and ":" in conversation_id:
        conversation_type, target_id = conversation_id.split(":", 1)
        return conversation_type or "unknown", target_id or None
    message = _first_meta_message(metadata)
    if message:
        scope = _clean_optional(message.get("scope"))
        target_id = (
            _clean_optional(message.get("target_id"))
            or _clean_optional(message.get("group_id"))
            or _clean_optional(message.get("user_id"))
        )
        return scope or "unknown", target_id
    return "unknown", None


def _sender_parts(
    record: dict[str, Any],
    metadata: dict[str, Any],
    role: str,
) -> tuple[str | None, str | None]:
    message = _first_meta_message(metadata)
    if role == "user" and message:
        return (
            _clean_optional(message.get("user_id")),
            _clean_optional(message.get("nickname")),
        )
    if role == "assistant":
        return _clean_optional(record.get("sender_id")) or "assistant", "assistant"
    if role in {"system", "tool"}:
        return role, role
    return (
        _clean_optional(record.get("sender_id")) or _clean_optional(metadata.get("sender_id")),
        _clean_optional(record.get("sender_name")) or _clean_optional(metadata.get("sender_name")),
    )


def _direction_for(record: dict[str, Any], metadata: dict[str, Any], role: str) -> str:
    if _metadata_is_runtime(metadata) or _text_is_runtime(_record_content_text(record.get("content"))):
        return "runtime"
    if role == "user":
        return "inbound"
    if (
        role == "assistant"
        and not record.get("tool_calls")
        and _assistant_has_outbound_proof(record, metadata)
    ):
        return "outbound"
    if role in {"assistant", "system", "tool"}:
        return "runtime"
    return "unknown"


def _message_kind_for(
    content: str,
    record: dict[str, Any],
    metadata: dict[str, Any],
    role: str,
) -> str:
    if role in {"system", "tool"} or record.get("tool_calls") or _metadata_is_runtime(metadata):
        return "runtime"
    kinds: set[str] = set()
    if "[图片" in content:
        kinds.add("image")
    if "[文件" in content:
        kinds.add("file")
    if "[音频消息" in content:
        kinds.add("audio")
    if "[合并转发" in content:
        kinds.add("forward")
    if not kinds:
        return "text"
    return next(iter(kinds)) if len(kinds) == 1 else "mixed"


def _extract_original_msg_id(record: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    for value in (record.get("original_msg_id"), record.get("message_id"), record.get("msg_id")):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned
    message = _first_meta_message(metadata)
    if message:
        return _clean_optional(message.get("message_id"))
    return None


def _first_meta_message(metadata: dict[str, Any]) -> dict[str, Any] | None:
    messages = metadata.get("messages")
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict):
                return item
    return None


def _metadata_is_runtime(metadata: dict[str, Any]) -> bool:
    return metadata.get("kind") in _RUNTIME_METADATA_KINDS


def _text_is_runtime(text: str) -> bool:
    return any(marker in text for marker in _RUNTIME_CONTEXT_MARKERS)


def _conversation_is_runtime(conversation_id: str | None) -> bool:
    value = str(conversation_id or "").strip().lower()
    return value.startswith(("system:", "runtime:", "internal:"))


def _query_to_dict(query: Any) -> dict[str, Any]:
    if query is None:
        return {}
    if isinstance(query, dict):
        return dict(query)
    model_dump = getattr(query, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True))
    return dict(getattr(query, "__dict__", {}) or {})


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        cleaned = _clean_optional(item)
        if cleaned:
            result.append(cleaned)
    return result


def _time_range_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append(dict(item))
            continue
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            result.append(dict(model_dump(exclude_none=True)))
    return result


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _clean_id(value: Any) -> str:
    return _clean_optional(value).lower()


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: Any, *, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value <= 0:
        return "0"
    digits: list[str] = []
    while value:
        value, rest = divmod(value, 36)
        digits.append(alphabet[rest])
    return "".join(reversed(digits))


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_MEDIA_PLACEHOLDER_PATTERN = re.compile(
    r"\[(图片|文件|音频消息)([^\]]*(?:url=|workspace=)[^\]]*)\]"
)
_MEDIA_URL_ATTR_PATTERN = re.compile(r"\surl=(?:[^\]\s]+)")
_MEDIA_RUNTIME_ATTR_PATTERN = re.compile(r"\s(?:url|workspace)=(?:[^\]\s]+)")
_URL_PATTERN = re.compile(r"https?://[^\s\]）)>\"']+")
_WINDOWS_PATH_PATTERN = re.compile(r"(?<![\w/\\])[A-Za-z]:[\\/][^\s\]）)>\"']+")
_SECRET_QUERY_PATTERN = re.compile(r"(?i)(?:rkey|clientkey|skey|token)=[^&\s\]]+")
_REAL_CHAT_ROLES = frozenset({"user", "assistant"})
_REAL_CHAT_DIRECTIONS = frozenset({"inbound", "outbound"})
_REAL_CHAT_MESSAGE_KINDS = frozenset(
    {"text", "image", "file", "audio", "forward", "mixed"}
)
_RUNTIME_METADATA_KINDS = frozenset(
    {
        "task_context_snapshot",
        "send_done_snapshot",
        "send_receipt",
        "send_receipt_task",
    }
)
_RUNTIME_CONTEXT_MARKERS = (
    "<task_context",
    "</task_context>",
    "<send_status",
    "</send_status>",
    "<send_receipt",
    "</send_receipt>",
    "<send_receipt_task",
    "</send_receipt_task>",
    "<tool_loop_final_warning",
    "</tool_loop_final_warning>",
    "<tool_loop_stop",
    "</tool_loop_stop>",
)
