"""Asynchronous outbound send manager for MessagePipeline.

This module is a mechanical split from `core.message_pipeline`. Keep behavior
equivalent; do not change send ordering, stale/review rules, or receipt shapes
while moving code.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from adapters.types import Target
from utils import get_time

from .state import PendingMessageItem

if TYPE_CHECKING:
    from .message_pipeline import MessagePipeline

logger = logging.getLogger("core.message_pipeline")


def _text_mentions_self_or_role(text: str, self_id: str, role_name: str) -> bool:
    content = str(text or "")
    cleaned_self_id = str(self_id or "").strip()
    if cleaned_self_id:
        tokens = (
            f"@{cleaned_self_id}",
            f"@QQ{cleaned_self_id}",
            f"[CQ:at,qq={cleaned_self_id}]",
        )
        if any(token in content for token in tokens):
            return True
    cleaned_role = str(role_name or "").strip()
    return bool(cleaned_role and f"@{cleaned_role}" in content)


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


class _AsyncSendManager:
    """Phase 0 每会话 FIFO 发送队列。

    工具调用只入队并立即返回；后台 worker 逐条真实发送。只有清洁完成以外的
    回执会投递给模型，且所有回执都只追加、不回改旧历史。
    """

    def __init__(self, pipeline: MessagePipeline) -> None:
        self.pipeline = pipeline
        self._states: dict[str, _SendConversationState] = {}
        self._recent_inbound: dict[str, list[_InboundRef]] = {}
        self._recent_recalls: dict[str, list[dict[str, Any]]] = {}
        self._send_counter = 0
        self._attempt_counter = 0
        self._send_attempts: dict[str, _SendAttempt] = {}
        self._tool_call_results: dict[str, dict[str, Any]] = {}
        self._active_model_conversation: str | None = None

    def begin_model_turn(self, conversation_id: str | None) -> None:
        self._active_model_conversation = conversation_id

    def end_model_turn(self, conversation_id: str | None) -> None:
        if self._active_model_conversation == conversation_id:
            self._active_model_conversation = None

    def is_model_active(self, conversation_id: str) -> bool:
        return self._active_model_conversation == conversation_id

    def has_in_flight(self, conversation_id: str) -> bool:
        state = self._states.get(conversation_id)
        return bool(state and (state.in_flight or state.queue))

    def should_defer_batch(self, conversation_id: str) -> bool:
        state = self._states.get(conversation_id)
        return bool(state and state.needs_resync)

    def notify_inbound(self, item: PendingMessageItem) -> None:
        ref = _InboundRef(
            seq=item.inbound_seq,
            conversation_id=item.conversation_id,
            message_id=item.message_id,
            user_id=item.user_id,
            nickname=item.nickname,
            text=item.text,
            reply_to=item.raw_event.reply_to,
            self_id=str(getattr(item.raw_event, "self_id", "") or ""),
            received_at=item.received_at,
        )
        recent = self._recent_inbound.setdefault(item.conversation_id, [])
        recent.append(ref)
        if len(recent) > 200:
            del recent[:-200]

        msg = self._inbound_to_receipt_message(ref)
        state = self._state(item.conversation_id)
        if state.in_flight or state.queue:
            if self._ignores_post_send_interrupts(state):
                if self._queued_jobs_should_interrupt(state, item, msg):
                    state.needs_resync = True
                    state.deferred_queue_interrupt_pending = True
                    state.interrupt_messages.append(msg)
                return
            if not self._inbound_should_interrupt(state, item, msg):
                return
            state.needs_resync = True
            state.interrupt_messages.append(msg)
            state.interrupt_event.set()
            return

        if not self.is_model_active(item.conversation_id):
            return

        if not self._model_thinking_inbound_should_interrupt(item, msg):
            return
        state.needs_resync = True
        state.interrupt_messages.append(msg)

        # LLM 正在思考但还没有发送在途：也要把新消息作为回执边界带回模型。
        receipt = self._find_or_create_model_interrupt_receipt(
            state,
            item.conversation_id,
        )
        receipt["new_messages"].append(msg)

    def notify_recall(
        self,
        conversation_id: str,
        *,
        message_id: str,
        note: str,
    ) -> None:
        """记录撤回导致的会话状态变化，阻止模型继续发送旧判断。"""
        recalled = {
            "conversation_id": conversation_id,
            "time": get_time(),
            "msg_id": str(message_id),
            "note": note,
            "qq_visible": False,
        }
        recent = self._recent_recalls.setdefault(conversation_id, [])
        recent.append(recalled)
        if len(recent) > 200:
            del recent[:-200]
        state = self._state(conversation_id)
        needs_interrupt = (
            state.in_flight
            or bool(state.queue)
            or self.is_model_active(conversation_id)
            or bool(state.pending_receipts)
        )
        if not needs_interrupt:
            return
        state.recall_events.append(recalled)
        if len(state.recall_events) > 50:
            del state.recall_events[:-50]
        state.interrupt_messages = [
            msg for msg in state.interrupt_messages
            if str(msg.get("msg_id") or "") != str(message_id)
        ]
        for receipt in state.pending_receipts:
            receipt["new_messages"] = [
                msg for msg in receipt.get("new_messages", [])
                if str(msg.get("msg_id") or "") != str(message_id)
            ]
            known = {
                str(msg.get("msg_id") or "")
                for msg in receipt.get("recalled_messages", [])
            }
            if str(message_id) not in known:
                receipt.setdefault("recalled_messages", []).append(recalled)

        if state.in_flight or state.queue:
            state.needs_resync = True
            state.interrupt_event.set()
            return
        if not self.is_model_active(conversation_id):
            return
        state.needs_resync = True
        receipt = self._find_or_create_model_interrupt_receipt(state, conversation_id)
        known = {
            str(msg.get("msg_id") or "")
            for msg in receipt.get("recalled_messages", [])
        }
        if str(message_id) not in known:
            receipt.setdefault("recalled_messages", []).append(recalled)

    async def submit(
        self,
        actions: list[dict[str, Any]],
        source_tool: str,
        *,
        trigger_message_id: str | None = None,
        trigger_inbound_seq: int = 0,
        trigger_user_id: str | None = None,
        default_reviewed_until_seq: int | None = None,
        default_focus_user_ids: list[str] | None = None,
        default_trigger_message_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(metadata or {})
        tool_call_id = str(metadata.get("tool_call_id") or "").strip()
        if tool_call_id and tool_call_id in self._tool_call_results:
            return copy.deepcopy(self._tool_call_results[tool_call_id])

        if source_tool == "commit_send_attempt" or metadata.get("commit_send_attempt_id"):
            result = await self._commit_send_attempt(
                metadata,
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
            )
            self._remember_tool_call_result(tool_call_id, result)
            return result

        send_id = self._next_send_id()
        normalized = [self._normalize_action(a) for a in actions]
        if not normalized:
            result = {
                "ok": True,
                "status": "sent",
                "qq_visible": False,
                "send_id": send_id,
                "count": 0,
                "sent": [],
            }
            self._remember_tool_call_result(tool_call_id, result)
            return result

        delivery_interrupt_policy = str(
            metadata.get("delivery_interrupt_policy")
            or self._group_interrupt_policy(normalized)
        )
        for action in normalized:
            action["interrupt_policy"] = delivery_interrupt_policy

        groups: dict[str, list[dict[str, Any]]] = {}
        for action in normalized:
            groups.setdefault(self._conversation_id(action), []).append(action)

        reviewed_until_seq = self._reviewed_until_seq(
            metadata.get("reviewed_until_seq"),
            default_reviewed_until_seq
            if default_reviewed_until_seq is not None
            else trigger_inbound_seq,
        )
        review_policy = str(metadata.get("review_policy") or "review_priority")
        responding_to_message_ids = self._message_id_list(
            metadata.get("responding_to_message_ids")
        )
        reply_to_message_id = str(metadata.get("reply_to_message_id") or "").strip()
        trigger_message_ids = self._trigger_message_ids(
            trigger_message_id,
            default_trigger_message_ids or [],
            responding_to_message_ids,
            reply_to_message_id,
        )
        focus_user_ids = self._focus_user_ids(
            list(groups.keys()),
            responding_to_message_ids=responding_to_message_ids,
            default_focus_user_ids=default_focus_user_ids,
            trigger_user_id=trigger_user_id,
        )
        preflight = self._preflight_send(
            list(groups.keys()),
            reviewed_until_seq=reviewed_until_seq,
            review_policy=review_policy,
            focus_user_ids=focus_user_ids,
            trigger_message_ids=trigger_message_ids,
        )
        if preflight["needs_review"]:
            attempt = self._create_send_attempt(
                normalized,
                source_tool=source_tool,
                conversation_ids=list(groups.keys()),
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
                focus_user_ids=focus_user_ids,
                trigger_message_ids=trigger_message_ids,
                reviewed_until_seq=reviewed_until_seq,
                review_policy=review_policy,
                delivery_interrupt_policy=delivery_interrupt_policy,
                tool_call_id=tool_call_id or None,
                reason=str(metadata.get("reason") or "") or None,
            )
            result = self._needs_review_result(
                attempt,
                preflight,
                status="needs_review",
            )
            self._remember_tool_call_result(tool_call_id, result)
            return result

        result = await self._accept_send(
            send_id,
            normalized,
            groups,
            source_tool,
            ignore_review_interrupts=bool(metadata.get("ignore_review_interrupts")),
            trigger_message_id=trigger_message_id,
            trigger_inbound_seq=trigger_inbound_seq,
            trigger_user_id=trigger_user_id,
        )
        self._remember_tool_call_result(tool_call_id, result)
        return result

    async def _accept_send(
        self,
        send_id: str,
        normalized: list[dict[str, Any]],
        groups: dict[str, list[dict[str, Any]]],
        source_tool: str,
        *,
        ignore_review_interrupts: bool,
        trigger_message_id: str | None,
        trigger_inbound_seq: int,
        trigger_user_id: str | None,
    ) -> dict[str, Any]:
        can_sync = all(self._can_sync_send(cid, acts) for cid, acts in groups.items())
        if can_sync:
            return await self._send_sync(
                send_id,
                groups,
                source_tool,
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
            )

        for conversation_id, group_actions in groups.items():
            state = self._state(conversation_id)
            job = _SendJob(
                send_id=send_id,
                conversation_id=conversation_id,
                actions=group_actions,
                source_tool=source_tool,
                interrupt_policy=self._group_interrupt_policy(group_actions),
                ignore_review_interrupts=ignore_review_interrupts,
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
                created_at=time.monotonic(),
            )
            state.queue.append(job)
            if state.worker is None or state.worker.done():
                state.worker = asyncio.create_task(self._worker(conversation_id, state))

        return {
            "ok": True,
            "status": "accepted",
            "accepted": True,
            "delivery": "pending",
            "qq_visible": "pending",
            "send_id": send_id,
            "accepted_messages": self._accepted_items(normalized),
            "next": "这批消息已经被系统接收，不要重复提交同一批；如需补充，只发送新增内容。",
            "data": {
                "conversation_ids": list(groups.keys()),
                "message_count": sum(len(items) for items in groups.values()),
            },
        }

    async def _commit_send_attempt(
        self,
        metadata: dict[str, Any],
        *,
        trigger_message_id: str | None,
        trigger_inbound_seq: int,
        trigger_user_id: str | None,
    ) -> dict[str, Any]:
        _ = trigger_message_id, trigger_inbound_seq, trigger_user_id
        attempt_id = str(metadata.get("commit_send_attempt_id") or "").strip()
        attempt = self._send_attempts.get(attempt_id)
        if attempt is None:
            return {
                "ok": False,
                "status": "not_found",
                "send_attempt_id": attempt_id,
                "qq_visible": False,
                "error": "send_attempt 不存在或已过期",
            }
        if attempt.consumed:
            return {
                "ok": False,
                "status": "already_committed",
                "send_attempt_id": attempt_id,
                "qq_visible": False,
                "next": "这个 send_attempt 已经提交过；不要重复发送同一批。如需补充，只发送新增内容。",
            }
        reviewed_until_seq = self._reviewed_until_seq(
            metadata.get("reviewed_until_seq"),
            attempt.reviewed_until_seq,
        )
        ignore_review_interrupts = bool(metadata.get("ignore_review_interrupts"))
        preflight = self._preflight_send(
            attempt.conversation_ids,
            reviewed_until_seq=reviewed_until_seq,
            review_policy=attempt.review_policy,
            focus_user_ids=attempt.focus_user_ids,
            trigger_message_ids=attempt.trigger_message_ids,
        )
        if preflight["recalled_messages"]:
            return {
                "ok": False,
                "status": "cannot_commit_recalled_trigger",
                "send_attempt_id": attempt_id,
                "qq_visible": False,
                "recalled_messages": preflight["recalled_messages"],
                "next": "相关消息已撤回，不能确认发送旧内容。请重新判断或 no_action。",
            }
        forced_unseen_messages: list[dict[str, Any]] = []
        if preflight["needs_review"] and not ignore_review_interrupts:
            attempt.revision += 1
            return self._needs_review_result(
                attempt,
                preflight,
                status="needs_review_again",
            )
        if preflight["needs_review"] and ignore_review_interrupts:
            forced_unseen_messages = list(preflight["unseen_messages"])

        normalized = [dict(action) for action in attempt.actions]
        delivery_policy = str(
            metadata.get("delivery_interrupt_policy")
            or attempt.delivery_interrupt_policy
            or self._group_interrupt_policy(normalized)
        )
        for action in normalized:
            action["interrupt_policy"] = delivery_policy
        self._apply_reply_to_first_text(normalized, metadata.get("reply_to_message_id"))

        groups: dict[str, list[dict[str, Any]]] = {}
        for action in normalized:
            groups.setdefault(self._conversation_id(action), []).append(action)

        send_id = self._next_send_id()
        result = await self._accept_send(
            send_id,
            normalized,
            groups,
            attempt.source_tool,
            ignore_review_interrupts=False,
            trigger_message_id=attempt.trigger_message_id,
            trigger_inbound_seq=attempt.trigger_inbound_seq,
            trigger_user_id=attempt.trigger_user_id,
        )
        attempt.consumed = True
        result["send_attempt_id"] = attempt_id
        if forced_unseen_messages:
            result["ignored_review_interrupts"] = True
            result["forced_unseen_messages"] = forced_unseen_messages
            result["next"] = "已按 ignore_review_interrupts 提交旧 attempt。不要重复提交同一批。"
        return result

    def pop_pending_receipts(self, conversation_id: str) -> list[dict[str, Any]]:
        state = self._state(conversation_id)
        receipts = state.pending_receipts[:]
        state.pending_receipts.clear()
        return receipts

    def mark_receipts_delivered(self, conversation_id: str) -> None:
        state = self._state(conversation_id)
        state.needs_resync = False
        state.deferred_queue_interrupt_pending = False
        state.interrupt_messages.clear()
        state.recall_events.clear()
        state.interrupt_event.clear()

    def clear_resync(self, conversation_id: str) -> None:
        """清理已处理的 resync 标记。"""
        state = self._state(conversation_id)
        state.needs_resync = False
        state.deferred_queue_interrupt_pending = False
        state.interrupt_messages.clear()
        state.recall_events.clear()
        state.interrupt_event.clear()

    def _state(self, conversation_id: str) -> _SendConversationState:
        state = self._states.get(conversation_id)
        if state is None:
            state = _SendConversationState()
            self._states[conversation_id] = state
        return state

    def _next_send_id(self) -> str:
        self._send_counter += 1
        return f"send-{int(time.time() * 1000)}-{self._send_counter}"

    def _next_attempt_id(self) -> str:
        self._attempt_counter += 1
        return f"attempt-{int(time.time() * 1000)}-{self._attempt_counter}"

    def _remember_tool_call_result(
        self,
        tool_call_id: str,
        result: dict[str, Any],
    ) -> None:
        if not tool_call_id:
            return
        self._tool_call_results[tool_call_id] = copy.deepcopy(result)
        if len(self._tool_call_results) > 200:
            for key in list(self._tool_call_results)[:50]:
                self._tool_call_results.pop(key, None)

    @staticmethod
    def _conversation_id(action: dict[str, Any]) -> str:
        return f"{action['target_scope']}:{action['target_id']}"

    @staticmethod
    def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": action.get("kind", "text"),
            "order": int(action.get("order", 0)),
            "target_scope": str(action.get("target_scope")),
            "target_id": str(action.get("target_id")),
            "content": str(action.get("content") or ""),
            "label": str(action.get("label") or action.get("content") or ""),
            "delay": float(action.get("delay") or 0.0),
            "audio_path": str(action.get("audio_path") or ""),
            "image_path": str(action.get("image_path") or ""),
            "image_url": str(action.get("image_url") or ""),
            "interrupt_policy": str(action.get("interrupt_policy") or "interrupt_all"),
        }

    @staticmethod
    def _reviewed_until_seq(value: Any, default: int | None) -> int:
        if value is None or value == "":
            return max(0, int(default or 0))
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return max(0, int(default or 0))

    @staticmethod
    def _message_id_list(value: Any) -> list[str]:
        if value is None:
            return []
        raw_items = value if isinstance(value, list) else [value]
        result: list[str] = []
        for item in raw_items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def _trigger_message_ids(
        self,
        trigger_message_id: str | None,
        default_trigger_message_ids: list[str],
        responding_to_message_ids: list[str],
        reply_to_message_id: str | None,
    ) -> list[str]:
        result: list[str] = []
        for item in [
            trigger_message_id,
            *default_trigger_message_ids,
            *responding_to_message_ids,
            reply_to_message_id,
        ]:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def _focus_user_ids(
        self,
        conversation_ids: list[str],
        *,
        responding_to_message_ids: list[str],
        default_focus_user_ids: list[str] | None,
        trigger_user_id: str | None,
    ) -> list[str]:
        result: list[str] = []
        for message_id in responding_to_message_ids:
            user_id = self._user_id_for_message(conversation_ids, message_id)
            if user_id and user_id not in result:
                result.append(user_id)
        if result:
            return result
        for user_id in default_focus_user_ids or []:
            text = str(user_id or "").strip()
            if text and text not in result:
                result.append(text)
        if result:
            return result
        text = str(trigger_user_id or "").strip()
        return [text] if text else []

    def _user_id_for_message(
        self,
        conversation_ids: list[str],
        message_id: str,
    ) -> str | None:
        target = str(message_id or "").strip()
        if not target:
            return None
        for conversation_id in conversation_ids:
            for ref in reversed(self._recent_inbound.get(conversation_id) or []):
                if str(ref.message_id) == target:
                    return ref.user_id
        return None

    def _preflight_send(
        self,
        conversation_ids: list[str],
        *,
        reviewed_until_seq: int,
        review_policy: str,
        focus_user_ids: list[str],
        trigger_message_ids: list[str],
    ) -> dict[str, Any]:
        unseen: list[dict[str, Any]] = []
        priority_interrupts: list[dict[str, Any]] = []
        recalled_messages: list[dict[str, Any]] = []
        for conversation_id in conversation_ids:
            for ref in self._recent_inbound.get(conversation_id) or []:
                if ref.seq <= reviewed_until_seq:
                    continue
                item = self._preflight_message(ref)
                reasons = self._priority_reasons_for_ref(
                    ref,
                    focus_user_ids=focus_user_ids,
                    trigger_message_ids=trigger_message_ids,
                )
                item["priority"] = bool(reasons)
                if reasons:
                    item["priority_reasons"] = reasons
                    priority_interrupts.append(item)
                unseen.append(item)
            state = self._state(conversation_id)
            recall_events = [
                *self._recent_recalls.get(conversation_id, []),
                *state.recall_events,
            ]
            seen_recall_ids: set[str] = set()
            for recalled in recall_events:
                msg_id = str(recalled.get("msg_id") or "")
                if not msg_id or msg_id in seen_recall_ids:
                    continue
                seen_recall_ids.add(msg_id)
                if (
                    msg_id in trigger_message_ids
                    or msg_id in {str(item.get("msg_id") or "") for item in unseen}
                ):
                    recalled_messages.append(recalled)

        needs_review = False
        if recalled_messages:
            needs_review = True
        elif review_policy == "review_all" and unseen:
            needs_review = True
        elif priority_interrupts:
            needs_review = True
        latest_seq = reviewed_until_seq
        for conversation_id in conversation_ids:
            recent = self._recent_inbound.get(conversation_id) or []
            if recent:
                latest_seq = max(latest_seq, max(ref.seq for ref in recent))
        return {
            "needs_review": needs_review,
            "unseen_messages": unseen,
            "priority_interrupts": priority_interrupts,
            "recalled_messages": recalled_messages,
            "latest_seq": latest_seq,
        }

    def _priority_reasons_for_ref(
        self,
        ref: _InboundRef,
        *,
        focus_user_ids: list[str],
        trigger_message_ids: list[str],
    ) -> list[str]:
        reasons: list[str] = []
        if ref.conversation_id.startswith("private:"):
            reasons.append("private_message")
        if ref.user_id in focus_user_ids:
            reasons.append("focus_user")
        if ref.reply_to and ref.reply_to in trigger_message_ids:
            reasons.append("reply_to_trigger_message")
        if _text_mentions_self_or_role(ref.text, ref.self_id, self.pipeline.persona.name):
            reasons.append("mentions_bot_or_role")
        for message in self.pipeline.chat_timeline.recent(ref.conversation_id, 20):
            if message.direction != "outbound" or not message.msg_id:
                continue
            if ref.reply_to and str(ref.reply_to) == str(message.msg_id):
                reasons.append("reply_to_recent_bot_message")
            break
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _preflight_message(ref: _InboundRef) -> dict[str, Any]:
        return {
            "conversation_id": ref.conversation_id,
            "seq": ref.seq,
            "time": get_time(),
            "nickname": ref.nickname,
            "user_id": ref.user_id,
            "text": ref.text,
            "msg_id": ref.message_id,
            "reply_to": ref.reply_to,
            "qq_visible": True,
        }

    def _create_send_attempt(
        self,
        actions: list[dict[str, Any]],
        *,
        source_tool: str,
        conversation_ids: list[str],
        trigger_message_id: str | None,
        trigger_inbound_seq: int,
        trigger_user_id: str | None,
        focus_user_ids: list[str],
        trigger_message_ids: list[str],
        reviewed_until_seq: int,
        review_policy: str,
        delivery_interrupt_policy: str,
        tool_call_id: str | None,
        reason: str | None,
    ) -> _SendAttempt:
        attempt = _SendAttempt(
            send_attempt_id=self._next_attempt_id(),
            conversation_ids=list(conversation_ids),
            actions=[dict(action) for action in actions],
            source_tool=source_tool,
            trigger_message_id=trigger_message_id,
            trigger_inbound_seq=trigger_inbound_seq,
            trigger_user_id=trigger_user_id,
            focus_user_ids=list(focus_user_ids),
            trigger_message_ids=list(trigger_message_ids),
            reviewed_until_seq=reviewed_until_seq,
            review_policy=review_policy,
            delivery_interrupt_policy=delivery_interrupt_policy,
            tool_call_id=tool_call_id,
            reason=reason,
            created_at=time.monotonic(),
        )
        self._send_attempts[attempt.send_attempt_id] = attempt
        if len(self._send_attempts) > 100:
            for key in list(self._send_attempts)[:25]:
                old = self._send_attempts.get(key)
                if old and old.consumed:
                    self._send_attempts.pop(key, None)
        return attempt

    def _needs_review_result(
        self,
        attempt: _SendAttempt,
        preflight: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "status": status,
            "qq_visible": False,
            "send_attempt_id": attempt.send_attempt_id,
            "attempt_revision": attempt.revision,
            "revision": attempt.revision,
            "attempted_messages": self._attempted_items(
                attempt.actions,
                attempt.send_attempt_id,
            ),
            "unseen_messages": preflight["unseen_messages"],
            "priority_interrupts": preflight["priority_interrupts"],
            "latest_seq": preflight["latest_seq"],
            "next": (
                "这次发送内容是在未看到这些新消息时生成的。请复核："
                "调用 commit_send_attempt 确认旧内容、重新调用发送工具改写，或 no_action 放弃。"
            ),
        }
        if preflight["recalled_messages"]:
            result["recalled_messages"] = preflight["recalled_messages"]
        return result

    @staticmethod
    def _apply_reply_to_first_text(
        actions: list[dict[str, Any]],
        reply_to_message_id: Any,
    ) -> None:
        reply_to = str(reply_to_message_id or "").strip()
        if not reply_to:
            return
        prefix = f"[CQ:reply,id={reply_to}]"
        for action in actions:
            if action.get("kind", "text") != "text":
                continue
            content = str(action.get("content") or "")
            if content.startswith("[CQ:reply,"):
                return
            action["content"] = f"{prefix}{content}"
            return

    def _can_sync_send(self, conversation_id: str, actions: list[dict[str, Any]]) -> bool:
        state = self._state(conversation_id)
        if state.in_flight or state.queue or state.needs_resync:
            return False
        if len(actions) == 1:
            return True
        return all(float(a.get("delay") or 0.0) <= 0 for a in actions)

    @staticmethod
    def _group_interrupt_policy(actions: list[dict[str, Any]]) -> str:
        policies = {str(action.get("interrupt_policy") or "interrupt_all") for action in actions}
        if "interrupt_all" in policies:
            return "interrupt_all"
        if "interrupt_priority" in policies:
            return "interrupt_priority"
        return "atomic"

    def _inbound_should_interrupt(
        self,
        state: _SendConversationState,
        item: PendingMessageItem,
        msg: dict[str, Any],
    ) -> bool:
        policy = state.active_interrupt_policy
        if policy == "interrupt_all":
            return True
        if policy == "atomic":
            return False
        return self._is_priority_inbound(
            item,
            msg,
            trigger_user_id=state.active_trigger_user_id,
            trigger_message_id=state.active_trigger_message_id,
        )

    @staticmethod
    def _ignores_post_send_interrupts(state: _SendConversationState) -> bool:
        if state.in_flight:
            return state.active_ignore_review_interrupts
        if state.queue:
            return bool(state.queue[0].ignore_review_interrupts)
        return False

    def _queued_jobs_should_interrupt(
        self,
        state: _SendConversationState,
        item: PendingMessageItem,
        msg: dict[str, Any],
    ) -> bool:
        queued = list(state.queue)
        if not state.in_flight and queued:
            queued = queued[1:]
        for job in queued:
            if job.ignore_review_interrupts:
                continue
            probe = dict(msg)
            if self._job_should_interrupt(job, item, probe):
                msg.update(
                    {
                        key: value
                        for key, value in probe.items()
                        if key == "priority_reason"
                    }
                )
                return True
        return False

    def _job_should_interrupt(
        self,
        job: _SendJob,
        item: PendingMessageItem,
        msg: dict[str, Any],
    ) -> bool:
        if job.interrupt_policy == "interrupt_all":
            return True
        if job.interrupt_policy == "atomic":
            return False
        return self._is_priority_inbound(
            item,
            msg,
            trigger_user_id=job.trigger_user_id,
            trigger_message_id=job.trigger_message_id,
        )

    def _model_thinking_inbound_should_interrupt(
        self,
        item: PendingMessageItem,
        msg: dict[str, Any],
    ) -> bool:
        if item.raw_event.is_private():
            msg["priority_reason"] = "private_message"
            return True
        return self._is_priority_inbound(
            item,
            msg,
            trigger_user_id=None,
            trigger_message_id=None,
        )

    def _is_priority_inbound(
        self,
        item: PendingMessageItem,
        msg: dict[str, Any],
        *,
        trigger_user_id: str | None,
        trigger_message_id: str | None,
    ) -> bool:
        if item.raw_event.is_private():
            msg["priority_reason"] = "private_message"
            return True
        if trigger_user_id and item.user_id == trigger_user_id:
            msg["priority_reason"] = "same_trigger_user"
            return True
        if trigger_message_id and item.raw_event.reply_to == trigger_message_id:
            msg["priority_reason"] = "reply_to_trigger_message"
            return True
        if _text_mentions_self_or_role(
            item.text,
            item.raw_event.self_id,
            self.pipeline.persona.name,
        ):
            msg["priority_reason"] = "mentions_bot_or_role"
            return True
        return False

    async def _send_sync(
        self,
        send_id: str,
        groups: dict[str, list[dict[str, Any]]],
        source_tool: str,
        *,
        trigger_message_id: str | None,
        trigger_inbound_seq: int,
        trigger_user_id: str | None,
    ) -> dict[str, Any]:
        sent: list[dict[str, Any]] = []
        errors: list[str] = []
        for conversation_id, actions in groups.items():
            for _index, action in enumerate(actions):
                try:
                    msg_id = await self._send_one(
                        action,
                        source_tool,
                        conversation_id,
                        trigger_message_id=trigger_message_id,
                        trigger_inbound_seq=trigger_inbound_seq,
                        trigger_user_id=trigger_user_id,
                    )
                    sent.append(self._sent_item(action, msg_id))
                except Exception as e:  # noqa: BLE001
                    logger.exception("同步发送失败 conversation_id=%s: %s", conversation_id, e)
                    errors.append(f"{conversation_id}: {e}")

        result: dict[str, Any] = {
            "ok": bool(sent) or not errors,
            "status": "sent",
            "accepted": bool(sent) or not errors,
            "delivery": "done" if sent else "none",
            "qq_visible": bool(sent),
            "send_id": send_id,
            "count": len(sent),
            "sent": sent,
            "accepted_messages": self._accepted_items(
                [action for actions in groups.values() for action in actions]
            ),
            "next": "这批消息已经发送完成，不要重复提交同一批；如需补充，只发送新增内容。",
        }
        if errors:
            result["errors"] = errors
        return result

    async def _worker(self, conversation_id: str, state: _SendConversationState) -> None:
        try:
            while state.queue:
                job = state.queue.popleft()
                state.in_flight = True
                state.active_interrupt_policy = job.interrupt_policy
                state.active_ignore_review_interrupts = job.ignore_review_interrupts
                state.active_trigger_user_id = job.trigger_user_id
                state.active_trigger_message_id = job.trigger_message_id
                if (
                    state.deferred_queue_interrupt_pending
                    and not job.ignore_review_interrupts
                ):
                    state.interrupt_event.set()
                sent: list[dict[str, Any]] = []
                errors: list[str] = []
                interrupted = False
                unsent: list[dict[str, Any]] = []

                for index, action in enumerate(job.actions):
                    if state.interrupt_event.is_set():
                        interrupted = True
                        unsent.extend(self._unsent_items(job.actions[index:], job.send_id))
                        break

                    try:
                        msg_id = await self._send_one(
                            action,
                            job.source_tool,
                            conversation_id,
                            trigger_message_id=job.trigger_message_id,
                            trigger_inbound_seq=job.trigger_inbound_seq,
                            trigger_user_id=job.trigger_user_id,
                        )
                        sent.append(self._sent_item(action, msg_id))
                    except Exception as e:  # noqa: BLE001
                        logger.exception("异步发送失败 conversation_id=%s: %s", conversation_id, e)
                        errors.append(f"order={action.get('order')}: {e}")
                        continue

                    delay = float(action.get("delay") or 0.0)
                    if delay > 0 and index < len(job.actions) - 1:
                        try:
                            await asyncio.wait_for(state.interrupt_event.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                        if state.interrupt_event.is_set():
                            interrupted = True
                            unsent.extend(self._unsent_items(job.actions[index + 1 :], job.send_id))
                            break

                if interrupted:
                    unsent.extend(self._flush_queued_unsent(state))

                receipt = {
                    "type": "send_receipt",
                    "send_id": job.send_id,
                    "conversation_id": conversation_id,
                    "sent": sent,
                    "unsent": unsent,
                    "interrupted": interrupted,
                    "new_messages": list(state.interrupt_messages),
                }
                if state.recall_events:
                    receipt["recalled_messages"] = list(state.recall_events)
                if errors:
                    receipt["errors"] = errors
                clean = not interrupted and not errors
                await self._handle_receipt(conversation_id, receipt, clean=clean)

                if clean and state.needs_resync and not state.queue:
                    self.clear_resync(conversation_id)
                    self.pipeline._schedule_deferred_batch(conversation_id)

                if interrupted:
                    state.interrupt_event.clear()
                    state.interrupt_messages.clear()
                    state.recall_events.clear()
                    state.deferred_queue_interrupt_pending = False
                    break
        finally:
            state.in_flight = False
            state.active_interrupt_policy = "interrupt_all"
            state.active_ignore_review_interrupts = False
            state.active_trigger_user_id = None
            state.active_trigger_message_id = None
            state.worker = None
            if state.queue:
                state.worker = asyncio.create_task(self._worker(conversation_id, state))

    async def _send_one(
        self,
        action: dict[str, Any],
        source_tool: str,
        conversation_id: str,
        *,
        trigger_message_id: str | None,
        trigger_inbound_seq: int,
        trigger_user_id: str | None,
    ) -> str | None:
        target = Target(
            adapter=self.pipeline.adapter.name,
            scope=action["target_scope"],  # type: ignore[arg-type]
            target_id=action["target_id"],
        )
        kind = action.get("kind", "text")
        if kind == "voice":
            send_voice = getattr(self.pipeline.adapter, "send_voice", None)
            if send_voice is None:
                raise RuntimeError("当前适配器不支持发送语音")
            msg_id = await send_voice(target, Path(action.get("audio_path") or ""))
        elif kind in {"emoji", "image"}:
            msg_id = await self.pipeline.adapter.send_image(
                target,
                image_path=(
                    Path(str(action.get("image_path")))
                    if action.get("image_path")
                    else None
                ),
                image_url=str(action.get("image_url") or "") or None,
            )
        else:
            content = action.get("content") or ""
            msg_id = await self.pipeline.adapter.send_text(target, content)

        self.pipeline.mark_activity()
        if msg_id is not None:
            self.pipeline._record_successful_outbound(
                action,
                conversation_id=conversation_id,
                msg_id=str(msg_id),
            )
        logger.debug(
            "出站气泡 sent_at_ms=%s msg_id=%s source=%s conversation_id=%s "
            "trigger_msg_id=%s order=%s kind=%s",
            int(time.time() * 1000),
            msg_id,
            source_tool,
            conversation_id,
            trigger_message_id,
            action.get("order"),
            kind,
        )
        return msg_id

    @staticmethod
    def _sent_item(action: dict[str, Any], msg_id: str | None) -> dict[str, Any]:
        target_scope = action.get("target_scope")
        target_id = action.get("target_id")
        item: dict[str, Any] = {
            "conversation_id": f"{target_scope}:{target_id}",
            "order": int(action.get("order", 0)),
            "target_type": target_scope,
            "target_id": target_id,
            "msg_id": str(msg_id) if msg_id is not None else None,
            "content": action.get("label") or action.get("content") or "",
            "delay": float(action.get("delay") or 0.0),
            "time": get_time(),
            "qq_visible": True,
        }
        if target_scope == "private":
            item["target_qq"] = target_id
        if target_scope == "group":
            item["group_id"] = target_id
        return item

    @staticmethod
    def _unsent_items(actions: list[dict[str, Any]], send_id: str) -> list[dict[str, Any]]:
        return [
            {
                "send_id": send_id,
                "conversation_id": f"{action.get('target_scope')}:{action.get('target_id')}",
                "order": int(action.get("order", 0)),
                "target_type": action.get("target_scope"),
                "target_id": action.get("target_id"),
                "content": action.get("label") or action.get("content") or "",
                "delay": float(action.get("delay") or 0.0),
                "qq_visible": False,
            }
            for action in actions
        ]

    @staticmethod
    def _attempted_items(actions: list[dict[str, Any]], send_id: str) -> list[dict[str, Any]]:
        return [
            {
                "send_id": send_id,
                "conversation_id": f"{action.get('target_scope')}:{action.get('target_id')}",
                "target_type": action.get("target_scope"),
                "target_id": action.get("target_id"),
                "order": int(action.get("order", 0)),
                "content": action.get("label") or action.get("content") or "",
                "delay": float(action.get("delay") or 0.0),
                "qq_visible": False,
            }
            for action in actions
        ]

    @staticmethod
    def _accepted_items(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "conversation_id": f"{action.get('target_scope')}:{action.get('target_id')}",
                "target_type": action.get("target_scope"),
                "target_id": action.get("target_id"),
                "order": int(action.get("order", 0)),
                "content": action.get("label") or action.get("content") or "",
                "delay": float(action.get("delay") or 0.0),
            }
            for action in actions
        ]

    def _flush_queued_unsent(self, state: _SendConversationState) -> list[dict[str, Any]]:
        unsent: list[dict[str, Any]] = []
        while state.queue:
            queued = state.queue.popleft()
            unsent.extend(self._unsent_items(queued.actions, queued.send_id))
        return unsent

    async def _handle_receipt(
        self,
        conversation_id: str,
        receipt: dict[str, Any],
        *,
        clean: bool,
    ) -> None:
        if clean:
            await self.pipeline._record_clean_send_receipt(receipt)
            return
        state = self._state(conversation_id)
        state.pending_receipts.append(receipt)
        if not self.is_model_active(conversation_id):
            self.pipeline._schedule_send_receipt_turn(conversation_id)

    def _find_or_create_model_interrupt_receipt(
        self,
        state: _SendConversationState,
        conversation_id: str,
    ) -> dict[str, Any]:
        for receipt in state.pending_receipts:
            if receipt.get("send_id") is None and receipt.get("interrupted"):
                return receipt
        receipt = {
            "type": "send_receipt",
            "send_id": None,
            "conversation_id": conversation_id,
            "sent": [],
            "unsent": [],
            "interrupted": True,
            "new_messages": [],
            "note": "模型思考期间当前会话来了新消息",
        }
        state.pending_receipts.append(receipt)
        return receipt

    @staticmethod
    def _inbound_to_receipt_message(ref: _InboundRef) -> dict[str, Any]:
        return {
            "conversation_id": ref.conversation_id,
            "seq": ref.seq,
            "time": get_time(),
            "nickname": ref.nickname,
            "user_id": ref.user_id,
            "text": ref.text,
            "msg_id": ref.message_id,
            "qq_visible": True,
        }
