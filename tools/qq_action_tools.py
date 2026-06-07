"""轻量 QQ 动作工具：读单条消息、戳一戳、消息表情回复。"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import ToolContext, tool
from .schemas import GetMsgArgs, SendPokeArgs, SetMsgEmojiLikeArgs

logger = logging.getLogger(__name__)


@tool(
    name="get_msg",
    description=(
        "读取一条已知 QQ 消息的详情。用于确认被引用消息、补看旧 msg_id、"
        "或在发送前核对某条真实消息内容；必须有真实 message_id。"
    ),
    args_model=GetMsgArgs,
    category="platform",
)
async def get_msg(args: GetMsgArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return _failed("未连接适配器，无法读取消息。")
    try:
        raw = await ctx.adapter.call_api("get_msg", message_id=int(args.message_id))
    except Exception as e:
        logger.warning("get_msg 调用失败 message_id=%s: %s", args.message_id, e)
        return {
            "ok": False,
            "status": "failed",
            "brief": f"读取消息 {args.message_id} 失败：{e}",
            "error": str(e),
            "data": {"message_id": str(args.message_id)},
        }
    message = _normalize_message(raw)
    return {
        "ok": True,
        "status": "inline",
        "brief": f"已读取消息 {args.message_id}。",
        "message": message,
        "content": _message_text(message),
        "data": {
            "message_id": str(args.message_id),
            "conversation_id": _message_conversation_id(message),
        },
    }


@tool(
    name="send_poke",
    description=(
        "向明确 QQ 用户发送戳一戳。适合轻量提醒、玩笑互动或用户明确要求戳某人。"
        "不要把戳一戳当普通回复滥用。"
    ),
    args_model=SendPokeArgs,
    category="platform",
    no_feedback=True,
)
async def send_poke(args: SendPokeArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return _failed("未连接适配器，无法发送戳一戳。")
    group_id = _resolve_group_id(args.group_id, ctx)
    params: dict[str, Any] = {"user_id": int(args.user_id)}
    if group_id is not None:
        params["group_id"] = int(group_id)
    try:
        raw = await ctx.adapter.call_api("send_poke", **params)
    except Exception as e:
        logger.warning("send_poke 调用失败 params=%s: %s", params, e)
        return {
            "ok": False,
            "status": "failed",
            "brief": f"戳一戳 QQ {args.user_id} 失败：{e}",
            "error": str(e),
            "data": _poke_data(args, group_id),
        }
    if ctx.activity_cb is not None:
        ctx.activity_cb()
    target = f"群 {group_id} 内 QQ {args.user_id}" if group_id else f"QQ {args.user_id}"
    return {
        "ok": True,
        "status": "done",
        "brief": f"已向 {target} 发送戳一戳。",
        "data": _poke_data(args, group_id),
        "raw": raw,
    }


@tool(
    name="set_msg_emoji_like",
    description=(
        "给一条明确 QQ 消息添加或取消表情回复。适合用表情表达轻量反应；"
        "必须知道真实 message_id 和 emoji_id。"
    ),
    args_model=SetMsgEmojiLikeArgs,
    category="platform",
    no_feedback=True,
)
async def set_msg_emoji_like(args: SetMsgEmojiLikeArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return _failed("未连接适配器，无法设置消息表情回复。")
    params = {
        "message_id": int(args.message_id),
        "emoji_id": str(args.emoji_id),
        "set": bool(args.set),
    }
    try:
        raw = await ctx.adapter.call_api("set_msg_emoji_like", **params)
    except Exception as e:
        logger.warning("set_msg_emoji_like 调用失败 params=%s: %s", params, e)
        return {
            "ok": False,
            "status": "failed",
            "brief": f"设置消息 {args.message_id} 的表情回复失败：{e}",
            "error": str(e),
            "data": _emoji_like_data(args),
        }
    if ctx.activity_cb is not None:
        ctx.activity_cb()
    action = "添加" if args.set else "取消"
    return {
        "ok": True,
        "status": "done",
        "brief": f"已{action}消息 {args.message_id} 的表情回复 {args.emoji_id}。",
        "data": _emoji_like_data(args),
        "raw": raw,
    }


def _resolve_group_id(group_id: int | None, ctx: ToolContext) -> str | None:
    if group_id is not None:
        return str(group_id)
    if ctx.conversation_id and ctx.conversation_id.startswith("group:"):
        return ctx.conversation_id.split(":", 1)[1]
    target = ctx.extras.get("default_reply_target")
    if isinstance(target, dict) and target.get("target_type") == "group":
        value = target.get("target_id")
        if value is not None:
            return str(value)
    return None


def _normalize_message(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    return {"value": raw}


def _message_text(message: dict[str, Any]) -> str:
    for key in ("raw_message", "message", "content", "text"):
        value = message.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return _segments_to_text(value)
    try:
        return json.dumps(message, ensure_ascii=False, default=str)
    except TypeError:
        return str(message)


def _segments_to_text(segments: list[Any]) -> str:
    out: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            out.append(str(segment))
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        seg_type = str(segment.get("type") or "")
        if seg_type == "text":
            out.append(str(data.get("text") or ""))
        elif seg_type:
            out.append(f"[{seg_type}]")
    return "".join(out).strip()


def _message_conversation_id(message: dict[str, Any]) -> str | None:
    if message.get("group_id") is not None:
        return f"group:{message.get('group_id')}"
    if message.get("user_id") is not None:
        return f"private:{message.get('user_id')}"
    return None


def _poke_data(args: SendPokeArgs, group_id: str | None) -> dict[str, Any]:
    return {
        "user_id": str(args.user_id),
        "group_id": group_id,
        "reason": args.reason,
    }


def _emoji_like_data(args: SetMsgEmojiLikeArgs) -> dict[str, Any]:
    return {
        "message_id": str(args.message_id),
        "emoji_id": str(args.emoji_id),
        "set": bool(args.set),
        "reason": args.reason,
    }


def _failed(brief: str) -> dict:
    return {
        "ok": False,
        "status": "failed",
        "brief": brief,
        "error": brief,
    }
