"""工具检索入口：为 stub 工具按需返回参数摘要或完整 schema。"""

from __future__ import annotations

from .base import ToolContext, tool
from .schemas import ToolSearchArgs


@tool(
    name="tool_search",
    description=(
        "查询工具的说明、真实参数摘要/完整 JSON schema、示例和风险约束。"
        "当工具 schema 只有简短 stub，或你不确定参数/风险时先调用。"
    ),
    args_model=ToolSearchArgs,
    category="control",
)
async def tool_search(args: ToolSearchArgs, ctx: ToolContext) -> dict:
    registry = ctx.extras.get("tool_registry")
    if registry is None or not hasattr(registry, "get_spec"):
        return {
            "ok": False,
            "status": "unavailable",
            "tool_name": args.tool_name,
            "error": "当前运行时未注入工具注册表，无法查询工具详情",
        }
    spec = registry.get_spec(args.tool_name)
    if spec is None:
        candidates = [
            name
            for name in getattr(registry, "names", lambda: [])()
            if args.tool_name.lower() in name.lower()
        ][:10]
        return {
            "ok": False,
            "status": "not_found",
            "tool_name": args.tool_name,
            "candidates": candidates,
            "next": "请检查工具名；不要猜测不存在的工具。",
        }
    approved = ctx.extras.setdefault("tool_search_approved_tools", set())
    if isinstance(approved, set):
        approved.add(spec.name)
    result = spec.tool_search_result(detail=args.detail)
    result["intent"] = args.intent
    return result
