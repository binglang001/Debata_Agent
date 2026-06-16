"""发送管理器的纯 helper。"""

from __future__ import annotations

from typing import Any

from utils import get_time

from .send_state import _InboundRef


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
