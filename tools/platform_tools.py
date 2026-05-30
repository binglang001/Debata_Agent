"""平台查询/操作类工具。

包括：
    - list_contacts: 列好友/群/群成员
    - get_user_info: 获取陌生人信息
    - get_forward_msg: 提取合并转发
    - set_friend_add_request: 处理加好友请求
    - set_group_add_request: 处理加群请求
    - summarize_chat_history: 拉取群历史并交给 LLM 总结
    - recall_history: 从本地永久归档检索较早上下文
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
    RecallHistoryArgs,
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
    description=(
        "查询 QQ 联系人信息。需要知道可私聊的好友、当前加入的群，或某个群里有哪些成员时使用；"
        "scope=group_members 时必须提供 group_id。不要用它读取聊天记录。"
    ),
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
    return {"ok": True, "info": _public_user_info(_to_dict(info))}


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
        "拉取指定群的近期聊天记录并交给总结模型整理。"
        "当用户要求了解某个群、总结群历史、分析成员印象时使用；必须知道 group_id。"
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
            count=ctx.default_history_fetch_count,
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
        # 复用 summary agent 的 timeout 配置；temperature/max_tokens 用总结合理默认
        result = await ctx.summary_provider.chat_completion(
            messages,
            model=ctx.summary_model,
            tools=None,
            temperature=0.3,
            max_tokens=4096,
            stream=False,
            timeout=ctx.summary_provider.timeout if hasattr(ctx.summary_provider, "timeout") else 60.0,
        )
    except ProviderError as e:
        return {"ok": False, "error": f"总结失败: {e}"}

    return {"ok": True, "summary": result.content}


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

    snippets: list[dict] = []
    for record in records:
        snippets.append(
            {
                "role": record.get("role"),
                "conversation_id": record.get("conversation_id"),
                "content": (record.get("content") or "")[:1000],
                "metadata": record.get("metadata", {}),
            }
        )
    return {
        "ok": True,
        "count": len(snippets),
        "results": snippets,
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
