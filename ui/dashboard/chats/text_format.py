"""对话页文本格式化工具。"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from ..tool_display import format_tool_call, format_tool_result

INLINE_PREVIEW_LIMIT = 80

_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\s<>'\"|?*]+")
_WORKSPACE_PATH_RE = re.compile(r"\b(?:workspace=)?(?:incoming|workspace|outgoing)[/\\][^\s\]]+")


def _parse_json_object(content: str) -> dict[str, Any] | None:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _format_send_status_summary(content: str) -> str:
    body = _extract_tag_text(content, "send_status") or content
    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and "不是用户新发言" not in line
    ]
    summary = lines[-1] if lines else _first_nonempty_line(body)
    return _compact_inline_tokens(summary) or "发送状态"


def _send_status_info(content: str) -> dict[str, Any] | None:
    if "<send_status" not in content and "send_id" not in content:
        return None
    body = _extract_tag_text(content, "send_status") or content
    payload = _parse_json_object(body)
    if payload:
        send_id = str(payload.get("send_id") or "").strip()
        msg_ids = payload.get("msg_ids") or payload.get("message_ids")
        if not isinstance(msg_ids, list):
            msg_ids = []
        parsed_msg_ids = [str(item).strip() for item in msg_ids if str(item).strip()]
        return {
            "send_id": send_id,
            "msg_ids": parsed_msg_ids,
            "completed": _send_status_payload_completed(payload, parsed_msg_ids),
        } if send_id else None

    send_id_match = re.search(r"\bsend_id=([^\s,;，。]+)", body)
    if not send_id_match:
        return None
    msg_ids: list[str] = []
    list_match = re.search(r"\bmsg_ids=\[([^\]]*)\]", body)
    if list_match:
        msg_ids = [
            part.strip().strip("'\"")
            for part in list_match.group(1).split(",")
            if part.strip().strip("'\"")
        ]
    else:
        single_match = re.search(r"\bmsg_id=([^\s,;，。]+)", body)
        if single_match:
            msg_ids = [single_match.group(1).strip().strip("'\"")]
    return {
        "send_id": send_id_match.group(1).strip(),
        "msg_ids": msg_ids,
        "completed": _send_status_text_completed(body, msg_ids),
    }


def _send_status_payload_completed(payload: dict[str, Any], msg_ids: list[str]) -> bool:
    status = str(payload.get("status") or payload.get("delivery") or "").strip().casefold()
    if status in {"sent", "done", "completed", "complete", "success", "succeeded"}:
        return True
    if status in {"pending", "queued", "accepted", "needs_review", "failed", "stale"}:
        return False
    if payload.get("ok") is True and msg_ids:
        return True
    return bool(msg_ids and status not in {"failed", "stale"})


def _send_status_text_completed(body: str, msg_ids: list[str]) -> bool:
    if not msg_ids:
        return False
    lowered = body.casefold()
    if any(token in lowered for token in ("失败", "过期", "stale", "failed", "pending", "等待")):
        return False
    return any(token in body for token in ("发送完成", "发送成功", "已发送", "投递完成")) or "sent" in lowered


def _format_send_receipt_summary(content: str) -> str:
    payload = _extract_tag_json(content, "send_receipt")
    if not payload:
        return _format_send_receipt_text_summary(content)
    status = payload.get("status")
    ok = payload.get("ok")
    sent = payload.get("sent") or []
    unsent = payload.get("unsent") or []
    attempted = payload.get("attempted_messages") or []
    new_messages = payload.get("new_messages") or payload.get("new_visible_messages") or []
    recalled = payload.get("recalled_messages") or []
    parts = []
    if status:
        parts.append(f"状态 {status}")
    elif ok is not None:
        parts.append("成功" if ok else "未完成")
    if sent:
        parts.append(f"已发送 {len(sent)} 条")
    if unsent:
        parts.append(f"未发送 {len(unsent)} 条")
    if attempted:
        parts.append(f"待发送/尝试 {len(attempted)} 条")
    accepted = payload.get("accepted") or payload.get("queued") or []
    if accepted:
        parts.append(f"排队/待确认 {len(accepted)} 条")
    if new_messages:
        parts.append(f"新消息 {len(new_messages)} 条")
    if recalled:
        parts.append(f"撤回 {len(recalled)} 条")
    delivery = payload.get("delivery")
    if delivery:
        parts.append(f"投递 {delivery}")
    if payload.get("qq_visible") == "pending":
        parts.append("QQ 可见性待确认")
    note = str(payload.get("note") or payload.get("next") or "").strip()
    if note:
        parts.append(_compact_inline_tokens(note))
    return "；".join(parts) if parts else "发送回执"


def _format_send_receipt_text_summary(content: str) -> str:
    body = _extract_tag_text(content, "send_receipt") or content
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    parts: list[str] = []
    for line in lines:
        match = re.match(r"^状态[:：]\s*(.+?)\s*$", line)
        if match:
            status = match.group(1).strip().rstrip("。.")
            if status:
                parts.append(f"状态 {status}")
            break
    for source, label in (
        ("已发送", "已发送"),
        ("未发送", "未发送"),
        ("新消息", "新消息"),
        ("撤回消息", "撤回"),
        ("错误", "错误"),
    ):
        count = _send_receipt_text_section_count(lines, source)
        if count > 0:
            parts.append(f"{label} {count} 条")
    if parts:
        return "；".join(parts)
    first = _first_nonempty_line(body)
    return _compact_inline_tokens(first) if first else "发送回执"


def _send_receipt_sent_items(content: str) -> list[dict[str, Any]]:
    payload = _extract_tag_json(content, "send_receipt")
    if not payload:
        return _send_receipt_text_sent_items(content)
    sent = payload.get("sent")
    if not isinstance(sent, list):
        return []
    send_id = str(payload.get("send_id") or "").strip()
    result: list[dict[str, Any]] = []
    for order, item in enumerate(sent):
        if not isinstance(item, dict) or item.get("qq_visible") is False:
            continue
        enriched = {
            **item,
            "send_order": item.get("send_order", order),
            "_synthetic_source": "send_receipt",
        }
        if send_id and not enriched.get("send_id"):
            enriched["send_id"] = send_id
        result.append(enriched)
    return result


def _send_receipt_text_sent_items(content: str) -> list[dict[str, Any]]:
    body = _extract_tag_text(content, "send_receipt")
    if not body:
        return []
    try:
        return _parse_send_receipt_text_sent_items(body)
    except (AttributeError, TypeError, ValueError):
        return []


def _parse_send_receipt_text_sent_items(body: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    send_id = _send_receipt_text_send_id(lines)
    start = -1
    declared_count = 0
    for index, line in enumerate(lines):
        count = _send_receipt_text_section_count([line], "已发送")
        if count >= 0:
            start = index + 1
            declared_count = count
            break
    if start < 0 or declared_count <= 0:
        return []

    result: list[dict[str, Any]] = []
    for order, line in enumerate(_send_receipt_text_section_lines(lines, start)):
        match = re.match(r"^\s*\d+[\.\)、]\s*(.+?)\s*$", line)
        if not match:
            continue
        item = _parse_send_receipt_text_sent_line(
            match.group(1),
            fallback_send_id=send_id,
            fallback_order=order,
        )
        if item:
            result.append(item)
    return result


def _send_receipt_text_send_id(lines: list[str]) -> str:
    for line in lines:
        match = re.match(r"^发送回执[:：]\s*(\S+)\s*$", line)
        if match:
            value = match.group(1).strip()
            return "" if value == "无" else value
    return ""


def _send_receipt_text_section_count(lines: list[str], title: str) -> int:
    pattern = rf"^{re.escape(title)}\s*(\d+)\s*条[:：]?\s*$"
    for line in lines:
        match = re.match(pattern, line)
        if match:
            return int(match.group(1))
    return -1


def _send_receipt_text_section_lines(lines: list[str], start: int) -> list[str]:
    result: list[str] = []
    for line in lines[start:]:
        if _send_receipt_text_is_section_heading(line) or line.startswith("处理要求"):
            break
        result.append(line)
    return result


def _send_receipt_text_is_section_heading(line: str) -> bool:
    return bool(
        re.match(
            r"^(已发送|未发送|新消息|撤回消息|错误|attempted|accepted|待发送/尝试|排队/待确认|已接受消息)\s*\d+\s*条[:：]?\s*$",
            line,
        )
    )


def _parse_send_receipt_text_sent_line(
    text: str,
    *,
    fallback_send_id: str,
    fallback_order: int,
) -> dict[str, Any] | None:
    parts = [part.strip() for part in re.split(r"[；;](?=[A-Za-z_][\w-]*=)", text) if part.strip()]
    if not parts:
        return None
    item: dict[str, Any] = {}
    content_parts: list[str] = []
    for part in parts:
        match = re.match(r"^([A-Za-z_][\w-]*)=(.*)$", part)
        if not match:
            content_parts.append(part)
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if value and value != "无":
            item[key] = _send_receipt_text_field_value(key, value)
    content = str(item.get("content") or "；".join(content_parts)).strip()
    if not content:
        return None
    if item.get("qq_visible") is False:
        return None
    item["content"] = content
    item.setdefault("qq_visible", True)
    item.setdefault("send_order", item.get("order", fallback_order))
    if fallback_send_id:
        item.setdefault("send_id", fallback_send_id)
    item["_synthetic_source"] = "send_receipt"
    return item


def _send_receipt_text_field_value(key: str, value: str) -> Any:
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if key in {"order", "send_order", "message_index", "index"} and value.isdigit():
        return int(value)
    return value


def _format_task_context_summary(content: str) -> str:
    parts = ["本轮系统上下文"]
    conv_match = re.search(r"当前会话：([^。\n]+)", content)
    if conv_match:
        parts.append(f"当前会话 {conv_match.group(1).strip()}")
    recent_count = len(re.findall(r"<recent_group_messages\b", content))
    if recent_count:
        parts.append("包含最近群聊窗口")
    if "可用表情" in content:
        parts.append("包含可用表情提示")
    return "；".join(parts)


def _extract_tag_json(content: str, tag: str) -> dict[str, Any] | None:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", content, re.S)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        json_start = raw.find("{")
        json_end = raw.rfind("}")
        if json_start < 0 or json_end <= json_start:
            return None
        try:
            value = json.loads(raw[json_start:json_end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _extract_tag_text(content: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}\b[^>]*>\s*(.*?)\s*</{tag}>", content, re.S)
    if not match:
        return None
    return match.group(1).strip()


def _format_tool_result_summary(content: str) -> str:
    return format_tool_result(content).summary


def _format_message_list_summary(value: Any, *, head: str, noun: str) -> str:
    if not isinstance(value, list) or not value:
        return ""
    details: list[str] = []
    times = [
        str(item.get("time") or item.get("timestamp") or item.get("created_at") or "").strip()
        for item in value
        if isinstance(item, dict)
    ]
    times = [item for item in times if item]
    if times:
        details.append(times[0] if times[0] == times[-1] else f"{times[0]}-{times[-1]}")
    sources = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(
            item.get("conversation_id")
            or item.get("source")
            or item.get("group_id")
            or item.get("target_id")
            or ""
        ).strip()
        if source and source not in sources:
            sources.append(source)
    if sources:
        shown = "、".join(sources[:2])
        if len(sources) > 2:
            shown = f"{shown} 等 {len(sources)} 个来源"
        details.append(f"来源 {shown}")
    sample = _message_list_sample(value)
    if sample:
        details.append(f"样例：{sample}")
    suffix = f"（{'；'.join(details)}）" if details else ""
    return f"{head} {len(value)} 条{noun}{suffix}"


def _message_list_sample(value: list[Any]) -> str:
    first = next((item for item in value if isinstance(item, dict)), None)
    if first is None:
        return ""
    sender = str(
        first.get("sender_name")
        or first.get("nickname")
        or first.get("user_id")
        or first.get("sender_id")
        or ""
    ).strip()
    text = str(
        first.get("text")
        or first.get("content")
        or first.get("message")
        or first.get("raw_message")
        or ""
    ).strip()
    text = _short_text(_compact_inline_tokens(text), limit=48)
    if sender and text:
        return f"{sender}: {text}"
    return sender or text


def _compact_inline_tokens(content: str) -> str:
    json_compact = _compact_json_blob(content)
    if json_compact != content:
        return json_compact
    compact = _URL_RE.sub(lambda m: _compact_url(m.group(0)), content)
    compact = _WINDOWS_PATH_RE.sub(lambda m: _compact_path(m.group(0)), compact)
    compact = _WORKSPACE_PATH_RE.sub(lambda m: _compact_workspace_path(m.group(0)), compact)
    return compact


def _compact_json_blob(content: str) -> str:
    stripped = content.strip()
    if len(stripped) <= INLINE_PREVIEW_LIMIT:
        return content
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return content
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return content
    kind = "JSON对象" if isinstance(value, dict) else "JSON数组"
    if isinstance(value, dict):
        preview_keys = "、".join(str(key) for key in list(value)[:4])
        suffix = f" · {preview_keys}" if preview_keys else ""
    elif isinstance(value, list):
        suffix = f" · {len(value)} 项"
    else:
        suffix = ""
    return f"[{kind}{suffix} · {len(stripped)}字，已折叠]"


def _compact_url(value: str) -> str:
    if len(value) <= INLINE_PREVIEW_LIMIT:
        return value
    parsed = urlsplit(value)
    host = parsed.netloc or "url"
    tail = parsed.path.rsplit("/", 1)[-1] or parsed.path.strip("/") or "link"
    tail = tail[:24] + "..." if len(tail) > 27 else tail
    return f"[URL {host}/{tail} · {len(value)}字]"


def _compact_path(value: str) -> str:
    if len(value) <= INLINE_PREVIEW_LIMIT:
        return value
    tail = value.replace("/", "\\").rsplit("\\", 1)[-1] or "path"
    return f"[路径 {tail} · {len(value)}字]"


def _compact_workspace_path(value: str) -> str:
    if len(value) <= INLINE_PREVIEW_LIMIT:
        return value
    cleaned = value.removeprefix("workspace=")
    tail = cleaned.replace("\\", "/").rsplit("/", 1)[-1] or "workspace"
    return f"[workspace {tail} · {len(value)}字]"


def _first_nonempty_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _format_tool_call_for_display(tool_call: dict) -> str:
    return format_tool_call(tool_call).summary


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _format_private_send_args(args: dict[str, Any]) -> str:
    targets = args.get("targets")
    if not isinstance(targets, list):
        targets = []
    lines = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        qq = target.get("target_qq") or target.get("target_id") or "默认私聊"
        lines.append(f"向 {qq} 发送消息：{_message_with_delay(target)}")
    return "；".join(lines) or "发送私聊消息"


def _format_group_send_args(args: dict[str, Any]) -> str:
    group_id = args.get("group_id") or args.get("target_id") or "默认群"
    targets = args.get("targets")
    if not isinstance(targets, list):
        targets = []
    messages = [
        _message_with_delay(target)
        for target in targets
        if isinstance(target, dict)
    ]
    joined = "；".join(messages) if messages else "(空消息)"
    return f"在群 {group_id} 发送消息：{joined}"


def _message_with_delay(target: dict[str, Any]) -> str:
    content = str(target.get("content") or "")
    delay = target.get("delay")
    if delay in (None, 0, 0.0, ""):
        return content
    return f"{content}（{delay}s）"


def _format_commit_send_attempt_args(args: dict[str, Any]) -> str:
    parts = []
    attempt_id = str(args.get("send_attempt_id") or "").strip()
    if attempt_id:
        parts.append(f"send_attempt_id={attempt_id}")
    if args.get("reviewed_until_seq") is not None:
        parts.append(f"已复核到 seq {args.get('reviewed_until_seq')}")
    policy = str(args.get("delivery_interrupt_policy") or "").strip()
    if policy:
        parts.append(f"中断策略 {policy}")
    reply_to = str(args.get("reply_to_message_id") or "").strip()
    if reply_to:
        parts.append(f"引用 msg_id={reply_to}")
    if args.get("ignore_review_interrupts") is True:
        parts.append("忽略复核打断")
    reason = str(args.get("reason") or "").strip()
    if reason:
        parts.append(f"原因：{_short_text(_compact_inline_tokens(reason), limit=60)}")
    detail = "；".join(parts) if parts else "无参数"
    return f"提交发送尝试：{detail}"


def _format_generic_tool_args(name: str, args: dict[str, Any]) -> str:
    if not args:
        return f"{name}：无参数"
    parts = []
    for key, value in args.items():
        parts.append(f"{key}={_format_tool_arg_value(value)}")
        if len(parts) >= 5:
            break
    if len(args) > len(parts):
        parts.append(f"另有 {len(args) - len(parts)} 项")
    return f"{name}：{'；'.join(parts)}"


def _format_tool_arg_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        sample = _message_list_sample(value)
        suffix = f"，样例：{sample}" if sample else ""
        return f"{len(value)} 项{suffix}"
    if isinstance(value, dict):
        keys = "、".join(str(key) for key in list(value)[:4])
        return f"对象({keys})" if keys else "对象"
    return _short_text(_compact_inline_tokens(str(value)), limit=80)


def _format_upload_args(args: dict[str, Any]) -> str:
    target_type = args.get("target_type") or "-"
    target_id = args.get("target_id") or "-"
    path = args.get("file_path") or args.get("path") or "-"
    return f"上传文件到 {target_type}:{target_id}：{path}"


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(s: str) -> str:
    return _escape(s).replace('"', "&quot;").replace("'", "&#x27;")


def _short_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."
