"""合并转发读取、解析和渲染 helper。"""

from __future__ import annotations

import json
import re
import time
from html import unescape
from typing import Any

_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PARAM_SPLIT_RE = re.compile(r",(?=\w+=)")


async def build_forward_tree(
    adapter: Any,
    forward_id: str,
    *,
    recursive: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """读取合并转发并保留嵌套结构。供工具和后台子 Agent 共用。"""
    return await _fetch_forward_tree(
        adapter,
        str(forward_id),
        depth=0,
        max_depth=max(0, min(int(max_depth), 5)),
        recursive=bool(recursive),
        seen=set(),
    )


async def _fetch_forward_tree(
    adapter: Any,
    forward_id: str,
    *,
    depth: int,
    max_depth: int,
    recursive: bool,
    seen: set[str],
) -> dict[str, Any]:
    if not forward_id:
        return {
            "type": "forward",
            "forward_id": forward_id,
            "status": "failed",
            "depth": depth,
            "error": "empty forward_id",
            "message_count": 0,
            "messages": [],
        }
    if forward_id in seen:
        return {
            "type": "forward",
            "forward_id": forward_id,
            "status": "failed",
            "depth": depth,
            "error": "cycle detected",
            "message_count": 0,
            "messages": [],
        }

    seen.add(forward_id)
    try:
        raw_messages = await adapter.get_forward_msg(forward_id)
    except NotImplementedError:
        raise
    except Exception as e:
        status = "expired" if _looks_forward_expired(e) else "failed"
        return {
            "type": "forward",
            "forward_id": forward_id,
            "status": status,
            "depth": depth,
            "error": str(e),
            "message_count": 0,
            "messages": [],
        }
    finally:
        seen.discard(forward_id)

    if not isinstance(raw_messages, list):
        raw_messages = []

    messages: list[dict[str, Any]] = []
    tree = {
        "type": "forward",
        "forward_id": forward_id,
        "status": "ok",
        "depth": depth,
        "message_count": len(raw_messages),
        "messages": messages,
    }
    for index, raw in enumerate(raw_messages, start=1):
        message = _normalize_forward_message(raw, index=index)
        for segment in message["segments"]:
            if segment.get("type") != "forward":
                continue
            nested_id = str(segment.get("forward_id") or segment.get("id") or "")
            if not nested_id:
                continue
            if not recursive:
                segment["expanded"] = False
                segment["not_expanded_reason"] = "recursive disabled"
                continue
            if depth >= max_depth:
                segment["expanded"] = False
                segment["not_expanded_reason"] = "max_depth reached"
                continue
            segment["expanded"] = True
            segment["node"] = await _fetch_forward_tree(
                adapter,
                nested_id,
                depth=depth + 1,
                max_depth=max_depth,
                recursive=recursive,
                seen=set(seen) | {forward_id},
            )
        messages.append(message)
    return tree


def write_forward_artifact(
    workspace_dir: Any,
    tree: dict[str, Any],
    *,
    output: str = "json",
    prefix: str | None = None,
) -> Any:
    out_dir = workspace_dir / "runtime" / "forwards"
    out_dir.mkdir(parents=True, exist_ok=True)
    forward_id = str(tree.get("forward_id") or "forward")
    safe_id = _SAFE_PATH_RE.sub("_", forward_id).strip("._") or "forward"
    safe_prefix = _SAFE_PATH_RE.sub("_", prefix or "forward").strip("._") or "forward"
    suffix = ".md" if output == "markdown" else ".json"
    path = out_dir / f"{safe_prefix}_{safe_id}_{int(time.time() * 1000)}{suffix}"
    if output == "markdown":
        path.write_text(forward_tree_to_markdown(tree), encoding="utf-8")
    else:
        path.write_text(
            json.dumps(tree, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return path


def summarize_forward_tree(tree: dict[str, Any]) -> dict[str, int]:
    summary = {
        "message_count": 0,
        "nested_forward_count": 0,
        "expired_forward_count": 0,
        "failed_forward_count": 0,
        "image_count": 0,
    }
    _walk_forward_summary(tree, summary, include_current=True)
    return summary


def _walk_forward_summary(
    node: dict[str, Any],
    summary: dict[str, int],
    *,
    include_current: bool,
) -> None:
    if include_current:
        summary["message_count"] += int(node.get("message_count") or 0)
        status = str(node.get("status") or "")
        if status == "expired":
            summary["expired_forward_count"] += 1
        elif status not in {"", "ok"}:
            summary["failed_forward_count"] += 1

    for message in node.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for segment in message.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") in {"image", "face"}:
                summary["image_count"] += 1
            if segment.get("type") == "forward":
                summary["nested_forward_count"] += 1
                nested = segment.get("node")
                if isinstance(nested, dict):
                    _walk_forward_summary(nested, summary, include_current=True)


def forward_preview(tree: dict[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
    messages = [m for m in tree.get("messages") or [] if isinstance(m, dict)]
    if len(messages) <= limit:
        selected = messages
    else:
        half = max(1, limit // 2)
        selected = messages[:half] + messages[-half:]
    return [_preview_forward_message(m) for m in selected]


def _preview_forward_message(message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    return {
        "index": message.get("index"),
        "sender": sender.get("nickname") or sender.get("user_id") or "未知",
        "message_id": message.get("message_id"),
        "segments": [
            _preview_segment(seg)
            for seg in (message.get("segments") or [])[:6]
            if isinstance(seg, dict)
        ],
    }


def _preview_segment(segment: dict[str, Any]) -> dict[str, Any]:
    seg_type = str(segment.get("type") or "unknown")
    if seg_type == "text":
        return {
            "type": "text",
            "text": _trim_preview_text(str(segment.get("text") or "")),
        }
    if seg_type == "forward":
        node = segment.get("node")
        status = node.get("status") if isinstance(node, dict) else None
        return {
            "type": "forward",
            "forward_id": segment.get("forward_id"),
            "expanded": segment.get("expanded", False),
            "status": status,
        }
    out = {"type": seg_type}
    for key in ("summary", "url", "file", "forward_id", "name", "file_size"):
        if segment.get(key):
            out[key] = segment[key]
    return out


def forward_tree_to_markdown(tree: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_forward_markdown(tree, lines, level=0)
    return "\n".join(lines) + ("\n" if lines else "")


def _append_forward_markdown(
    node: dict[str, Any],
    lines: list[str],
    *,
    level: int,
) -> None:
    prefix = "#" * min(6, level + 1)
    lines.append(
        f"{prefix} 合并转发 {node.get('forward_id')} "
        f"({node.get('status')}, {node.get('message_count', 0)} 条)"
    )
    if node.get("error"):
        lines.append(f"> 错误：{node.get('error')}")
    lines.append("")
    for message in node.get("messages") or []:
        if not isinstance(message, dict):
            continue
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        sender_text = sender.get("nickname") or sender.get("user_id") or "未知"
        content = " ".join(
            _segment_to_markdown(seg) for seg in message.get("segments") or []
        )
        lines.append(f"- {sender_text}：{content or '(空消息)'}")
        for seg in message.get("segments") or []:
            if isinstance(seg, dict) and isinstance(seg.get("node"), dict):
                lines.append("")
                _append_forward_markdown(seg["node"], lines, level=level + 1)
    lines.append("")


def _segment_to_markdown(segment: Any) -> str:
    if not isinstance(segment, dict):
        return str(segment)
    seg_type = str(segment.get("type") or "unknown")
    if seg_type == "text":
        return str(segment.get("text") or "")
    if seg_type == "forward":
        text = f"[合并转发 id={segment.get('forward_id')}]"
        node = segment.get("node")
        if isinstance(node, dict):
            text += f"({node.get('status')}, {node.get('message_count', 0)} 条)"
        elif segment.get("not_expanded_reason"):
            text += f"({segment.get('not_expanded_reason')})"
        return text
    parts = [seg_type]
    for key in ("summary", "url", "file", "name", "file_size"):
        if segment.get(key):
            parts.append(f"{key}={segment[key]}")
    return "[" + " ".join(parts) + "]"


def _normalize_forward_message(raw: Any, *, index: int) -> dict[str, Any]:
    raw_dict = raw if isinstance(raw, dict) else {"raw_message": str(raw)}
    sender_raw = raw_dict.get("sender") if isinstance(raw_dict.get("sender"), dict) else {}
    sender = {
        "nickname": sender_raw.get("nickname") or raw_dict.get("nickname") or "未知",
        "user_id": str(sender_raw.get("user_id") or raw_dict.get("user_id") or ""),
    }
    raw_message = raw_dict.get("raw_message")
    content = raw_dict.get("content")
    message = raw_dict.get("message")
    if raw_message in (None, "") and isinstance(content, str):
        raw_message = content
    return {
        "index": index,
        "sender": sender,
        "time": raw_dict.get("time") or raw_dict.get("timestamp"),
        "message_id": raw_dict.get("message_id") or raw_dict.get("msg_id"),
        "raw_message": str(raw_message or ""),
        "segments": _forward_segments(raw_message, content, message),
    }


def _forward_segments(
    raw_message: Any,
    content: Any,
    message: Any = None,
) -> list[dict[str, Any]]:
    if isinstance(message, list):
        segments = [_segment_from_onebot(seg) for seg in message]
        return [seg for seg in segments if seg]
    if isinstance(content, list):
        segments = [_segment_from_onebot(seg) for seg in content]
        return [seg for seg in segments if seg]
    if isinstance(raw_message, str) and raw_message:
        return _segments_from_raw(raw_message)
    if isinstance(content, str) and content:
        return _segments_from_raw(content)
    return []


def _segment_from_onebot(segment: Any) -> dict[str, Any]:
    if not isinstance(segment, dict):
        return {"type": "text", "text": str(segment)}
    seg_type = str(segment.get("type") or "unknown")
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    if seg_type == "text":
        return {"type": "text", "text": str(data.get("text") or "")}
    if seg_type == "forward":
        forward_id = str(data.get("id") or segment.get("id") or "")
        return {
            "type": "forward",
            "forward_id": forward_id,
            "raw": segment,
        }
    if seg_type in {"image", "face", "file", "record", "voice", "video"}:
        return _media_segment(seg_type, data, raw=segment)
    return {"type": seg_type, "data": data, "raw": segment}


def _segments_from_raw(raw_message: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    pos = 0
    for segment_match in _iter_cq_segments(raw_message):
        if segment_match["start"] > pos:
            text = raw_message[pos : segment_match["start"]]
            if text:
                segments.append({"type": "text", "text": unescape(text)})
        body = segment_match["body"]
        if "," in body:
            cq_type, params_str = body.split(",", 1)
        else:
            cq_type, params_str = body, ""
        params = _parse_cq_params(params_str)
        if cq_type == "forward":
            segments.append(
                {
                    "type": "forward",
                    "forward_id": params.get("id") or "",
                    "raw": segment_match["raw"],
                }
            )
        elif cq_type in {"image", "face", "file", "record", "voice", "video"}:
            segments.append(_media_segment(cq_type, params, raw=segment_match["raw"]))
        else:
            segments.append({"type": cq_type, "data": params, "raw": segment_match["raw"]})
        pos = int(segment_match["end"])
    if pos < len(raw_message):
        text = raw_message[pos:]
        if text:
            segments.append({"type": "text", "text": unescape(text)})
    if not segments and raw_message:
        segments.append({"type": "text", "text": unescape(raw_message)})
    return segments


def _iter_cq_segments(raw_message: str) -> list[dict[str, Any]]:
    """扫描 CQ 段，容忍参数值里未转义的 `]`。

    OneBot 正常会把 summary 里的方括号转义成 HTML 实体，但 NapCat/转发
    内容里偶尔会出现 `summary=[图片]` 这种未转义值。结束 `]` 后若紧跟
    `,key=`，说明它仍属于参数值，不应提前截断。
    """
    segments: list[dict[str, Any]] = []
    i = 0
    while True:
        start = raw_message.find("[CQ:", i)
        if start < 0:
            break
        pos = start + 4
        while True:
            end = raw_message.find("]", pos)
            if end < 0:
                return segments
            tail = raw_message[end + 1 :]
            if not re.match(r"^,\w+=", tail):
                break
            pos = end + 1
        raw = raw_message[start : end + 1]
        segments.append(
            {
                "start": start,
                "end": end + 1,
                "body": raw_message[start + 4 : end],
                "raw": raw,
            }
        )
        i = end + 1
    return segments


def _media_segment(seg_type: str, data: dict[str, Any], *, raw: Any) -> dict[str, Any]:
    normalized_type = "voice" if seg_type in {"record", "voice"} else seg_type
    item: dict[str, Any] = {"type": normalized_type}
    for src, dst in (
        ("summary", "summary"),
        ("url", "url"),
        ("file", "file"),
        ("file_id", "file"),
        ("name", "name"),
        ("file_name", "name"),
        ("sub_type", "sub_type"),
        ("file_size", "file_size"),
        ("id", "id"),
    ):
        if data.get(src) not in (None, ""):
            item[dst] = data.get(src)
    if normalized_type == "forward" and item.get("id"):
        item["forward_id"] = item["id"]
    if raw:
        item["raw"] = raw
    return item


def _parse_cq_params(params_str: str) -> dict[str, str]:
    params: dict[str, str] = {}
    if not params_str:
        return params
    for part in _PARAM_SPLIT_RE.split(params_str):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        params[key] = unescape(value)
    return params


def _looks_forward_expired(error: Exception) -> bool:
    text = str(error)
    return any(
        marker in text
        for marker in ("retcode=1200", "消息已过期", "内层消息", "过期")
    )


def _trim_preview_text(text: str, limit: int = 120) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...（截断预览）"
