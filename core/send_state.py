"""发送管理器内部状态模型。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class _InboundRef:
    seq: int
    conversation_id: str
    message_id: str
    user_id: str
    nickname: str
    text: str
    reply_to: str | None
    self_id: str
    received_at: float


@dataclass(slots=True)
class _SendJob:
    send_id: str
    conversation_id: str
    actions: list[dict[str, Any]]
    source_tool: str
    interrupt_policy: str
    ignore_review_interrupts: bool
    trigger_message_id: str | None
    trigger_inbound_seq: int
    trigger_user_id: str | None
    created_at: float


@dataclass(slots=True)
class _SendConversationState:
    queue: deque[_SendJob] = field(default_factory=deque)
    worker: asyncio.Task | None = None
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_messages: list[dict[str, Any]] = field(default_factory=list)
    recall_events: list[dict[str, Any]] = field(default_factory=list)
    pending_receipts: list[dict[str, Any]] = field(default_factory=list)
    needs_resync: bool = False
    in_flight: bool = False
    active_interrupt_policy: str = "interrupt_all"
    active_ignore_review_interrupts: bool = False
    deferred_queue_interrupt_pending: bool = False
    active_trigger_user_id: str | None = None
    active_trigger_message_id: str | None = None


@dataclass(slots=True)
class _SendAttempt:
    send_attempt_id: str
    conversation_ids: list[str]
    actions: list[dict[str, Any]]
    source_tool: str
    trigger_message_id: str | None
    trigger_inbound_seq: int
    trigger_user_id: str | None
    focus_user_ids: list[str]
    trigger_message_ids: list[str]
    reviewed_until_seq: int
    review_policy: str
    delivery_interrupt_policy: str
    tool_call_id: str | None
    reason: str | None
    created_at: float
    revision: int = 1
    consumed: bool = False
