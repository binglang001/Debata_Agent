"""平台查询/操作类工具。

包括：
    - list_contacts: 列好友/群/群成员
    - get_user_info: 获取陌生人信息
    - get_forward_msg: 提取合并转发
    - set_friend_add_request: 处理加好友请求
    - set_group_add_request: 处理加群请求
    - summarize_chat_history: 拉取群历史并交给 LLM 总结
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass

from providers.base import ProviderError

from .base import ToolContext, tool
from .schemas import (
    GetForwardMsgArgs,
    GetUserInfoArgs,
    ListContactsArgs,
    SetFriendRequestArgs,
    SetGroupRequestArgs,
    SummarizeChatArgs,
)

logger = logging.getLogger(__name__)


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
    description="查询好友/群/群成员",
    args_model=ListContactsArgs,
    category="platform",
)
async def list_contacts(args: ListContactsArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}

    try:
        if args.scope == "friends":
            friends = await ctx.adapter.list_friends()
            items = [
                {"nickname": f.nickname, "user_id": f.user_id} for f in friends
            ]
            return {"ok": True, "friends": items, "count": len(items)}

        if args.scope == "groups":
            groups = await ctx.adapter.list_groups()
            items = [
                {"group_name": g.group_name, "group_id": g.group_id} for g in groups
            ]
            return {"ok": True, "groups": items, "count": len(items)}

        if args.scope == "group_members":
            if args.group_id is None:
                return {"ok": False, "error": "group_members 需要 group_id"}
            members = await ctx.adapter.list_group_members(str(args.group_id))
            items = [
                {
                    "nickname": m.nickname,
                    "card": m.display_name,
                    "user_id": m.user_id,
                }
                for m in members
            ]
            return {"ok": True, "members": items, "count": len(items)}

    except Exception as e:
        logger.warning(f"list_contacts 失败: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"未知 scope: {args.scope}"}


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
        return {"ok": False, "error": "未连接适配器"}
    try:
        info = await ctx.adapter.get_user_info(str(args.user_id))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "info": _to_dict(info)}


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

    try:
        messages = await ctx.adapter.get_forward_msg(args.forward_id)
    except NotImplementedError:
        return {"ok": False, "error": "当前适配器不支持合并转发提取"}
    except Exception as e:
        logger.warning(f"get_forward_msg 失败: {e}")
        return {"ok": False, "error": str(e)}

    # adapter 已返回结构化列表（OneBot 协议的 messages 数组），这里仅做扁平化
    formatted: list[str] = []
    for m in messages:
        sender = m.get("sender", {}).get("nickname", "未知") if isinstance(m, dict) else "未知"
        content = m.get("raw_message", "") or m.get("content", "") if isinstance(m, dict) else ""
        if isinstance(content, list):
            parts: list[str] = []
            for seg in content:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        parts.append(seg.get("data", {}).get("text", ""))
                    elif seg.get("type") == "image":
                        parts.append("[图片]")
                    else:
                        parts.append(f"[{seg.get('type', '')}]")
                else:
                    parts.append(str(seg))
            content = "".join(parts)
        formatted.append(f"{sender}: {content}")

    return {"ok": True, "content": "\n".join(formatted)}


# ============================================================
# set_friend_add_request
# ============================================================


@tool(
    name="set_friend_add_request",
    description="处理好友添加请求。必须先经管理员同意才能调用。",
    args_model=SetFriendRequestArgs,
    category="platform",
    no_feedback=True,
)
async def set_friend_add_request(args: SetFriendRequestArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}
    try:
        await ctx.adapter.handle_friend_request(
            flag=args.flag,
            approve=args.approve,
            remark=args.remark or "",
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# ============================================================
# set_group_add_request
# ============================================================


@tool(
    name="set_group_add_request",
    description="处理加群申请或群邀请。必须先经管理员同意才能调用。",
    args_model=SetGroupRequestArgs,
    category="platform",
    no_feedback=True,
)
async def set_group_add_request(args: SetGroupRequestArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}
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
        return {"ok": False, "error": str(e)}
    return {"ok": True}


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
        "获取群聊聊天记录并总结。了解群基本情况、成员、氛围、对各成员的印象。"
    ),
    args_model=SummarizeChatArgs,
    category="platform",
)
async def summarize_chat_history(args: SummarizeChatArgs, ctx: ToolContext) -> dict:
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}
    if ctx.summary_provider is None or not ctx.summary_model:
        return {"ok": False, "error": "未配置总结模型"}

    try:
        history = await ctx.adapter.get_group_history(
            str(args.group_id),
            count=ctx.chat_history_count,
        )
    except NotImplementedError:
        return {"ok": False, "error": "当前适配器不支持群历史拉取"}
    except Exception as e:
        return {"ok": False, "error": f"获取群聊记录失败: {e}"}

    prompt = args.custom_prompt or _DEFAULT_SUMMARIZE_PROMPT
    messages = [
        {
            "role": "user",
            "content": f"{prompt}\n\n聊天记录：\n{history}",
        }
    ]

    try:
        result = await ctx.summary_provider.chat_completion(
            messages,
            model=ctx.summary_model,
            tools=None,
            temperature=0.3,
            max_tokens=4096,
            stream=False,
            timeout=60.0,
        )
    except ProviderError as e:
        return {"ok": False, "error": f"总结失败: {e}"}

    return {"ok": True, "summary": result.content}
