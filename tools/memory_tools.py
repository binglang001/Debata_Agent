"""重要记忆类工具：save / update / delete。"""

from __future__ import annotations

import logging

from memory.important import normalize_scope

from .base import ToolContext, tool
from .schemas import DeleteMemoryArgs, SaveMemoryArgs, UpdateMemoryArgs

logger = logging.getLogger(__name__)


@tool(
    name="save_important_memory",
    description=(
        "保存一条新的长期重要记忆，只用于稳定信息：人物身份、偏好、约定、长期目标、需要以后一直参考的事实。"
        "必须写成客观、完整、有明确主语的一句话；不要保存“你生日七月八号”这类无头片段。"
        "必须显式传 scope，并按事实适用范围选择 global、user:QQ号 或 group:群号；不会按当前会话自动推断。"
        "如果是在修正或补充已有记忆，优先用 update_important_memory 覆写旧记忆。"
        "程序只拦截完全相同文本，不做语义去重。"
    ),
    args_model=SaveMemoryArgs,
    category="memory",
    no_feedback=True,
)
async def save_important_memory(args: SaveMemoryArgs, ctx: ToolContext) -> dict:
    if ctx.important is None:
        return {"ok": False, "error": "重要记忆管理器未注入"}

    scope_error = _validate_explicit_scope(args.scope)
    if scope_error:
        return scope_error
    scope = normalize_scope(args.scope)
    try:
        result = await ctx.important.save(
            args.memory_text,
            scope=scope,
            pinned=args.pinned,
        )
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    saved = bool(result["saved"])
    memory_id = result.get("id")
    status = "done" if saved else "exact_duplicate"
    return {
        "ok": True,
        "status": status,
        "brief": "已保存重要记忆。" if saved else "存在完全相同的重要记忆，未重复保存。",
        "saved": saved,
        "memory_id": memory_id,
        "duplicate": result.get("duplicate", False),
        "duplicate_type": result.get("duplicate_type"),
        "existing_id": result.get("existing_id"),
        "scope": scope,
        "pinned": args.pinned,
        "data": {
            "saved": saved,
            "memory_id": memory_id,
            "duplicate": result.get("duplicate", False),
            "duplicate_type": result.get("duplicate_type"),
            "existing_id": result.get("existing_id"),
            "scope": scope,
            "pinned": args.pinned,
        },
    }


@tool(
    name="update_important_memory",
    description=(
        "覆写一条已有长期重要记忆。用于修正、补充或合并已有事实，避免把同一主体的事实重复保存成多条。"
        "memory_text 必须是更新后的完整客观表述，有明确主语；不要只写新增片段或无头聊天句。"
        "修改内容时重新判断适用范围；仍适用原范围可不传 scope，语义变成跨场景、单用户或单群时要同步传新 scope。"
        "程序只拦截完全相同文本，不做语义判断。"
    ),
    args_model=UpdateMemoryArgs,
    category="memory",
    no_feedback=True,
)
async def update_important_memory(args: UpdateMemoryArgs, ctx: ToolContext) -> dict:
    if ctx.important is None:
        return {"ok": False, "error": "重要记忆管理器未注入"}

    try:
        result = await ctx.important.update(
            args.memory_id,
            args.memory_text,
            scope=args.scope,
            pinned=args.pinned,
        )
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    if result.get("missing"):
        return {
            "ok": False,
            "status": "not_found",
            "brief": "没有找到要更新的重要记忆。",
            "memory_id": args.memory_id,
        }
    if result.get("empty"):
        return {
            "ok": False,
            "status": "empty",
            "brief": "更新后的记忆不能为空。",
            "memory_id": args.memory_id,
        }
    if result.get("duplicate"):
        return {
            "ok": True,
            "status": "exact_duplicate",
            "brief": "已有完全相同的重要记忆，未更新。",
            "updated": False,
            "duplicate": True,
            "duplicate_type": result.get("duplicate_type"),
            "existing_id": result.get("existing_id"),
            "memory_id": args.memory_id,
            "data": {
                "memory_id": args.memory_id,
                "existing_id": result.get("existing_id"),
                "reason": args.reason,
            },
        }

    return {
        "ok": True,
        "status": "done",
        "brief": "已更新重要记忆。",
        "updated": True,
        "memory_id": result.get("id") or args.memory_id,
        "scope": args.scope,
        "pinned": args.pinned,
        "data": {
            "memory_id": result.get("id") or args.memory_id,
            "old_content": result.get("old_content"),
            "content": result.get("content"),
            "scope": args.scope,
            "pinned": args.pinned,
            "reason": args.reason,
        },
    }


def _validate_explicit_scope(scope: str | None) -> dict | None:
    raw = (scope or "").strip()
    if not raw:
        return _scope_retry_error(
            "missing_scope",
            "save_important_memory 必须显式填写 scope。",
        )
    if raw == "global":
        return None
    if raw.startswith("private:"):
        return _scope_retry_error(
            "invalid_scope",
            "scope 不能写 private:QQ；私聊对象本人相关的语义范围应写 user:QQ。",
            raw_scope=raw,
        )
    if raw.startswith(("user:", "group:")):
        prefix, ident = raw.split(":", 1)
        if ident.strip() and ":" not in ident and not any(ch.isspace() for ch in ident):
            return None
        return _scope_retry_error(
            "invalid_scope",
            f"{prefix}: 后必须是非空且不含空白或冒号的 ID。",
            raw_scope=raw,
        )
    return _scope_retry_error(
        "invalid_scope",
        "scope 只能是 global、user:QQ号 或 group:群号。",
        raw_scope=raw,
    )


def _scope_retry_error(
    status: str,
    brief: str,
    *,
    raw_scope: str | None = None,
) -> dict:
    data = {
        "allowed_scopes": ["global", "user:QQ号", "group:群号"],
        "rules": {
            "global": "跨场景都应参考的事实、长期目标、项目、稳定关系或全局偏好。",
            "user:QQ号": "只适用于某个具体 QQ 用户本人的身份、偏好、私聊约定或关系事实。",
            "group:群号": "只适用于某个群的群规、群内约定、群内梗或群内关系。",
        },
        "examples": [
            "提到某用户不等于 user scope；先判断这条事实是否只适用于该用户本人。",
            "冰狼正在做短中期项目这类跨场景事实应选择 global。",
        ],
    }
    if raw_scope is not None:
        data["raw_scope"] = raw_scope
    return {
        "ok": False,
        "status": status,
        "brief": brief,
        "error": brief,
        "retryable": True,
        "next": (
            "请重新调用 save_important_memory，并按语义显式选择 scope："
            "global=跨场景事实；user:QQ号=只适用于该用户本人；group:群号=只适用于该群。"
            "提到某用户不等于 user scope。"
        ),
        "data": data,
    }


@tool(
    name="delete_important_memory",
    description=(
        "删除一条重要记忆。当记忆过时、重复、或不再需要时使用。"
        "推荐使用 memory_id 精确删除，memory_id 必须来自重要记忆上下文中展示的 ID、"
        "save_important_memory 返回的 memory_id，或 existing_id。"
        "keyword 仅为旧版兼容的模糊删除参数；能看到 ID 时不要用 keyword，避免误删多条记忆。"
    ),
    args_model=DeleteMemoryArgs,
    category="memory",
    no_feedback=True,
)
async def delete_important_memory(args: DeleteMemoryArgs, ctx: ToolContext) -> dict:
    if ctx.important is None:
        return {"ok": False, "error": "重要记忆管理器未注入"}

    if args.memory_id:
        try:
            deleted = await ctx.important.delete_by_id(args.memory_id)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        if not deleted:
            return {
                "ok": False,
                "status": "not_found",
                "brief": "没有找到要删除的重要记忆。",
                "memory_id": args.memory_id,
                "deleted": 0,
                "data": {
                    "memory_id": args.memory_id,
                    "deleted": 0,
                },
            }
        return {
            "ok": True,
            "status": "done",
            "brief": "已删除 1 条重要记忆。",
            "memory_id": args.memory_id,
            "deleted": 1,
            "data": {
                "memory_id": args.memory_id,
                "deleted": 1,
            },
        }

    keyword = args.keyword or ""
    try:
        deleted_count = await ctx.important.delete_by_keyword(keyword)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "status": "legacy_keyword",
        "brief": f"已按旧版关键词兼容模式删除 {deleted_count} 条匹配的重要记忆。",
        "deleted": deleted_count,
        "data": {
            "keyword": keyword,
            "deleted": deleted_count,
            "legacy_keyword": True,
        },
    }
