"""对话页会话分组 helper。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any

from .text_format import (
    _parse_json_object,
    _parse_tool_arguments,
    _send_receipt_sent_items,
    _send_status_info,
)

_LEGACY_HEADER_RE = re.compile(
    r"^【(?P<timestamp>.*?) (?P<location>群聊 (?P<group_id>\S+)|私聊) "
    r"(?P<nickname>.*?)\((?P<user_id>.*?)\) msg_id=(?P<message_id>.*?)】",
    re.S,
)
_LEGACY_HEADER_LINE_RE = re.compile(
    r"(?m)^【(?P<timestamp>[^\n】]*?) (?P<location>群聊 (?P<group_id>\S+)|私聊) "
    r"(?P<nickname>.*?)\((?P<user_id>.*?)\) msg_id=(?P<message_id>.*?)】"
)


def _group_records_by_conversation(records: list[dict]) -> list[dict]:
    """把线性 history 分组成会话。

    新记录优先读 metadata；旧记录从 MessagePipeline 写入的正文头部解析。
    优先使用 record.conversation_id；无会话 ID 的 system/tool 归入系统记录。
    旧记录没有 conversation_id 时，assistant 仅在紧跟用户消息时归入最近会话，
    避免后台/撤回等系统轮次漂移到普通会话。
    """
    conversations: dict[str, dict] = {}
    order: list[str] = []
    current_key: str | None = None
    send_id_targets: dict[str, list[dict[str, str]]] = {}

    def _ensure(key: str, label: str) -> dict:
        if key not in conversations:
            conversations[key] = {
                "key": key,
                "label": label,
                "records": [],
                "preview": "",
            }
            order.append(key)
        return conversations[key]

    def _append(info: dict[str, str], rec: dict) -> None:
        conv = _ensure(info["key"], info["label"])
        conv["records"].append(rec)
        content = (rec.get("content") or "").strip()
        if content:
            conv["preview"] = content

    for rec in records:
        role = _record_role(rec)
        if role == "tool":
            _remember_send_id_targets(rec, send_id_targets)

        explicit_cid = rec.get("conversation_id")
        explicit_key = explicit_cid if isinstance(explicit_cid, str) else ""
        for info, fallback_record in _send_receipt_sent_fallback_records(
            rec,
            explicit_key=explicit_key,
        ):
            _append(info, fallback_record)

        send_status = _send_status_info(str(rec.get("content") or ""))
        if send_status and send_status.get("completed"):
            send_id = send_status.get("send_id") or ""
            targets = send_id_targets.get(send_id, [])
            if targets:
                accepted = _accepted_messages_for_send_id(records, send_id)
                for info in targets:
                    clone = {
                        **rec,
                        "_display_conversation_id": info["key"],
                        "_accepted_messages_for_send_status": accepted,
                    }
                    _append(info, clone)

        if isinstance(explicit_cid, str) and explicit_cid:
            info = _conversation_info_from_id(explicit_cid)
            if (
                send_status
                and send_status.get("completed")
                and info["key"] in {target["key"] for target in send_id_targets.get(send_id, [])}
            ):
                continue
            current_key = info["key"] if role == "user" else current_key
            _append(info, rec)
        elif role == "user":
            info = _conversation_info(rec)
            current_key = info["key"]
            _append(info, rec)
        elif role in {"system", "tool"}:
            _append({"key": "system:global", "label": "系统记录"}, rec)
        else:
            if current_key is None:
                _append({"key": "unknown:history", "label": "未标记来源"}, rec)
            else:
                _append(
                    {
                        "key": current_key,
                        "label": "系统记录" if current_key.startswith("system:") else current_key,
                    },
                    rec,
                )

    # 最近活跃的会话排前面。
    return [conversations[k] for k in reversed(order)]


def _targeted_tool_calls_for_record(
    rec: dict,
) -> tuple[list[tuple[dict[str, str], list[dict]]], dict[str, list[dict[str, str]]]]:
    by_conversation: dict[str, tuple[dict[str, str], list[dict]]] = {}
    call_targets: dict[str, list[dict[str, str]]] = {}
    for tool_call in rec.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        targets = _tool_call_target_infos(tool_call)
        if not targets:
            continue
        tool_call_id = str(tool_call.get("id") or "").strip()
        if tool_call_id:
            call_targets[tool_call_id] = targets
        for info in targets:
            entry = by_conversation.setdefault(info["key"], (info, []))
            entry[1].append(tool_call)
    return list(by_conversation.values()), call_targets


def _tool_call_target_infos(tool_call: dict) -> list[dict[str, str]]:
    func = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(func, dict):
        return []
    name = str(func.get("name") or "").strip()
    args = _parse_tool_arguments(func.get("arguments"))
    if name == "send_private_messages":
        targets = args.get("targets")
        if not isinstance(targets, list):
            targets = [args]
        return _unique_conversation_infos(
            _private_target_info(target)
            for target in targets
            if isinstance(target, dict)
        )
    if name == "send_group_message":
        return _unique_conversation_infos([_group_target_info(args)])
    return _conversation_infos_from_payload(args)


def _tool_result_target_infos(
    rec: dict,
    tool_call_targets: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    payload = _parse_json_object(str(rec.get("content") or ""))
    if payload:
        targets.extend(_conversation_infos_from_payload(payload))
        for key in ("sent", "accepted_messages", "accepted", "queued"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        targets.extend(_conversation_infos_from_payload(item))
    tool_call_id = str(rec.get("tool_call_id") or "").strip()
    if tool_call_id:
        targets.extend(tool_call_targets.get(tool_call_id, []))
    return _unique_conversation_infos(targets)


def _remember_send_id_targets(
    rec: dict,
    send_id_targets: dict[str, list[dict[str, str]]],
) -> None:
    payload = _parse_json_object(str(rec.get("content") or ""))
    if not payload:
        return
    send_id = str(payload.get("send_id") or "").strip()
    if not send_id:
        return
    targets: list[dict[str, str]] = []
    for key in ("accepted_messages", "sent"):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                targets.extend(_conversation_infos_from_payload(item))
    if targets:
        send_id_targets[send_id] = _unique_conversation_infos(
            [*send_id_targets.get(send_id, []), *targets]
        )


def _send_receipt_sent_fallback_records(
    rec: dict[str, Any],
    *,
    explicit_key: str,
) -> list[tuple[dict[str, str], dict[str, Any]]]:
    result: list[tuple[dict[str, str], dict[str, Any]]] = []
    for sent in _send_receipt_sent_items(str(rec.get("content") or "")):
        text = str(sent.get("content") or sent.get("label") or "").strip()
        if not text:
            continue
        for info in _conversation_infos_from_payload(sent):
            if info["key"] == explicit_key:
                continue
            fallback = {
                **sent,
                "role": "assistant",
                "direction": "outbound",
                "content": text,
                "conversation_id": info["key"],
                "_display_conversation_id": info["key"],
                "qq_visible": sent.get("qq_visible", True),
                "_synthetic_source": "send_receipt",
                "_record_order": rec.get("_record_order"),
            }
            timestamp = sent.get("time") or rec.get("timestamp") or rec.get("created_at")
            if timestamp and not fallback.get("timestamp"):
                fallback["timestamp"] = timestamp
            result.append((info, fallback))
    return result


def _accepted_messages_for_send_id(records: list[dict], send_id: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for rec in records:
        payload = _parse_json_object(str(rec.get("content") or ""))
        if not payload or str(payload.get("send_id") or "").strip() != send_id:
            continue
        accepted = payload.get("accepted_messages")
        if isinstance(accepted, list):
            messages.extend(dict(item) for item in accepted if isinstance(item, dict))
    return messages


def _conversation_infos_from_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    infos: list[dict[str, str]] = []
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if conversation_id:
        infos.append(_conversation_info_from_id(conversation_id))
    scope = str(payload.get("scope") or payload.get("target_type") or "").strip()
    if scope == "group":
        infos.append(_group_target_info(payload))
    elif scope in {"private", "user"}:
        infos.append(_private_target_info(payload))
    elif payload.get("group_id"):
        infos.append(_group_target_info(payload))
    elif payload.get("target_qq") or payload.get("user_id") or payload.get("target_id"):
        infos.append(_private_target_info(payload))
    return _unique_conversation_infos(infos)


def _private_target_info(payload: dict[str, Any]) -> dict[str, str] | None:
    target_id = str(
        payload.get("target_qq")
        or payload.get("user_id")
        or payload.get("target_id")
        or payload.get("sender_id")
        or ""
    ).strip()
    if not target_id:
        return None
    return _conversation_info_from_id(f"private:{target_id}")


def _group_target_info(payload: dict[str, Any]) -> dict[str, str] | None:
    group_id = str(payload.get("group_id") or payload.get("target_id") or "").strip()
    if not group_id:
        return None
    return _conversation_info_from_id(f"group:{group_id}")


def _unique_conversation_infos(infos: Iterable[dict[str, str] | None]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for info in infos:
        if not info:
            continue
        key = str(info.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({"key": key, "label": str(info.get("label") or key)})
    return result


def _conversation_info(rec: dict) -> dict[str, str]:
    meta_messages = (rec.get("metadata") or {}).get("messages") or []
    first = meta_messages[0] if meta_messages else None
    if isinstance(first, dict):
        scope = first.get("scope") or "private"
        if scope == "group":
            group_id = str(first.get("group_id") or first.get("target_id") or "未知群")
            return {"key": f"group:{group_id}", "label": f"群聊 {group_id}"}
        user_id = str(first.get("user_id") or first.get("target_id") or "未知用户")
        nickname = str(first.get("nickname") or "私聊")
        return {"key": f"private:{user_id}", "label": f"私聊 {nickname}({user_id})"}

    content = rec.get("content") or ""
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        if match.group("group_id"):
            group_id = match.group("group_id")
            return {"key": f"group:{group_id}", "label": f"群聊 {group_id}"}
        user_id = match.group("user_id")
        nickname = match.group("nickname")
        return {"key": f"private:{user_id}", "label": f"私聊 {nickname}({user_id})"}
    return {"key": "unknown:history", "label": "未标记来源"}


def _conversation_info_from_id(conversation_id: str) -> dict[str, str]:
    if ":" not in conversation_id:
        return {"key": conversation_id, "label": conversation_id}
    scope, target_id = conversation_id.split(":", 1)
    if scope == "group":
        return {"key": conversation_id, "label": f"群聊 {target_id}"}
    if scope == "private":
        return {"key": conversation_id, "label": f"私聊 {target_id}"}
    if scope == "system":
        return {"key": conversation_id, "label": _system_conversation_label(target_id)}
    return {"key": conversation_id, "label": conversation_id}


def _system_conversation_label(target_id: str) -> str:
    labels = {
        "global": "系统记录 · 全局",
        "proactive": "系统记录 · 社交决策",
        "wakeup": "系统记录 · 定时唤醒",
        "agent_task": "系统记录 · 后台任务",
        "request": "系统记录 · 请求处理",
    }
    return labels.get(target_id, f"系统记录 · {target_id}")


def _conversation_list_signature(conversations: list[dict]) -> list[tuple[str, int, str]]:
    signature: list[tuple[str, int, str]] = []
    for conv in conversations:
        signature.append(
            (
                str(conv.get("key") or ""),
                len(conv.get("records") or []),
                str(conv.get("preview") or ""),
            )
        )
    return signature


def _records_cache_signature(records: list[dict]) -> tuple[int, str]:
    digest = hashlib.sha256()
    for record in records:
        digest.update(_record_cache_blob(record).encode("utf-8"))
        digest.update(b"\0")
    return len(records), digest.hexdigest()


def _record_cache_blob(record: dict) -> str:
    try:
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return repr(record)


def _record_role(rec: dict[str, Any]) -> str:
    role = str(rec.get("role") or "").strip()
    direction = str(rec.get("direction") or "").strip()
    if role in {"system", "tool"}:
        return role
    if direction == "outbound":
        return "assistant"
    if direction == "inbound":
        return "user"
    if role:
        return role
    if str(rec.get("conversation_id") or "").startswith("system:"):
        return "system"
    return "user"
