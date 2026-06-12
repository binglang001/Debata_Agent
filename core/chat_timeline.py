"""真实 QQ 可见聊天时间线。

该模块只记录 QQ 上真实可见的消息：
    - 入站消息：通过白名单/限速后进入 pipeline 的消息。
    - 出站消息：adapter 真实发送成功并返回 msg_id 后的消息。

Agent 工具参数、stale/unsent 草稿、system note 和模型思考不进入这里。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from adapters.types import IncomingMessage, MediaSegment, MediaType
from utils import get_time

_PARAM_SPLIT_RE = re.compile(r",(?=\w+=)")
_CQ_RE = re.compile(r"\[CQ:(?P<body>[^\]]+)\]")


@dataclass(slots=True)
class ChatTimelineMessage:
    conversation_id: str
    direction: Literal["inbound", "outbound"]
    timestamp: float
    time_text: str
    sender_name: str
    sender_id: str
    target_id: str | None
    group_id: str | None
    msg_id: str | None
    text: str
    raw_message: str
    cq_segments: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    source: Literal["qq"] = "qq"


class ChatTimelineStore:
    """每会话滑动窗口，默认保留最近 1000 条真实 QQ 消息。"""

    def __init__(self, *, max_per_conversation: int = 1000) -> None:
        self.max_per_conversation = max(1, int(max_per_conversation))
        self._items: dict[str, deque[ChatTimelineMessage]] = {}

    def append(self, message: ChatTimelineMessage) -> None:
        bucket = self._items.get(message.conversation_id)
        if bucket is None:
            bucket = deque(maxlen=self.max_per_conversation)
            self._items[message.conversation_id] = bucket
        bucket.append(message)

    def append_inbound_event(
        self,
        event: IncomingMessage,
        *,
        conversation_id: str,
        text: str,
        timestamp: float | None = None,
    ) -> None:
        timestamp = float(timestamp if timestamp is not None else time.time())
        raw_message = event.raw_message or text
        self.append(
            ChatTimelineMessage(
                conversation_id=conversation_id,
                direction="inbound",
                timestamp=timestamp,
                time_text=_format_time(timestamp),
                sender_name=event.nickname or event.user_id,
                sender_id=event.user_id,
                target_id=event.group_id if event.is_group() else event.user_id,
                group_id=event.group_id,
                msg_id=event.message_id,
                text=text,
                raw_message=raw_message,
                cq_segments=_parse_cq_segments(raw_message),
                attachments=_attachments_from_event(event, raw_message),
            )
        )

    def append_outbound_action(
        self,
        action: dict[str, Any],
        *,
        conversation_id: str,
        msg_id: str | None,
        self_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        timestamp = float(timestamp if timestamp is not None else time.time())
        target_scope = str(action.get("target_scope") or "")
        target_id = str(action.get("target_id") or "")
        raw_message = str(action.get("content") or "")
        label = str(action.get("label") or raw_message)
        sender_id = str(self_id or "self")
        sender_name = "我"
        self.append(
            ChatTimelineMessage(
                conversation_id=conversation_id,
                direction="outbound",
                timestamp=timestamp,
                time_text=_format_time(timestamp),
                sender_name=sender_name,
                sender_id=sender_id,
                target_id=target_id,
                group_id=target_id if target_scope == "group" else None,
                msg_id=str(msg_id) if msg_id is not None else None,
                text=label,
                raw_message=raw_message,
                cq_segments=_parse_cq_segments(raw_message),
                attachments=_attachments_from_raw(raw_message),
            )
        )

    def recent(
        self,
        conversation_id: str,
        limit: int,
        *,
        since_msg_id: str | None = None,
        before_msg_id: str | None = None,
    ) -> list[ChatTimelineMessage]:
        items = list(self._items.get(conversation_id) or [])
        if before_msg_id:
            items = _items_before_msg_id(items, before_msg_id)
        if since_msg_id:
            items = _items_since_msg_id(items, since_msg_id)
        limit = max(1, min(self.max_per_conversation, int(limit)))
        return items[-limit:]

    def to_markdown(
        self,
        messages: list[ChatTimelineMessage],
        *,
        include_raw: bool = False,
    ) -> str:
        lines: list[str] = []
        for message in messages:
            content = _timeline_content(message)
            suffix = f" [msg_id={message.msg_id}]" if message.msg_id else ""
            lines.append(
                f"{message.time_text} {message.sender_name}({message.sender_id})："
                f"{content}{suffix}"
            )
            if include_raw and message.raw_message and message.raw_message != content:
                lines.append(f"    raw: {message.raw_message}")
        return "\n".join(lines)


def _format_time(timestamp: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return get_time()


def _items_before_msg_id(
    items: list[ChatTimelineMessage],
    msg_id: str,
) -> list[ChatTimelineMessage]:
    for index, item in enumerate(items):
        if str(item.msg_id or "") == str(msg_id):
            return items[:index]
    return items


def _items_since_msg_id(
    items: list[ChatTimelineMessage],
    msg_id: str,
) -> list[ChatTimelineMessage]:
    for index, item in enumerate(items):
        if str(item.msg_id or "") == str(msg_id):
            return items[index + 1 :]
    return items


def _timeline_content(message: ChatTimelineMessage) -> str:
    text = (message.text or "").strip()
    if message.attachments:
        attachment_texts = [
            rendered
            for item in message.attachments
            if (rendered := _format_attachment_if_missing(item, text))
        ]
        if text and attachment_texts:
            return f"{text} {' '.join(attachment_texts)}"
        if text:
            return text
        return " ".join(_format_attachment(item) for item in message.attachments)
    if text:
        return text
    return "(空消息)"


def _attachments_from_event(
    event: IncomingMessage,
    raw_message: str,
) -> list[dict[str, Any]]:
    attachments = [_attachment_from_media(seg) for seg in event.media]
    attachments.extend(_attachments_from_raw(raw_message))
    return _dedupe_attachments([item for item in attachments if item])


def _attachment_from_media(seg: MediaSegment) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": _media_type_value(seg.type),
    }
    if seg.url:
        item["url"] = seg.url
    if seg.file_id:
        if item["type"] == "forward":
            item["forward_id"] = seg.file_id
        else:
            item["file"] = seg.file_id
    if seg.name:
        item["name"] = seg.name
    if seg.extra:
        item["extra"] = dict(seg.extra)
        if seg.extra.get("summary"):
            item["summary"] = seg.extra.get("summary")
    return item


def _media_type_value(value: MediaType | str) -> str:
    if isinstance(value, MediaType):
        return value.value
    return str(value)


def _attachments_from_raw(raw_message: str) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for segment in _parse_cq_segments(raw_message):
        cq_type = str(segment.get("type") or "")
        params = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if cq_type == "image":
            item = _attachment_from_params("image", params, segment.get("raw"))
        elif cq_type == "face":
            item = _attachment_from_params("face", params, segment.get("raw"))
        elif cq_type == "file":
            item = _attachment_from_params("file", params, segment.get("raw"))
        elif cq_type in {"record", "voice"}:
            item = _attachment_from_params("voice", params, segment.get("raw"))
        elif cq_type == "forward":
            item = _attachment_from_params("forward", params, segment.get("raw"))
            if params.get("id"):
                item["forward_id"] = params.get("id")
        else:
            continue
        attachments.append(item)
    return attachments


def _attachment_from_params(
    item_type: str,
    params: dict[str, str],
    raw: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {"type": item_type}
    for src, dst in (
        ("summary", "summary"),
        ("url", "url"),
        ("file", "file"),
        ("file_id", "file"),
        ("id", "id"),
        ("name", "name"),
    ):
        if params.get(src):
            item[dst] = params[src]
    if raw:
        item["raw"] = str(raw)
    return item


def _parse_cq_segments(raw_message: str) -> list[dict[str, Any]]:
    if not raw_message:
        return []
    segments: list[dict[str, Any]] = []
    for match in _CQ_RE.finditer(raw_message):
        body = match.group("body")
        if "," in body:
            cq_type, params_str = body.split(",", 1)
        else:
            cq_type, params_str = body, ""
        raw = match.group(0)
        segments.append(
            {
                "type": cq_type,
                "data": _parse_cq_params(params_str),
                "raw": raw,
            }
        )
    return segments


def _parse_cq_params(params_str: str) -> dict[str, str]:
    if not params_str:
        return {}
    params: dict[str, str] = {}
    for part in _PARAM_SPLIT_RE.split(params_str):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key] = value
    return params


def _dedupe_attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        key = (
            str(item.get("type") or ""),
            str(item.get("url") or ""),
            str(item.get("file") or ""),
            str(item.get("forward_id") or item.get("id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _format_attachment(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "unknown")
    parts = [item_type]
    for key in ("summary", "url", "file", "forward_id", "name", "id"):
        value = item.get(key)
        if value:
            parts.append(f"{key}={value}")
    return "[" + " ".join(parts) + "]"


def _format_attachment_if_missing(item: dict[str, Any], text: str) -> str:
    """只补充正文里没有体现的关键附件信息，避免重复刷屏。"""
    for key in ("url", "file", "forward_id", "id"):
        value = item.get(key)
        if value and str(value) not in text:
            return _format_attachment(item)
    return ""
