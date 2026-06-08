"""重要记忆类工具：save / update / delete。"""

from __future__ import annotations

import logging

from memory.important import normalize_scope, scope_from_conversation_id

from .base import ToolContext, tool
from .schemas import DeleteMemoryArgs, SaveMemoryArgs, UpdateMemoryArgs

logger = logging.getLogger(__name__)


@tool(
    name="save_important_memory",
    description=(
        "保存一条新的长期重要记忆，只用于稳定信息：人物身份、偏好、约定、长期目标、需要以后一直参考的事实。"
        "必须写成客观、完整、有明确主语的一句话；不要保存“你生日七月八号”这类无头片段。"
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

    scope = normalize_scope(args.scope) if args.scope else (
        scope_from_conversation_id(ctx.conversation_id) or "global"
    )
    try:
        result = await ctx.important.save(
            args.memory_text,
            scope=scope,
            pinned=args.pinned,
        )
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    saved = bool(result["saved"])
    status = "done" if saved else "exact_duplicate"
    return {
        "ok": True,
        "status": status,
        "brief": "已保存重要记忆。" if saved else "存在完全相同的重要记忆，未重复保存。",
        "saved": saved,
        "duplicate": result.get("duplicate", False),
        "duplicate_type": result.get("duplicate_type"),
        "existing_id": result.get("existing_id"),
        "scope": scope,
        "pinned": args.pinned,
        "data": {
            "saved": saved,
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


@tool(
    name="delete_important_memory",
    description=(
        "删除一条重要记忆。当记忆过时、重复、或不再需要时使用。"
        "传入要删除的记忆关键词进行模糊匹配。"
    ),
    args_model=DeleteMemoryArgs,
    category="memory",
    no_feedback=True,
)
async def delete_important_memory(args: DeleteMemoryArgs, ctx: ToolContext) -> dict:
    if ctx.important is None:
        return {"ok": False, "error": "重要记忆管理器未注入"}

    try:
        deleted = await ctx.important.delete_by_keyword(args.keyword)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "status": "done",
        "brief": f"已删除 {deleted} 条匹配的重要记忆。",
        "deleted": deleted,
        "data": {
            "keyword": args.keyword,
            "deleted": deleted,
        },
    }
