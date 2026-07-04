"""发送回执与发送事件 payload 的纯 helper。"""

from __future__ import annotations

import hashlib
from typing import Any


def _send_message_payload(
    send_id: str,
    action: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    content_length, content_hash = _action_content_fingerprint(action)
    target_scope = _optional_text(action.get("target_scope"))
    target_id = _optional_text(action.get("target_id"))
    payload = {
        "source": "send_manager",
        "send_id": send_id,
        "status": status,
        "order": _safe_int(action.get("order"), default=0),
        "target_scope": target_scope,
        "target_id": target_id,
        "target_conversation_id": (
            f"{target_scope}:{target_id}" if target_scope and target_id else None
        ),
        "kind": _optional_text(action.get("kind")) or "text",
        "content_hash": content_hash,
        "content_length": content_length,
    }
    return _drop_none(payload)


def _action_content_fingerprint(action: dict[str, Any]) -> tuple[int, str]:
    text = str(action.get("label") or action.get("content") or "")
    return len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


def _send_action_counts(
    actions: list[dict[str, Any]],
    conversation_ids: list[str],
) -> dict[str, int]:
    return {
        "messages": len(actions),
        "conversations": len(conversation_ids),
        "text": sum(1 for action in actions if action.get("kind", "text") == "text"),
        "voice": sum(1 for action in actions if action.get("kind") == "voice"),
        "image": sum(1 for action in actions if action.get("kind") in {"image", "emoji"}),
    }


def _send_receipt_counts(receipt: dict[str, Any]) -> dict[str, int]:
    return {
        "sent": _list_count(receipt.get("sent")),
        "unsent": _list_count(receipt.get("unsent")),
        "new_messages": _list_count(receipt.get("new_messages")),
        "recalled_messages": _list_count(receipt.get("recalled_messages")),
        "errors": _list_count(receipt.get("errors")),
        "accepted_messages": _list_count(receipt.get("accepted_messages")),
        "attempted_messages": _list_count(receipt.get("attempted_messages")),
        "forced_unseen_messages": _list_count(receipt.get("forced_unseen_messages")),
        "unseen_messages": _list_count(receipt.get("unseen_messages")),
        "priority_interrupts": _list_count(receipt.get("priority_interrupts")),
    }


def _send_receipt_event_status(
    receipt: dict[str, Any],
    counts: dict[str, int],
) -> str:
    status = _optional_text(receipt.get("status"))
    if status:
        return status
    if receipt.get("interrupted"):
        return "interrupted"
    if counts["errors"] and counts["sent"]:
        return "partial"
    if counts["errors"]:
        return "failed"
    if counts["unsent"] and counts["sent"]:
        return "partial"
    if counts["unsent"]:
        return "unsent"
    if counts["sent"]:
        return "succeeded"
    return "empty"


def _single_conversation_id(conversation_ids: list[str]) -> str | None:
    return conversation_ids[0] if len(conversation_ids) == 1 else None


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
