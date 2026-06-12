"""QQ 群管理工具。

这组工具只走适配器通用 API 通道，默认作为 stub 暴露。执行前会查询机器人自身
在目标群的 role，权限不足时返回 insufficient_permission，不让模型猜测权限。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import ToolContext, tool
from .schemas import (
    GetGroupSelfRoleArgs,
    SetGroupBanArgs,
    SetGroupKickArgs,
    SetGroupLeaveArgs,
    SetGroupWholeBanArgs,
)

logger = logging.getLogger(__name__)

_ADMIN_ROLES = {"owner", "admin"}
_GROUP_ROLE_CACHE_TTL_SECONDS = 300.0


@tool(
    name="get_group_self_role",
    description=(
        "查询机器人自己在某个 QQ 群里的身份权限，返回 owner/admin/member/unknown。"
        "群管理操作前可用它确认机器人是否有权限。"
    ),
    args_model=GetGroupSelfRoleArgs,
    category="platform",
)
async def get_group_self_role(args: GetGroupSelfRoleArgs, ctx: ToolContext) -> dict:
    group_id = _resolve_group_id(args.group_id, ctx)
    if group_id is None:
        return _failed("缺少群号，无法查询机器人群权限。", status="need_group_id")
    info = await _get_group_self_role(ctx, group_id)
    return {
        "ok": info["role"] != "unknown",
        "status": "inline" if info["role"] != "unknown" else "failed",
        "brief": f"机器人在群 {group_id} 的身份是 {info['role']}。",
        "group_id": group_id,
        "role": info["role"],
        "self_id": info.get("self_id"),
        "role_checked_at": info.get("role_checked_at"),
        "data": info,
        **({"error": info["error"]} if info.get("error") else {}),
    }


@tool(
    name="set_group_kick",
    description=(
        "高风险 QQ 群管理工具：将明确 QQ 用户踢出指定群。"
        "只能在用户明确要求、群号和目标 QQ 都明确时使用。"
    ),
    args_model=SetGroupKickArgs,
    category="platform",
)
async def set_group_kick(args: SetGroupKickArgs, ctx: ToolContext) -> dict:
    group_id = str(args.group_id)
    permission = await _require_group_admin(ctx, group_id)
    if permission is not None:
        return permission
    return await _call_group_admin_api(
        ctx,
        "set_group_kick",
        group_id=group_id,
        params={
            "group_id": int(group_id),
            "user_id": int(args.user_id),
            "reject_add_request": bool(args.reject_add_request),
        },
        brief=f"已请求将 QQ {args.user_id} 移出群 {group_id}。",
        data={
            "operation": "set_group_kick",
            "group_id": group_id,
            "user_id": str(args.user_id),
            "reject_add_request": bool(args.reject_add_request),
            "reason": args.reason,
        },
    )


@tool(
    name="set_group_ban",
    description=(
        "高风险 QQ 群管理工具：禁言指定群成员一段明确时长。"
        "只能在用户明确要求、目标 QQ 和时长都明确时使用。"
    ),
    args_model=SetGroupBanArgs,
    category="platform",
)
async def set_group_ban(args: SetGroupBanArgs, ctx: ToolContext) -> dict:
    group_id = str(args.group_id)
    permission = await _require_group_admin(ctx, group_id)
    if permission is not None:
        return permission
    return await _call_group_admin_api(
        ctx,
        "set_group_ban",
        group_id=group_id,
        params={
            "group_id": int(group_id),
            "user_id": int(args.user_id),
            "duration": int(args.duration_seconds),
        },
        brief=f"已请求禁言 QQ {args.user_id} {args.duration_seconds} 秒。",
        data={
            "operation": "set_group_ban",
            "group_id": group_id,
            "user_id": str(args.user_id),
            "duration_seconds": int(args.duration_seconds),
            "reason": args.reason,
        },
    )


@tool(
    name="set_group_whole_ban",
    description=(
        "高风险 QQ 群管理工具：开启或关闭指定群全员禁言。"
        "只能在用户明确要求当前群全员禁言/解除全员禁言时使用。"
    ),
    args_model=SetGroupWholeBanArgs,
    category="platform",
)
async def set_group_whole_ban(args: SetGroupWholeBanArgs, ctx: ToolContext) -> dict:
    group_id = str(args.group_id)
    permission = await _require_group_admin(ctx, group_id)
    if permission is not None:
        return permission
    return await _call_group_admin_api(
        ctx,
        "set_group_whole_ban",
        group_id=group_id,
        params={"group_id": int(group_id), "enable": bool(args.enable)},
        brief=f"已请求{'开启' if args.enable else '关闭'}群 {group_id} 全员禁言。",
        data={
            "operation": "set_group_whole_ban",
            "group_id": group_id,
            "enable": bool(args.enable),
            "reason": args.reason,
        },
    )


@tool(
    name="set_group_leave",
    description=(
        "高风险 QQ 群管理工具：让机器人退出当前群。"
        "必须是明确要求机器人离开当前群；不能用来跨群退群。"
    ),
    args_model=SetGroupLeaveArgs,
    category="platform",
)
async def set_group_leave(args: SetGroupLeaveArgs, ctx: ToolContext) -> dict:
    group_id = _resolve_group_id(args.group_id, ctx)
    if group_id is None:
        return _failed("退群需要明确当前群号。", status="need_group_id")
    if ctx.conversation_id and ctx.conversation_id.startswith("group:"):
        current_group = ctx.conversation_id.split(":", 1)[1]
        if str(group_id) != current_group:
            return _failed(
                "退群只能作用于当前群聊，不能跨群退群。",
                status="target_mismatch",
            )
    permission = await _require_group_member(ctx, str(group_id))
    if permission is not None:
        return permission
    return await _call_group_admin_api(
        ctx,
        "set_group_leave",
        group_id=str(group_id),
        params={"group_id": int(group_id), "is_dismiss": False},
        brief=f"已请求退出群 {group_id}。",
        data={
            "operation": "set_group_leave",
            "group_id": str(group_id),
            "reason": args.reason,
        },
    )


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


async def _require_group_admin(ctx: ToolContext, group_id: str) -> dict | None:
    role_info = await _get_group_self_role(ctx, group_id)
    role = str(role_info.get("role") or "unknown")
    if role in _ADMIN_ROLES:
        return None
    return {
        "ok": False,
        "status": "insufficient_permission",
        "brief": f"机器人在群 {group_id} 的身份是 {role}，无群管理权限。",
        "group_id": group_id,
        "role": role,
        "data": role_info,
        "next": "不要继续尝试该群管理操作；可以说明机器人权限不足。",
    }


async def _require_group_member(ctx: ToolContext, group_id: str) -> dict | None:
    role_info = await _get_group_self_role(ctx, group_id)
    role = str(role_info.get("role") or "unknown")
    if role != "unknown":
        return None
    return {
        "ok": False,
        "status": "insufficient_permission",
        "brief": f"无法确认机器人在群 {group_id} 的成员身份，不能退群。",
        "group_id": group_id,
        "role": role,
        "data": role_info,
    }


async def _get_group_self_role(ctx: ToolContext, group_id: str) -> dict[str, Any]:
    now = float(ctx.extras.get("now_monotonic") or time.monotonic())
    cache = ctx.extras.setdefault("group_self_role_cache", {})
    cache_key = str(group_id)
    cached = cache.get(cache_key) if isinstance(cache, dict) else None
    if isinstance(cached, dict) and now > 0:
        checked = float(cached.get("_checked_monotonic") or 0.0)
        if checked and now - checked <= _GROUP_ROLE_CACHE_TTL_SECONDS:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    self_id = _resolve_self_id(ctx)
    if not self_id:
        return {
            "group_id": str(group_id),
            "self_id": None,
            "role": "unknown",
            "error": "缺少机器人 self_id，无法查询自身群身份",
        }
    if ctx.adapter is None:
        return {
            "group_id": str(group_id),
            "self_id": self_id,
            "role": "unknown",
            "error": "未连接适配器",
        }

    try:
        data = await ctx.adapter.call_api(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(self_id),
            no_cache=True,
        )
    except Exception as e:
        logger.warning("查询机器人群身份失败 group_id=%s self_id=%s: %s", group_id, self_id, e)
        return {
            "group_id": str(group_id),
            "self_id": self_id,
            "role": "unknown",
            "error": str(e),
        }

    role = _normalize_group_role(data.get("role"))
    info = {
        "group_id": str(group_id),
        "self_id": self_id,
        "role": role,
        "role_checked_at": data.get("join_time") or data.get("update_time") or None,
    }
    if isinstance(cache, dict):
        cache[cache_key] = dict(info, _checked_monotonic=now)
    return info


def _resolve_self_id(ctx: ToolContext) -> str | None:
    for key in ("self_id", "bot_qq", "bot_id"):
        value = ctx.extras.get(key)
        if value:
            return str(value)
    if ctx.conversation_id:
        by_conversation = ctx.extras.get("self_id_by_conversation")
        if isinstance(by_conversation, dict):
            value = by_conversation.get(ctx.conversation_id)
            if value:
                return str(value)
    return None


def _normalize_group_role(value: Any) -> str:
    role = str(value or "").lower()
    if role in {"owner", "admin", "member"}:
        return role
    return "unknown"


async def _call_group_admin_api(
    ctx: ToolContext,
    action: str,
    *,
    group_id: str,
    params: dict[str, Any],
    brief: str,
    data: dict[str, Any],
) -> dict:
    if ctx.adapter is None:
        return _failed("未连接适配器，无法执行群管理操作。")
    try:
        raw = await ctx.adapter.call_api(action, **params)
    except Exception as e:
        logger.warning("群管理 API 调用失败 action=%s group_id=%s: %s", action, group_id, e)
        return {
            "ok": False,
            "status": "failed",
            "brief": f"群管理操作失败：{e}",
            "error": str(e),
            "data": data,
        }
    if ctx.activity_cb is not None:
        ctx.activity_cb()
    return {
        "ok": True,
        "status": "done",
        "brief": brief,
        "data": data,
        "raw": raw,
    }


def _failed(brief: str, *, status: str = "failed") -> dict:
    return {
        "ok": False,
        "status": status,
        "brief": brief,
        "error": brief,
    }
