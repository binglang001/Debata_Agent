"""平台查询/操作类工具。

包括：
    - list_contacts: 列好友/群/群成员
    - get_user_info: 获取陌生人信息
    - get_forward_msg: 提取合并转发
    - get_recent_chat_messages: 读取当前运行期真实 QQ 可见聊天时间线
    - set_friend_add_request: 处理加好友请求
    - set_group_add_request: 处理加群请求
    - summarize_chat_history: 拉取 NapCat/QQ 服务器侧近期群历史并启动后台子 Agent
    - summarize_conversation: 用后台子 Agent 总结本地归档和活跃历史
    - recall_history: 从本地永久归档检索较早上下文
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, is_dataclass
from html import unescape
from typing import Any

from utils.token_budget import TokenEstimator

from .base import ToolContext, tool
from .result_shrink import tool_budget
from .schemas import (
    GetForwardMsgArgs,
    GetRecentChatMessagesArgs,
    GetUserInfoArgs,
    ListContactsArgs,
    RecallHistoryArgs,
    SetFriendRequestArgs,
    SetGroupRequestArgs,
    SummarizeChatArgs,
    SummarizeConversationArgs,
)
from .workspace import relative_to_workspace

logger = logging.getLogger(__name__)

_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PARAM_SPLIT_RE = re.compile(r",(?=\w+=)")


# ============================================================
# list_contacts
# ============================================================


def _to_dict(obj) -> dict:
    """把 dataclass 或 dict 统一转成普通 dict。"""
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    return {"value": obj}


@tool(
    name="list_contacts",
    description=(
        "查询 QQ 联系人信息。需要知道可私聊的好友、当前加入的群，或某个群里有哪些成员时使用；"
        "scope=group_members 时必须提供 group_id。不要用它读取聊天记录。"
    ),
    args_model=ListContactsArgs,
    category="platform",
)
async def list_contacts(args: ListContactsArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "未连接适配器，无法查询联系人。",
            "error": "未连接适配器",
        }

    try:
        if args.scope == "friends":
            friends = await ctx.adapter.list_friends()
            items = [
                {"nickname": f.nickname, "user_id": f.user_id} for f in friends
            ]
            return _paged_contacts_result(
                scope=args.scope,
                key="friends",
                items=items,
                offset=args.offset,
                limit=args.limit,
            )

        if args.scope == "groups":
            groups = await ctx.adapter.list_groups()
            items = [
                {"group_name": g.group_name, "group_id": g.group_id} for g in groups
            ]
            return _paged_contacts_result(
                scope=args.scope,
                key="groups",
                items=items,
                offset=args.offset,
                limit=args.limit,
            )

        if args.scope == "group_members":
            if args.group_id is None:
                return {
                    "ok": False,
                    "status": "failed",
                    "brief": "查询群成员需要提供 group_id。",
                    "error": "group_members 需要 group_id",
                }
            members = await ctx.adapter.list_group_members(str(args.group_id))
            items = [
                {
                    "nickname": m.nickname,
                    "card": m.display_name,
                    "user_id": m.user_id,
                }
                for m in members
            ]
            return _paged_contacts_result(
                scope=args.scope,
                key="members",
                items=items,
                offset=args.offset,
                limit=args.limit,
                group_id=str(args.group_id),
            )

    except Exception as e:
        logger.warning(f"list_contacts 失败: {e}")
        return {
            "ok": False,
            "status": "failed",
            "brief": f"联系人查询失败：{e}",
            "error": str(e),
        }

    return {
        "ok": False,
        "status": "failed",
        "brief": f"未知联系人查询范围：{args.scope}",
        "error": f"未知 scope: {args.scope}",
    }


def _paged_contacts_result(
    *,
    scope: str,
    key: str,
    items: list[dict[str, Any]],
    offset: int,
    limit: int,
    group_id: str | None = None,
) -> dict[str, Any]:
    total = len(items)
    start = min(offset, total)
    page = items[start : start + limit]
    result: dict[str, Any] = {
        "ok": True,
        "status": "inline",
        "brief": f"已查询 {scope}，返回 {len(page)}/{total} 条。",
        key: page,
        "count": total,
        "offset": start,
        "limit": limit,
        "data": {
            "scope": scope,
            "count": total,
            "returned": len(page),
            "offset": start,
        },
    }
    if group_id is not None:
        result["data"]["group_id"] = group_id
    next_offset = start + len(page)
    if next_offset < total:
        result["next_offset"] = next_offset
        result["next"] = f"继续调用 list_contacts，传 scope={scope!r}, offset={next_offset} 读取下一页。"
        result["data"]["next_offset"] = next_offset
    return result


# ============================================================
# get_user_info
# ============================================================


@tool(
    name="get_user_info",
    description="获取指定 QQ 用户的公开信息（昵称、性别、年龄等）。用于了解陌生人或新成员。",
    args_model=GetUserInfoArgs,
    category="platform",
)
async def get_user_info(args: GetUserInfoArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "未连接适配器，无法查询用户信息。",
            "error": "未连接适配器",
        }
    try:
        info = await ctx.adapter.get_user_info(str(args.user_id))
    except Exception as e:
        return {
            "ok": False,
            "status": "failed",
            "brief": f"查询用户 {args.user_id} 失败：{e}",
            "error": str(e),
        }
    public = _public_user_info(_to_dict(info))
    name = public.get("nickname") or args.user_id
    return {
        "ok": True,
        "status": "inline",
        "brief": f"已获取用户 {name}({args.user_id}) 的公开信息。",
        "info": public,
        "data": {
            "user_id": str(args.user_id),
            "field_count": len(public),
        },
    }


def _public_user_info(info: dict) -> dict:
    """只保留对回复有用的公开字段，丢弃 richBuffer/extBuffer 等噪声。"""
    extra = info.get("extra") if isinstance(info.get("extra"), dict) else {}
    public: dict = {}
    for key in ("user_id", "nickname", "sex", "age"):
        if info.get(key) not in (None, ""):
            public[key] = info.get(key)
    for src, dst in (
        ("qid", "qid"),
        ("qqLevel", "qq_level"),
        ("longNick", "signature"),
        ("long_nick", "signature"),
        ("country", "country"),
        ("province", "province"),
        ("city", "city"),
        ("regTime", "reg_time"),
        ("reg_time", "reg_time"),
        ("is_vip", "is_vip"),
        ("vip_level", "vip_level"),
    ):
        if extra.get(src) not in (None, "") and dst not in public:
            public[dst] = extra.get(src)
    return public


# ============================================================
# get_forward_msg
# ============================================================


@tool(
    name="get_forward_msg",
    description=(
        "提取合并转发消息的内容。当用户发送合并转发消息时，"
        "使用此工具获取其中每条消息的具体内容。"
    ),
    args_model=GetForwardMsgArgs,
    category="platform",
)
async def get_forward_msg(args: GetForwardMsgArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}
    if ctx.workspace_dir is None:
        return {"ok": False, "error": "workspace 未配置，无法保存合并转发完整结果"}

    try:
        tree = await build_forward_tree(
            ctx.adapter,
            args.forward_id,
            recursive=args.recursive,
            max_depth=args.max_depth,
        )
    except NotImplementedError:
        return {"ok": False, "error": "当前适配器不支持合并转发提取"}
    except Exception as e:
        logger.warning(f"get_forward_msg 失败: {e}")
        return {"ok": False, "error": str(e)}

    output = args.output if args.output in {"json", "markdown"} else "json"
    path = write_forward_artifact(
        ctx.workspace_dir,
        tree,
        output=output,
    )
    summary = summarize_forward_tree(tree)
    rel_path = relative_to_workspace(path, ctx.workspace_dir)
    result = {
        "ok": tree.get("status") == "ok",
        "status": "artifact",
        "brief": (
            f"已提取合并转发 {args.forward_id}："
            f"消息 {summary['message_count']} 条，"
            f"嵌套 {summary['nested_forward_count']} 个，"
            f"图片/表情 {summary['image_count']} 个；完整结构已写入 {rel_path}。"
        ),
        "artifact": {
            "path": rel_path,
            "type": output,
            "count": summary["message_count"],
        },
        "data": {
            "forward_id": args.forward_id,
            **summary,
        },
        "preview": forward_preview(tree),
        "next": "需要整理完整对话时，把 artifact.path 交给 start_agent_task；不要把 preview 当完整正文。",
    }
    if tree.get("status") != "ok":
        result["error"] = tree.get("error") or "合并转发读取失败"
    return result


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
    if raw_message in (None, "") and isinstance(content, str):
        raw_message = content
    return {
        "index": index,
        "sender": sender,
        "time": raw_dict.get("time") or raw_dict.get("timestamp"),
        "message_id": raw_dict.get("message_id") or raw_dict.get("msg_id"),
        "raw_message": str(raw_message or ""),
        "segments": _forward_segments(raw_message, content),
    }


def _forward_segments(raw_message: Any, content: Any) -> list[dict[str, Any]]:
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


# ============================================================
# get_recent_chat_messages
# ============================================================


@tool(
    name="get_recent_chat_messages",
    description=(
        "读取当前运行期真实 QQ 可见聊天记录。用于 stale 发送后确认当前会话真实消息状态，"
        "或在群聊/私聊消息散落时按连续时间线查看最近消息。只记录真实入站和已成功发出的出站消息；"
        "不会包含工具参数、未发出的草稿、模型思考或系统记录。"
    ),
    args_model=GetRecentChatMessagesArgs,
    category="platform",
)
async def get_recent_chat_messages(
    args: GetRecentChatMessagesArgs,
    ctx: ToolContext,
) -> dict:
    conversation_id = (args.conversation_id or ctx.conversation_id or "").strip()
    if not conversation_id:
        return {
            "ok": False,
            "error": "缺少 conversation_id，且当前工具上下文无法推断会话",
        }

    timeline = (ctx.extras or {}).get("chat_timeline")
    if timeline is None or not hasattr(timeline, "recent"):
        return {"ok": False, "error": "当前运行时未启用真实 QQ 聊天时间线"}

    messages = timeline.recent(
        conversation_id,
        args.limit,
        since_msg_id=args.since_msg_id,
        before_msg_id=args.before_msg_id,
    )
    markdown = timeline.to_markdown(messages, include_raw=args.include_raw)
    meta = _chat_timeline_meta(messages, conversation_id)
    budget = tool_budget("get_recent_chat_messages", ctx)
    estimator = TokenEstimator()
    inline_result = {
        "ok": True,
        "status": "inline",
        "brief": (
            f"已读取 {conversation_id} 最近 {len(messages)} 条真实 QQ 消息"
            "（连续窗口）。"
        ),
        "content": markdown,
        "data": meta,
    }
    if _estimate_result(inline_result, estimator) <= budget.inline:
        return inline_result

    if ctx.workspace_dir is None:
        return {
            "ok": False,
            "error": "聊天记录超过 inline 预算，但 workspace 未配置，无法写出完整文件",
            "data": meta,
        }

    path = _write_chat_timeline_artifact(
        ctx,
        conversation_id=conversation_id,
        markdown=markdown,
        meta=meta,
    )
    return {
        "ok": True,
        "status": "artifact",
        "brief": (
            f"聊天记录较长，已写入完整连续文件：{path}；"
            f"共 {len(messages)} 条。"
        ),
        "path": path,
        "data": meta,
    }


def _chat_timeline_meta(messages: list[Any], conversation_id: str) -> dict[str, Any]:
    first = messages[0] if messages else None
    last = messages[-1] if messages else None
    return {
        "conversation_id": conversation_id,
        "count": len(messages),
        "range": "continuous",
        "from": getattr(first, "time_text", None) if first else None,
        "to": getattr(last, "time_text", None) if last else None,
        "first_msg_id": getattr(first, "msg_id", None) if first else None,
        "last_msg_id": getattr(last, "msg_id", None) if last else None,
    }


def _estimate_result(result: dict[str, Any], estimator: TokenEstimator) -> int:
    return estimator.estimate_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True)
    )


def _write_chat_timeline_artifact(
    ctx: ToolContext,
    *,
    conversation_id: str,
    markdown: str,
    meta: dict[str, Any],
) -> str:
    if ctx.workspace_dir is None:
        raise RuntimeError("workspace 未配置")
    out_dir = ctx.workspace_dir / "runtime" / "chat_timeline"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_cid = _SAFE_PATH_RE.sub("_", conversation_id).strip("._") or "conversation"
    path = out_dir / f"{safe_cid}_{int(time.time() * 1000)}.md"
    header = (
        "# 真实 QQ 聊天记录\n\n"
        f"- 会话：{conversation_id}\n"
        f"- 消息数：{meta.get('count')}\n"
        f"- 范围：{meta.get('from') or '-'} ~ {meta.get('to') or '-'}\n"
        f"- first_msg_id：{meta.get('first_msg_id') or '-'}\n"
        f"- last_msg_id：{meta.get('last_msg_id') or '-'}\n\n"
        "---\n\n"
    )
    path.write_text(header + markdown + ("\n" if markdown else ""), encoding="utf-8")
    return relative_to_workspace(path, ctx.workspace_dir)


# ============================================================
# set_friend_add_request
# ============================================================


@tool(
    name="set_friend_add_request",
    description=(
        "处理 QQ 好友添加请求。只有系统通知里出现好友请求 flag，且管理员明确同意/拒绝后才能调用；"
        "普通聊天里不要主动调用。"
    ),
    args_model=SetFriendRequestArgs,
    category="platform",
    no_feedback=True,
)
async def set_friend_add_request(args: SetFriendRequestArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "处理好友请求失败：未连接适配器。",
            "error": "未连接适配器",
        }
    try:
        await ctx.adapter.handle_friend_request(
            flag=args.flag,
            approve=args.approve,
            remark=args.remark or "",
        )
    except Exception as e:
        return {
            "ok": False,
            "status": "failed",
            "brief": f"处理好友请求失败：{e}",
            "error": str(e),
        }
    action = "同意" if args.approve else "拒绝"
    return {
        "ok": True,
        "status": "done",
        "brief": f"已{action}好友请求。",
        "data": {
            "flag": args.flag,
            "approve": args.approve,
            "remark": args.remark or "",
        },
    }


# ============================================================
# set_group_add_request
# ============================================================


@tool(
    name="set_group_add_request",
    description=(
        "处理加群申请或邀请入群。只有系统通知里出现请求 flag/sub_type，且管理员明确同意/拒绝后才能调用；"
        "普通聊天里不要主动调用。"
    ),
    args_model=SetGroupRequestArgs,
    category="platform",
    no_feedback=True,
)
async def set_group_add_request(args: SetGroupRequestArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "处理群请求失败：未连接适配器。",
            "error": "未连接适配器",
        }
    logger.info(
        f"[FLAG追踪] AI调用 set_group_add_request: flag={args.flag!r}, "
        f"sub_type={args.sub_type!r}, approve={args.approve}"
    )
    try:
        await ctx.adapter.handle_group_request(
            flag=args.flag,
            sub_type=args.sub_type,
            approve=args.approve,
            reason=args.reason or "",
        )
    except Exception as e:
        return {
            "ok": False,
            "status": "failed",
            "brief": f"处理群请求失败：{e}",
            "error": str(e),
        }
    action = "同意" if args.approve else "拒绝"
    return {
        "ok": True,
        "status": "done",
        "brief": f"已{action}群请求。",
        "data": {
            "flag": args.flag,
            "sub_type": args.sub_type,
            "approve": args.approve,
            "reason": args.reason or "",
        },
    }


# ============================================================
# summarize_chat_history
# ============================================================


_DEFAULT_SUMMARIZE_PROMPT = (
    "请总结此群聊的："
    "1) 群成员概况（活跃度、角色等）"
    "2) 群聊基本信息"
    "3) 群内日常讨论内容方向"
    "4) 对各活跃成员的印象和性格分析"
)


@tool(
    name="summarize_chat_history",
    description=(
        "从 NapCat/QQ 服务器侧拉取指定群的近期群消息，并用子 Agent 整理。"
        "工具会等待子 Agent 完成，并在本次工具结果中返回摘要内容和结果文件路径。"
        "只适合补充本地归档之外的群聊近期历史；必须知道 group_id，不支持私聊。"
    ),
    args_model=SummarizeChatArgs,
    category="platform",
)
async def summarize_chat_history(args: SummarizeChatArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}
    if ctx.agent_task_cb is None:
        return {"ok": False, "error": "当前运行时不支持后台子 Agent 任务"}

    try:
        history = await ctx.adapter.get_group_history(
            str(args.group_id),
            count=ctx.default_history_fetch_count,
        )
    except NotImplementedError:
        return {"ok": False, "error": "当前适配器不支持群历史拉取"}
    except Exception as e:
        return {"ok": False, "error": f"获取群聊记录失败: {e}"}

    prompt = args.custom_prompt or _DEFAULT_SUMMARIZE_PROMPT
    return await ctx.agent_task_cb(
        {
            "prompt": prompt,
            "sources": [
                {
                    "type": "inline_json",
                    "data": {
                        "source": "group_history",
                        "group_id": args.group_id,
                        "messages": history,
                    },
                }
            ],
            "output_format": "markdown",
            "output_name": f"group_{args.group_id}_summary.md",
        }
    )


# ============================================================
# summarize_conversation
# ============================================================


_DEFAULT_LOCAL_SUMMARY_GOAL = (
    "请按时间线总结本地对话，提炼关键事实、长期约定、用户偏好、已经完成的决定、"
    "仍未完成的事项，以及可能需要后续主动跟进的内容。"
)


@tool(
    name="summarize_conversation",
    description=(
        "启动后台子 Agent 总结本地永久归档和当前活跃历史中的对话，私聊和群聊都可用。"
        "工具会等待子 Agent 完成，并在本次工具结果中返回摘要内容和结果文件路径。"
        "当用户要总结过去对话、查本地归档、或总结私聊历史时优先使用。"
    ),
    args_model=SummarizeConversationArgs,
    category="platform",
)
async def summarize_conversation(args: SummarizeConversationArgs, ctx: ToolContext) -> dict:
    if ctx.archive is None:
        return {"ok": False, "error": "未配置本地历史归档"}
    if ctx.agent_task_cb is None:
        return {"ok": False, "error": "当前运行时不支持后台子 Agent 任务"}

    conversation_id = args.conversation_id or ctx.conversation_id
    range_hint = (args.range_hint or "").strip()
    goal = (args.goal or "").strip() or _DEFAULT_LOCAL_SUMMARY_GOAL
    return await ctx.agent_task_cb(
        {
            "prompt": (
                "只根据提供的本地记录完成任务，不臆测未出现的信息。\n"
                f"范围线索：{range_hint or '未指定'}\n"
                f"任务目标：{goal}"
            ),
            "sources": [
                {
                    "type": "conversation_history",
                    "conversation_id": conversation_id,
                    "time_range": range_hint or None,
                    "limit": 200,
                }
            ],
            "output_format": "markdown",
            "output_name": "conversation_summary.md",
        }
    )


# ============================================================
# recall_history
# ============================================================


@tool(
    name="recall_history",
    description=(
        "从本地永久归档和当前活跃历史中检索较早的对话原文。"
        "当你需要想起已被压缩出工作窗口的旧细节、旧 msg_id、旧约定时使用；"
        "conversation_id 可限制到当前私聊/群聊，不填则全局检索。"
    ),
    args_model=RecallHistoryArgs,
    category="platform",
)
async def recall_history(args: RecallHistoryArgs, ctx: ToolContext) -> dict:
    if ctx.archive is None:
        return {"ok": False, "error": "未配置本地历史归档"}

    records = await ctx.archive.search(
        conversation_id=args.conversation_id,
        keyword=args.keyword,
        time_range=args.time_range,
        limit=args.limit,
    )
    if ctx.history is not None:
        for record in await ctx.history.records():
            if _history_record_matches(
                record,
                conversation_id=args.conversation_id,
                keyword=args.keyword,
                time_range=args.time_range,
            ):
                records.append(record)
        records = records[-args.limit:]

    markdown = _format_history_recall_markdown(records)
    snippets = [_history_recall_snippet(record) for record in records]
    meta = {
        "count": len(records),
        "conversation_id": args.conversation_id,
        "keyword": args.keyword,
        "time_range": args.time_range,
        "range": "continuous_result_order",
    }
    inline_result = {
        "ok": True,
        "status": "inline",
        "brief": f"找到 {len(records)} 条本地历史记录。",
        "count": len(snippets),
        "content": markdown,
        "results": snippets,
        "data": meta,
    }
    budget = tool_budget("recall_history", ctx)
    estimator = TokenEstimator()
    if _estimate_result(inline_result, estimator) <= budget.inline:
        return inline_result

    if ctx.workspace_dir is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "历史记录超过 inline 预算，但 workspace 未配置，无法写出完整文件。",
            "error": "历史记录超过 inline 预算，但 workspace 未配置",
            "count": len(snippets),
            "results": snippets,
            "data": meta,
        }

    path = _write_history_recall_artifact(
        ctx,
        markdown=markdown,
        meta=meta,
    )
    return {
        "ok": True,
        "status": "artifact",
        "brief": f"找到 {len(records)} 条本地历史记录，完整 Markdown 已写入 {path}。",
        "path": path,
        "artifact": {
            "path": path,
            "type": "markdown",
            "count": len(records),
        },
        "count": len(snippets),
        "results": snippets[:5],
        "data": meta,
        "next": "需要分析完整历史时，把 artifact.path 交给 start_agent_task 或用 read_file 分页读取。",
    }


def _history_record_matches(
    record: dict,
    *,
    conversation_id: str | None,
    keyword: str | None,
    time_range: str | None,
) -> bool:
    if conversation_id and record.get("conversation_id") != conversation_id:
        return False
    text = "\n".join(
        [
            str(record.get("content") or ""),
            str(record.get("metadata") or ""),
        ]
    )
    keyword = (keyword or "").strip()
    if keyword and keyword not in text:
        return False
    time_range = (time_range or "").strip()
    if time_range and time_range not in text:
        return False
    return True


def _format_history_recall_markdown(records: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for record in records:
        lines.extend(_format_history_recall_record(record))
    return "\n".join(lines)


def _format_history_recall_record(record: dict[str, Any]) -> list[str]:
    timestamp = _summary_timestamp(record) or "-"
    conversation_id = str(record.get("conversation_id") or "-")
    role = str(record.get("role") or "-")
    sender = _history_recall_sender(record, role)
    content = str(record.get("content") or "").strip()
    if not content:
        content = "(空内容)"
    lines = [
        f"{timestamp} [{conversation_id}] {sender}({role})：{line}"
        for line in content.splitlines()
    ]
    return lines or [f"{timestamp} [{conversation_id}] {sender}({role})：(空内容)"]


def _history_recall_sender(record: dict[str, Any], role: str) -> str:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        messages = meta.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict):
                nickname = first.get("nickname")
                user_id = first.get("user_id")
                if nickname and user_id:
                    return f"{nickname}({user_id})"
                if nickname:
                    return str(nickname)
        if meta.get("nickname"):
            return str(meta.get("nickname"))
    if role == "assistant":
        return "assistant"
    if role == "system":
        return "system"
    if role == "tool":
        return "tool"
    return "user"


def _history_recall_snippet(record: dict[str, Any]) -> dict[str, Any]:
    content = str(record.get("content") or "")
    return {
        "role": record.get("role"),
        "conversation_id": record.get("conversation_id"),
        "timestamp": _summary_timestamp(record) or None,
        "content": content[:160],
    }


def _write_history_recall_artifact(
    ctx: ToolContext,
    *,
    markdown: str,
    meta: dict[str, Any],
) -> str:
    if ctx.workspace_dir is None:
        raise RuntimeError("workspace 未配置")
    out_dir = ctx.workspace_dir / "runtime" / "history_recall"
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = str(meta.get("conversation_id") or "global")
    safe_scope = _SAFE_PATH_RE.sub("_", scope).strip("._") or "history"
    path = out_dir / f"{safe_scope}_{int(time.time() * 1000)}.md"
    header = (
        "# 本地历史检索结果\n\n"
        f"- 记录数：{meta.get('count')}\n"
        f"- 会话：{meta.get('conversation_id') or '-'}\n"
        f"- 关键词：{meta.get('keyword') or '-'}\n"
        f"- 时间范围：{meta.get('time_range') or '-'}\n\n"
        "---\n\n"
    )
    path.write_text(header + markdown + ("\n" if markdown else ""), encoding="utf-8")
    return relative_to_workspace(path, ctx.workspace_dir)


async def _local_summary_records(
    ctx: ToolContext,
    conversation_id: str | None,
    range_hint: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if ctx.archive is not None:
        for record in await ctx.archive.records():
            if _summary_record_matches(
                record,
                conversation_id=conversation_id,
                range_hint=range_hint,
            ):
                copied = dict(record)
                copied["_source"] = "archive"
                records.append(copied)
    if ctx.history is not None:
        for record in await ctx.history.records():
            if _summary_record_matches(
                record,
                conversation_id=conversation_id,
                range_hint=range_hint,
            ):
                copied = dict(record)
                copied["_source"] = "active"
                records.append(copied)
    return records


def _summary_record_matches(
    record: dict[str, Any],
    *,
    conversation_id: str | None,
    range_hint: str,
) -> bool:
    if conversation_id and record.get("conversation_id") != conversation_id:
        return False
    if not range_hint:
        return True
    text = "\n".join(
        [
            str(record.get("content") or ""),
            str(record.get("metadata") or ""),
        ]
    )
    return range_hint in text


def _select_summary_records(
    records: list[dict[str, Any]],
    *,
    estimator: TokenEstimator,
    input_budget: int = 60000,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for record in reversed(records):
        cost = estimator.estimate_text(_format_summary_record(record))
        if selected and used + cost > input_budget:
            break
        selected.append(record)
        used += cost
    selected.reverse()
    return selected


def _format_summary_records(records: list[dict[str, Any]]) -> str:
    return "\n\n".join(_format_summary_record(record) for record in records)


def _format_summary_record(record: dict[str, Any]) -> str:
    source = str(record.get("_source") or "unknown")
    conversation_id = str(record.get("conversation_id") or "unknown")
    role = str(record.get("role") or "?")
    timestamp = _summary_timestamp(record)
    content = str(record.get("content") or "").strip()
    head = f"[{source}] [{conversation_id}] [{role}]"
    if timestamp:
        head += f" [{timestamp}]"
    return f"{head}\n{content}"


def _summary_timestamp(record: dict[str, Any]) -> str:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        if meta.get("timestamp") is not None:
            return str(meta.get("timestamp"))
        messages = meta.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict) and last.get("timestamp") is not None:
                return str(last.get("timestamp"))
    return ""
