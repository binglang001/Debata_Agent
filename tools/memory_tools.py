"""重要记忆类工具：save / delete。

file 与 RAG 模式都会注册。RAG 模式下 save/delete 会同步维护向量索引。
"""

from __future__ import annotations

import logging

from .base import ToolContext, tool
from .schemas import DeleteMemoryArgs, SaveMemoryArgs

logger = logging.getLogger(__name__)


@tool(
    name="save_important_memory",
    description=(
        "保存一条长期重要记忆，只用于稳定信息：人物身份、偏好、约定、长期目标、需要以后一直参考的事实。"
        "不要保存日常寒暄、临时请求、工具执行结果或已经过期的信息。"
    ),
    args_model=SaveMemoryArgs,
    category="memory",
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
