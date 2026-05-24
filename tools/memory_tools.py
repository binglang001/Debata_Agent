"""重要记忆类工具：save / delete。

仅在 features.long_term_memory.mode == "file" 时注册到 ToolRegistry。
RAG 模式下由后台抽取代替，避免给 AI 暴露混乱的两套机制。
"""

from __future__ import annotations

import logging

from .base import ToolContext, tool
from .schemas import DeleteMemoryArgs, SaveMemoryArgs

logger = logging.getLogger(__name__)


@tool(
    name="save_important_memory",
    description="永久保存重要记忆（人物、约定、秘密等）",
    args_model=SaveMemoryArgs,
    category="memory",
    feature="long_term_memory_file",
    no_feedback=True,
)
async def save_important_memory(args: SaveMemoryArgs, ctx: ToolContext) -> dict:
    if ctx.important is None:
        return {"ok": False, "error": "重要记忆管理器未注入"}

    try:
        result = await ctx.important.save(args.memory_text)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "saved": result["saved"],
        "duplicate": result.get("duplicate", False),
    }


@tool(
    name="delete_important_memory",
    description=(
        "删除一条重要记忆。当记忆过时、重复、或不再需要时使用。"
        "传入要删除的记忆关键词进行模糊匹配。"
    ),
    args_model=DeleteMemoryArgs,
    category="memory",
    feature="long_term_memory_file",
    no_feedback=True,
)
async def delete_important_memory(args: DeleteMemoryArgs, ctx: ToolContext) -> dict:
    if ctx.important is None:
        return {"ok": False, "error": "重要记忆管理器未注入"}

    try:
        deleted = await ctx.important.delete_by_keyword(args.keyword)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "deleted": deleted}
