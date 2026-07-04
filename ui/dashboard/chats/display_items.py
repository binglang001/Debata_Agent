"""对话页 DisplayItem 过滤、排序和媒体检测 helper。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .grouping import _LEGACY_HEADER_RE, _conversation_infos_from_payload
from .models import DisplayItem, SendDisplayContext
from .text_format import _parse_tool_arguments

CHAT_SORT_LAYER_RANK = {
    "archive": 0,
    "event_store": 1,
    "timeline": 2,
    "history": 3,
    "fallback": 4,
}

_CQ_MEDIA_RE = re.compile(r"\[CQ:(?:image|file|record|video)\b", re.I)
_MEDIA_OR_FILE_EXT_RE = re.compile(
    r"(?i)(?:^|[\s\"'=:/\\])[\w.\-()[\]/\\]+"
    r"\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|mov|mkv|webm|mp3|wav|ogg|flac|"
    r"pdf|docx?|xlsx?|pptx?|txt|md|zip|7z|rar|tar|gz|json|csv)\b"
)
_MEDIA_TOOL_NAMES = {
    "describe_image",
    "send_group_image",
    "send_private_image",
    "send_emoji",
    "upload_file",
    "read_file",
    "write_file",
    "edit_file",
    "get_forward_msg",
}


def _record_has_media_or_file(record: dict) -> bool:
    content = str(record.get("content") or "")
    if _text_has_media_or_file(content):
        return True

    meta = record.get("metadata")
    if isinstance(meta, dict) and _text_has_media_or_file(json.dumps(meta, ensure_ascii=False)):
        return True

    for tool_call in record.get("tool_calls") or []:
        func = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(func, dict):
            continue
        name = str(func.get("name") or "")
        if name in _MEDIA_TOOL_NAMES:
            return True
        if _text_has_media_or_file(json.dumps(_parse_tool_arguments(func.get("arguments")), ensure_ascii=False)):
            return True
    return False


def _text_has_media_or_file(text: str) -> bool:
    if not text:
        return False
    if _CQ_MEDIA_RE.search(text):
        return True
    if "[图片" in text or "[文件" in text or "[视频" in text or "[语音" in text:
        return True
    if _MEDIA_OR_FILE_EXT_RE.search(text):
        return True
    lowered = text.casefold()
    return any(
        marker in lowered
        for marker in (
            "workspace=incoming/",
            "workspace=incoming\\",
            "image_ref",
            "image_path",
            "image_url",
            "file_path",
            "file_name",
            "file_id",
            "forward_id",
        )
    )


def _should_attach_tool_result_to_call(item: DisplayItem) -> bool:
    if item.kind != "tool_result" or not item.related_tool_call_id:
        return False
    raw = item.raw if isinstance(item.raw, dict) else {}
    record = raw.get("record") if isinstance(raw.get("record"), dict) else raw
    return not (
        record.get("_source") == "event_store"
        and record.get("_runtime_event_type") == "tool_result_received"
    )


def _accepted_message_conversation_id(message: dict[str, Any], *, fallback: str) -> str:
    conversation_id = str(message.get("conversation_id") or "").strip()
    if conversation_id:
        return conversation_id
    infos = _conversation_infos_from_payload(message)
    if infos:
        return infos[0]["key"]
    return fallback


def _should_skip_generated_outbound(item: DisplayItem, context: SendDisplayContext) -> bool:
    if item.kind != "outbound_message" or not _is_generated_outbound(item):
        return False
    if item.related_message_id and (
        item.related_message_id in context.real_msg_ids
        or item.related_message_id in context.generated_msg_ids
    ):
        return True
    send_id = str(item.raw.get("send_id") or "").strip()
    order = _raw_send_order(item.raw)
    return bool(
        send_id
        and order is not None
        and ((send_id, order) in context.real_send_orders or (send_id, order) in context.generated_send_orders)
    )


def _remember_generated_outbound(item: DisplayItem, context: SendDisplayContext) -> None:
    if item.kind != "outbound_message" or not _is_generated_outbound(item):
        return
    if item.related_message_id:
        context.generated_msg_ids.add(item.related_message_id)
    send_id = str(item.raw.get("send_id") or "").strip()
    order = _raw_send_order(item.raw)
    if send_id and order is not None:
        context.generated_send_orders.add((send_id, order))


def _is_generated_outbound(item: DisplayItem) -> bool:
    return bool(item.raw.get("_synthetic_source"))


def _record_send_id(rec: dict[str, Any]) -> str | None:
    value = rec.get("send_id")
    if value:
        return str(value)
    meta = rec.get("metadata")
    if isinstance(meta, dict) and meta.get("send_id"):
        return str(meta.get("send_id"))
    return None


def _record_send_order(rec: dict[str, Any]) -> int | None:
    for source in (rec, rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}):
        order = _raw_send_order(source)
        if order is not None:
            return order
    return None


def _raw_send_order(raw: dict[str, Any]) -> int | None:
    for key in ("send_order", "order", "message_index", "index"):
        value = raw.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _unique_item_id(item_id: str, record_index: int, seen_ids: set[str]) -> str:
    if item_id not in seen_ids:
        seen_ids.add(item_id)
        return item_id
    unique = f"{item_id}:{record_index}"
    suffix = 1
    while unique in seen_ids:
        suffix += 1
        unique = f"{item_id}:{record_index}:{suffix}"
    seen_ids.add(unique)
    return unique


def _sort_display_items(items: list[DisplayItem]) -> list[DisplayItem]:
    return [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda pair: _display_item_sort_key(pair[1], pair[0]),
        )
    ]


def _display_item_sort_key(item: DisplayItem, fallback_order: int) -> tuple[int, int, float, int, int]:
    record = _display_item_record(item)
    layer = _record_sort_layer(record)
    record_order = _display_item_record_order(item, fallback_order=fallback_order)
    if layer is not None:
        sort_value = _record_sort_value_for_layer(record, layer)
        if sort_value is None:
            return CHAT_SORT_LAYER_RANK[layer], 1, 0.0, record_order, fallback_order
        return CHAT_SORT_LAYER_RANK[layer], 0, sort_value, record_order, fallback_order
    sort_ts = _display_item_sort_ts(item)
    if sort_ts is None:
        return CHAT_SORT_LAYER_RANK["fallback"], 1, 0.0, record_order, fallback_order
    return CHAT_SORT_LAYER_RANK["fallback"], 0, sort_ts, record_order, fallback_order


def _display_item_record(item: DisplayItem) -> dict[str, Any]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    return raw.get("record") if isinstance(raw.get("record"), dict) else raw


def _record_sort_layer(record: dict[str, Any]) -> str | None:
    layer = str(record.get("_sort_layer") or "").strip()
    if layer in CHAT_SORT_LAYER_RANK:
        return layer
    source = str(record.get("_source") or "").strip()
    if source == "event_store":
        return "event_store"
    if source == "chat_timeline":
        return "timeline"
    if source == "archive" or record.get("archive_id"):
        return "archive"
    return None


def _record_sort_value_for_layer(record: dict[str, Any], layer: str) -> float | None:
    if layer == "event_store":
        sort_value = _parse_float_value(record.get("_sort_value"))
        if sort_value is not None:
            return sort_value
        return _parse_float_value(record.get("event_id"))
    if layer in {"archive", "timeline", "history"}:
        sort_value = _parse_float_value(record.get("_sort_value"))
        if sort_value is not None:
            return sort_value
        return _record_timestamp_sort_value(record)
    return _parse_float_value(record.get("_sort_value"))


def _display_item_sort_ts(item: DisplayItem) -> float | None:
    record = _display_item_record(item)
    for value in (
        record.get("_sort_ts"),
        record.get("timestamp"),
        record.get("time"),
        record.get("created_at"),
        item.timestamp,
    ):
        parsed = _parse_timestamp_value(value)
        if parsed is not None:
            return parsed
    return None


def _display_item_record_order(item: DisplayItem, *, fallback_order: int) -> int:
    raw = item.raw
    record = raw.get("record") if isinstance(raw.get("record"), dict) else raw
    value = record.get("_record_order")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return fallback_order


def _record_timestamp_sort_value(record: dict[str, Any]) -> float | None:
    for value in (
        record.get("_sort_ts"),
        record.get("timestamp"),
        record.get("time"),
        record.get("created_at"),
    ):
        parsed = _parse_timestamp_value(value)
        if parsed is not None:
            return parsed
    meta = record.get("metadata")
    if isinstance(meta, dict):
        parsed = _parse_timestamp_value(meta.get("date"))
        if parsed is not None:
            return parsed
    content = str(record.get("content") or "")
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        return _parse_timestamp_value(match.group("timestamp"))
    return None


def _parse_float_value(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_timestamp_value(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _filter_display_items(
    items: list[DisplayItem],
    *,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    show_reasoning: bool = True,
    media_only: bool = False,
) -> list[DisplayItem]:
    query = search_text.strip().casefold()
    return [
        item
        for item in items
        if _display_item_matches_filters(
            item,
            query=query,
            show_chat=show_chat,
            show_system=show_system,
            show_tools=show_tools,
            show_reasoning=show_reasoning,
            media_only=media_only,
        )
    ]


def _display_item_matches_filters(
    item: DisplayItem,
    *,
    query: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    show_reasoning: bool = True,
    media_only: bool = False,
) -> bool:
    categories = _display_item_categories(item)
    if not show_chat and "chat" in categories:
        categories.discard("chat")
    if not show_system and "system" in categories:
        categories.discard("system")
    if not show_tools and "tool" in categories:
        categories.discard("tool")
    if not show_reasoning and "reasoning" in categories:
        categories.discard("reasoning")
    if not categories:
        return False
    if media_only and not _display_item_has_media_or_file(item):
        return False
    if not query:
        return True
    if query in _display_item_search_text(item).casefold():
        return True
    return item.kind == "tool_call" and any(
        query in _display_item_search_text(result).casefold()
        for result in item.tool_results
    )


def _display_item_categories(item: DisplayItem) -> set[str]:
    if item.kind in {"inbound_message", "outbound_message"}:
        return {"chat"}
    if item.kind in {"tool_call", "tool_result"}:
        return {"tool"}
    if item.kind == "reasoning":
        return {"reasoning"}
    return {"system"}


def _display_item_has_media_or_file(item: DisplayItem) -> bool:
    if _text_has_media_or_file(item.text) or _text_has_media_or_file(item.summary):
        return True
    if _text_has_media_or_file(json.dumps(item.raw, ensure_ascii=False, default=str)):
        return True
    return any(_display_item_has_media_or_file(result) for result in item.tool_results)


def _display_item_search_text(item: DisplayItem) -> str:
    parts = [
        item.kind,
        item.speaker_label or "",
        item.speaker_id or "",
        item.role_label,
        item.text,
        item.summary,
        item.related_tool_call_id or "",
        item.related_message_id or "",
        json.dumps(item.raw, ensure_ascii=False, default=str),
    ]
    for result in item.tool_results:
        parts.append(_display_item_search_text(result))
    return "\n".join(parts)
