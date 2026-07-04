"""对话页显示模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DisplayKind = Literal[
    "inbound_message",
    "outbound_message",
    "assistant_note",
    "tool_call",
    "tool_result",
    "system_event",
    "runtime_receipt",
    "reasoning",
]
DisplaySeverity = Literal["normal", "info", "warning", "error"]


@dataclass(slots=True)
class DisplayItem:
    item_id: str
    conversation_id: str
    timestamp: str | None
    kind: DisplayKind
    speaker_label: str | None
    speaker_id: str | None
    role_label: str
    text: str
    summary: str
    raw: dict[str, Any]
    related_tool_call_id: str | None = None
    related_message_id: str | None = None
    collapsed_by_default: bool = False
    severity: DisplaySeverity = "normal"
    tool_results: list[DisplayItem] = field(default_factory=list)


@dataclass(slots=True)
class SendDisplayContext:
    accepted_by_send_id: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    real_msg_ids: set[str] = field(default_factory=set)
    real_send_orders: set[tuple[str, int]] = field(default_factory=set)
    generated_msg_ids: set[str] = field(default_factory=set)
    generated_send_orders: set[tuple[str, int]] = field(default_factory=set)


@dataclass(slots=True)
class ConversationDisplayCache:
    conversation_key: str
    records_signature: tuple[int, str]
    persona_name: str
    normalized_items: list[DisplayItem]
    filter_signature: tuple[str, bool, bool, bool, bool, bool] | None = None
    filtered_items: list[DisplayItem] = field(default_factory=list)
