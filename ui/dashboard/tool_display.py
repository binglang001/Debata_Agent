"""工具调用/返回结果的人类可读展示。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

_TOOL_DISPLAY_CACHE_MAX_SIZE = 128
_TOOL_DISPLAY_CACHE_LOCK = threading.RLock()
_FORMAT_TOOL_CALL_CACHE: OrderedDict[tuple[Any, ...], ToolDisplay] = OrderedDict()
_FORMAT_TOOL_RESULT_CACHE: OrderedDict[tuple[Any, ...], ToolDisplay] = OrderedDict()


@dataclass(frozen=True, slots=True)
class ToolDisplay:
    tool_name: str
    title: str
    summary: str
    detail: str


def format_tool_call(tool_call: dict[str, Any]) -> ToolDisplay:
    cache_key = ("tool_call", _snapshot_cache_token(_tool_call_cache_content(tool_call)))
    cached = _cache_get(_FORMAT_TOOL_CALL_CACHE, cache_key)
    if cached is not None:
        return cached
    display = _format_tool_call_uncached(tool_call)
    _cache_put(_FORMAT_TOOL_CALL_CACHE, cache_key, display)
    return display


def _format_tool_call_uncached(tool_call: dict[str, Any]) -> ToolDisplay:
    func = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(func, dict):
        return ToolDisplay(
            tool_name="",
            title="未知工具调用",
            summary="未知工具调用",
            detail="工具调用：无法识别工具名称和参数。",
        )
    name = str(func.get("name") or "").strip() or "未知工具"
    args = _parse_tool_arguments(func.get("arguments"))

    if name == "commit_send_attempt":
        return _format_commit_send_attempt_call(name, args)
    if name == "send_private_messages":
        return _format_private_send_call(name, args)
    if name == "send_group_message":
        return _format_group_send_call(name, args)
    if name == "no_action":
        return ToolDisplay(
            tool_name=name,
            title="不发送消息",
            summary="选择不发送消息",
            detail="工具调用：不发送消息。本轮不需要向 QQ 投递内容。",
        )
    if name == "upload_file":
        target_type = _value_text(args.get("target_type") or "-")
        target_id = _value_text(args.get("target_id") or "-")
        path = _value_text(args.get("file_path") or args.get("path") or "-")
        summary = f"上传文件到 {target_type}:{target_id}：{path}"
        return ToolDisplay(name, "上传文件", summary, f"工具调用：上传文件。目标：{target_type}:{target_id}。文件：{path}。")
    if name == "get_forward_msg":
        forward_id = _value_text(args.get("forward_id") or "-")
        return ToolDisplay(name, "读取合并转发", f"读取合并转发：{forward_id}", f"工具调用：读取合并转发。转发 ID：{forward_id}。")
    if name == "get_recent_chat_messages":
        target = _value_text(args.get("conversation_id") or "当前会话")
        limit = _value_text(args.get("limit") or 50)
        return ToolDisplay(name, "读取最近聊天记录", f"读取最近聊天记录：{target}，{limit} 条", f"工具调用：读取最近聊天记录。范围：{target}。数量：{limit} 条。")
    if name == "recall_history":
        target = _value_text(args.get("conversation_id") or "全部会话")
        keyword = _value_text(args.get("keyword") or "-")
        return ToolDisplay(name, "检索历史记录", f"检索历史记录：{target}，关键词={keyword}", f"工具调用：检索历史记录。范围：{target}。关键词：{keyword}。")
    if name in {"read_file", "write_file", "edit_file", "list_files"}:
        return _format_file_tool_call(name, args)
    if name == "run_python":
        code = _short_text(str(args.get("code") or "").strip().replace("\n", " "), limit=80)
        summary = f"运行 Python：{code}" if code else "运行 Python"
        return ToolDisplay(name, "运行 Python", summary, f"工具调用：运行 Python。代码摘要：{code or '空'}。")
    return _format_generic_tool_call(name, args)


def format_tool_result(
    content: str,
    *,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
) -> ToolDisplay:
    del tool_call_id
    name = (tool_name or "").strip()
    cache_key = ("tool_result", name, _snapshot_cache_token(content))
    cached = _cache_get(_FORMAT_TOOL_RESULT_CACHE, cache_key)
    if cached is not None:
        return cached
    display = _format_tool_result_uncached(content, tool_name=name)
    _cache_put(_FORMAT_TOOL_RESULT_CACHE, cache_key, display)
    return display


def _format_tool_result_uncached(
    content: str,
    *,
    tool_name: str | None = None,
) -> ToolDisplay:
    payload = _parse_json_payload(content)
    name = (tool_name or "").strip()
    if name == "commit_send_attempt" or _looks_like_commit_send_attempt_result(payload):
        return _format_commit_send_attempt_result(name, payload, content)
    if isinstance(payload, dict):
        return _format_generic_tool_result(name, payload)
    text = _first_nonempty_line(content) or "(空结果)"
    detail = f"工具返回：文本结果：{_short_text(text, limit=260)}。"
    return ToolDisplay(name, "工具返回", _short_text(text, limit=96), detail)


def summarize_value(value: Any) -> str:
    return _summarize_value(value)


def _cache_get(cache: OrderedDict[tuple[Any, ...], ToolDisplay], key: tuple[Any, ...]) -> ToolDisplay | None:
    with _TOOL_DISPLAY_CACHE_LOCK:
        display = cache.get(key)
        if display is not None:
            cache.move_to_end(key)
        return display


def _cache_put(cache: OrderedDict[tuple[Any, ...], ToolDisplay], key: tuple[Any, ...], display: ToolDisplay) -> None:
    with _TOOL_DISPLAY_CACHE_LOCK:
        cache[key] = display
        cache.move_to_end(key)
        while len(cache) > _TOOL_DISPLAY_CACHE_MAX_SIZE:
            cache.popitem(last=False)


def _tool_call_cache_content(tool_call: Any) -> tuple[Any, ...]:
    if not isinstance(tool_call, dict):
        return ("invalid", tool_call)
    func = tool_call.get("function")
    if not isinstance(func, dict):
        return ("invalid_function", func)
    return ("function", func.get("name"), func.get("arguments"))


def _snapshot_cache_token(value: Any) -> str:
    hasher = hashlib.blake2b(digest_size=16)
    _update_snapshot_hash(hasher, value)
    return hasher.hexdigest()


def _update_snapshot_hash(hasher: Any, value: Any) -> None:
    if value is None:
        hasher.update(b"none;")
        return
    if isinstance(value, bool):
        hasher.update(b"bool:1;" if value else b"bool:0;")
        return
    if isinstance(value, str):
        _hash_bytes(hasher, b"str", value.encode("utf-8", "surrogatepass"))
        return
    if isinstance(value, bytes):
        _hash_bytes(hasher, b"bytes", value)
        return
    if isinstance(value, int):
        _hash_text(hasher, b"int", str(value))
        return
    if isinstance(value, float):
        _hash_text(hasher, b"float", repr(value))
        return
    if isinstance(value, dict):
        _hash_text(hasher, b"dict-len", str(len(value)))
        for key, item in value.items():
            hasher.update(b"key:")
            _update_snapshot_hash(hasher, key)
            hasher.update(b"value:")
            _update_snapshot_hash(hasher, item)
        return
    if isinstance(value, list):
        _hash_text(hasher, b"list-len", str(len(value)))
        for item in value:
            _update_snapshot_hash(hasher, item)
        return
    if isinstance(value, tuple):
        _hash_text(hasher, b"tuple-len", str(len(value)))
        for item in value:
            _update_snapshot_hash(hasher, item)
        return
    if isinstance(value, (set, frozenset)):
        child_tokens = sorted(_snapshot_cache_token(item) for item in value)
        _hash_text(hasher, b"set-len", str(len(child_tokens)))
        for token in child_tokens:
            _hash_text(hasher, b"set-item", token)
        return
    _hash_text(hasher, b"object", f"{type(value).__module__}.{type(value).__qualname__}:{value!s}")


def _hash_text(hasher: Any, marker: bytes, value: str) -> None:
    _hash_bytes(hasher, marker, value.encode("utf-8", "surrogatepass"))


def _hash_bytes(hasher: Any, marker: bytes, value: bytes) -> None:
    hasher.update(marker)
    hasher.update(b":")
    hasher.update(str(len(value)).encode("ascii"))
    hasher.update(b":")
    hasher.update(value)
    hasher.update(b";")


def _clear_tool_display_caches() -> None:
    with _TOOL_DISPLAY_CACHE_LOCK:
        _FORMAT_TOOL_CALL_CACHE.clear()
        _FORMAT_TOOL_RESULT_CACHE.clear()


def _format_commit_send_attempt_call(name: str, args: dict[str, Any]) -> ToolDisplay:
    attempt_id = _value_text(args.get("send_attempt_id") or args.get("attempt_id") or "")
    reviewed_until = args.get("reviewed_until_seq")
    ignore_interrupts = args.get("ignore_review_interrupts") is True
    reason = _value_text(args.get("reason") or "")
    reply_to = _value_text(args.get("reply_to_message_id") or "")

    summary_parts = []
    detail_parts = ["工具调用：提交发送尝试"]
    if attempt_id:
        summary_parts.append(f"ID {attempt_id}")
        detail_parts.append(f"ID 为 {attempt_id}")
    if reviewed_until is not None:
        summary_parts.append(f"已阅读到编号 {reviewed_until}")
        detail_parts.append(f"已阅读到编号 {reviewed_until}")
    if reply_to:
        detail_parts.append(f"引用消息 {reply_to}")
    if ignore_interrupts:
        summary_parts.append("忽略打断")
        detail_parts.append("忽略打断")
    if reason:
        summary_parts.append(f"原因：{_short_text(reason, limit=80)}")

    detail = "，".join(detail_parts) + "。"
    if reason:
        detail += f"原因：{reason}。"
    summary = "提交发送尝试"
    if summary_parts:
        summary = f"{summary}：" + "；".join(summary_parts)
    return ToolDisplay(name, "提交被打断的消息", summary, detail)


def _format_private_send_call(name: str, args: dict[str, Any]) -> ToolDisplay:
    targets = args.get("targets")
    if not isinstance(targets, list):
        targets = []
    lines = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        qq = _value_text(target.get("target_qq") or target.get("target_id") or "默认私聊")
        lines.append(f"向 {qq} 发送消息：{_message_with_delay(target)}")
    summary = "；".join(lines) or "发送私聊消息"
    detail = "工具调用：发送私聊消息。"
    if lines:
        detail += "；".join(lines) + "。"
    return ToolDisplay(name, "发送私聊消息", summary, detail)


def _format_group_send_call(name: str, args: dict[str, Any]) -> ToolDisplay:
    group_id = _value_text(args.get("group_id") or args.get("target_id") or "默认群")
    targets = args.get("targets")
    if not isinstance(targets, list):
        targets = []
    messages = [_message_with_delay(target) for target in targets if isinstance(target, dict)]
    joined = "；".join(messages) if messages else "(空消息)"
    summary = f"在群 {group_id} 发送消息：{joined}"
    return ToolDisplay(name, "发送群消息", summary, f"工具调用：发送群消息。群：{group_id}。内容：{joined}。")


def _format_file_tool_call(name: str, args: dict[str, Any]) -> ToolDisplay:
    labels = {
        "read_file": ("读取文件", "读取文件"),
        "write_file": ("写入文件", "写入文件"),
        "edit_file": ("编辑文件", "编辑文件"),
        "list_files": ("列出文件", "列出文件"),
    }
    title, action = labels.get(name, ("文件工具", name))
    path = _value_text(args.get("path") or ".")
    pattern = _value_text(args.get("pattern") or "*")
    if name == "list_files":
        summary = f"{action}：{path} / {pattern}"
        detail = f"工具调用：{action}。路径：{path}。匹配：{pattern}。"
    else:
        summary = f"{action}：{path}"
        detail = f"工具调用：{action}。路径：{path}。"
    return ToolDisplay(name, title, summary, detail)


def _format_generic_tool_call(name: str, args: dict[str, Any]) -> ToolDisplay:
    if not args:
        return ToolDisplay(name, f"调用工具：{name}", f"{name}：无参数", f"工具调用：{name}。没有参数。")
    parts = _summarize_mapping(args)
    summary = f"{name}：" + "；".join(parts)
    detail = f"工具调用：{name}。参数：" + "；".join(parts) + "。"
    return ToolDisplay(name, f"调用工具：{name}", summary, detail)


def _format_commit_send_attempt_result(
    name: str,
    payload: Any,
    content: str,
) -> ToolDisplay:
    if not isinstance(payload, dict):
        text = _first_nonempty_line(content) or "(空结果)"
        return ToolDisplay(name, "工具返回", _short_text(text, limit=96), f"工具返回：{_short_text(text, limit=260)}。")

    status = _status_label(_value_text(payload.get("status") or ("ok" if payload.get("ok") is True else "")))
    first_sentence = [f"工具返回：工具状态：{status or '未知'}"]
    summary_parts = [f"工具状态：{status or '未知'}"]
    send_id = _value_text(payload.get("send_id") or "")
    attempt_id = _value_text(payload.get("send_attempt_id") or "")
    if send_id:
        first_sentence.append(f"发送 ID：{send_id}")
        summary_parts.append(f"发送 ID：{send_id}")
    if attempt_id:
        first_sentence.append(f"打断消息 ID：{attempt_id}")
        summary_parts.append(f"打断消息 ID：{attempt_id}")

    state_parts = []
    delivery = _value_text(payload.get("delivery") or "")
    if delivery == "pending":
        state_parts.append("正在投递")
    elif delivery:
        state_parts.append(f"投递状态：{delivery}")
    qq_visible = payload.get("qq_visible")
    if qq_visible == "pending":
        state_parts.append("QQ 可见性待确认")
    elif qq_visible is False:
        state_parts.append("QQ 不可见")
    elif qq_visible is True:
        state_parts.append("QQ 已可见")

    count_parts = []
    sent = payload.get("sent")
    if isinstance(sent, list):
        count_parts.append(f"已发送 {len(sent)} 条")
    attempted = payload.get("attempted_messages")
    if isinstance(attempted, list):
        count_parts.append(f"待发送/尝试 {len(attempted)} 条")
    accepted = payload.get("accepted") or payload.get("queued")
    if isinstance(accepted, list):
        count_parts.append(f"排队/待确认 {len(accepted)} 条")
    accepted_messages = payload.get("accepted_messages")
    if isinstance(accepted_messages, list):
        count_parts.append(f"已接受消息 {len(accepted_messages)} 条")
    new_messages = payload.get("new_messages") or payload.get("new_visible_messages")
    if isinstance(new_messages, list):
        count_parts.append(f"发现 {len(new_messages)} 条新消息")

    interrupt_parts = [
        part
        for part in (
            _format_interrupts(payload.get("forced_unseen_messages"), action="已忽略", noun="复核打断"),
            _format_interrupts(payload.get("unseen_messages"), action="发现", noun="新打断"),
            _format_interrupts(payload.get("priority_interrupts"), action="发现", noun="高优先级打断"),
        )
        if part
    ]

    detail_sentences = ["，".join(first_sentence) + "。"]
    if state_parts:
        detail_sentences.append("，".join(state_parts) + "。")
    if count_parts:
        detail_sentences.append("；".join(count_parts) + "。")
    detail_sentences.extend(f"{part}。" for part in interrupt_parts)
    note = _value_text(payload.get("next") or payload.get("note") or "")
    if note:
        detail_sentences.append(f"提示：{note}。")

    summary_parts.extend(state_parts)
    summary_parts.extend(count_parts)
    summary_parts.extend(interrupt_parts)
    return ToolDisplay(
        name,
        "工具返回",
        "；".join(summary_parts) if summary_parts else "工具返回",
        "".join(detail_sentences),
    )


def _format_generic_tool_result(name: str, payload: dict[str, Any]) -> ToolDisplay:
    parts = []
    handled_keys = {"status", "ok"}
    status = payload.get("status")
    if status is not None:
        parts.append(f"状态 {_value_text(status)}")
    elif "ok" in payload:
        parts.append("成功" if payload.get("ok") else "失败")
    delivery = _value_text(payload.get("delivery") or "")
    if delivery:
        handled_keys.add("delivery")
        parts.append("正在投递" if delivery == "pending" else f"投递 {delivery}")
    if "qq_visible" in payload:
        handled_keys.add("qq_visible")
        qq_visible = payload.get("qq_visible")
        if qq_visible == "pending":
            parts.append("QQ 可见性待确认")
        elif qq_visible is False:
            parts.append("QQ 不可见")
        elif qq_visible is True:
            parts.append("QQ 已可见")
        else:
            parts.append(f"QQ 可见性 {_value_text(qq_visible)}")
    parts.extend(_summarize_mapping({key: value for key, value in payload.items() if key not in handled_keys}))
    summary = "；".join(parts) if parts else "工具返回"
    return ToolDisplay(name, "工具返回", summary, "工具返回：" + summary + "。")


def _summarize_mapping(values: dict[str, Any], *, limit: int = 5) -> list[str]:
    parts = []
    for key, value in values.items():
        parts.append(_summarize_key_value(str(key), _summarize_value(value)))
        if len(parts) >= limit:
            break
    extra = len(values) - len(parts)
    if extra > 0:
        parts.append(f"另有 {extra} 项")
    return parts


def _summarize_key_value(key: str, value_summary: str) -> str:
    if value_summary.startswith(("列表", "对象")):
        return f"{key} 为{value_summary}"
    return f"{key} 为 {value_summary}"


def _summarize_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        sample = _list_sample(value)
        suffix = f"，样例：{sample}" if sample else ""
        return f"列表 {len(value)} 项{suffix}"
    if isinstance(value, dict):
        keys = "、".join(str(key) for key in list(value)[:4])
        return f"对象，包含 {keys}" if keys else "对象"
    return _short_text(_value_text(value), limit=96)


def _format_interrupts(value: Any, *, action: str, noun: str) -> str:
    if not isinstance(value, list) or not value:
        return ""
    grouped: OrderedDict[tuple[str, str], int] = OrderedDict()
    for item in value:
        if not isinstance(item, dict):
            continue
        sender = _message_sender(item)
        source = _message_source(item)
        grouped[(sender, source)] = grouped.get((sender, source), 0) + 1
    if not grouped:
        return f"{action} {len(value)} 条{noun}"
    details = []
    for (sender, source), count in list(grouped.items())[:3]:
        if sender and source:
            details.append(f"{count} 条来自 {sender}（{source}）")
        elif sender:
            details.append(f"{count} 条来自 {sender}")
        elif source:
            details.append(f"{count} 条来自 {source}")
        else:
            details.append(f"{count} 条来源未知")
    extra_sources = len(grouped) - len(details)
    if extra_sources > 0:
        details.append(f"另有 {extra_sources} 个来源")
    return f"{action} {len(value)} 条{noun}：其中 " + "，".join(details)


def _message_sender(item: dict[str, Any]) -> str:
    return _value_text(
        item.get("sender_name")
        or item.get("nickname")
        or item.get("user_name")
        or item.get("sender_id")
        or item.get("user_id")
        or ""
    )


def _message_source(item: dict[str, Any]) -> str:
    conversation_id = _value_text(item.get("conversation_id") or "")
    if conversation_id.startswith("private:"):
        return f"私聊 {conversation_id.split(':', 1)[1]}"
    if conversation_id.startswith("group:"):
        return f"群聊 {conversation_id.split(':', 1)[1]}"
    group_id = _value_text(item.get("group_id") or "")
    if group_id:
        return f"群聊 {group_id}"
    target_id = _value_text(item.get("target_id") or item.get("user_id") or item.get("sender_id") or "")
    if target_id:
        return f"私聊 {target_id}"
    return conversation_id


def _list_sample(value: list[Any]) -> str:
    first = next((item for item in value if item is not None), None)
    if isinstance(first, dict):
        keys = "、".join(str(key) for key in list(first)[:3])
        return f"对象({keys})" if keys else "对象"
    if first is None:
        return ""
    return _short_text(_value_text(first), limit=32)


def _message_with_delay(target: dict[str, Any]) -> str:
    content = _value_text(target.get("content") or "")
    delay = target.get("delay")
    if delay in (None, 0, 0.0, ""):
        return content
    return f"{content}（{delay}s）"


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


def _parse_json_payload(content: str) -> Any:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def _looks_like_commit_send_attempt_result(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = {
        "send_id",
        "send_attempt_id",
        "ignored_review_interrupts",
        "forced_unseen_messages",
        "unseen_messages",
        "priority_interrupts",
    }
    return any(key in payload for key in keys)


def _status_label(status: str) -> str:
    labels = {
        "ok": "已接受",
        "sent": "已发送",
        "accepted": "已接受",
        "queued": "排队中",
        "pending": "等待确认",
        "stale": "已过期",
        "failed": "失败",
        "needs_review": "等待复核",
        "needs_review_again": "等待再次复核",
        "done": "完成",
    }
    return labels.get(status, status)


def _value_text(value: Any) -> str:
    return str(value).strip()


def _first_nonempty_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _short_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."
