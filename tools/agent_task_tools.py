"""后台子 Agent 任务工具。"""

from __future__ import annotations

from .base import ToolContext, tool
from .schemas import StartAgentTaskArgs


@tool(
    name="start_agent_task",
    description=(
        "启动一个后台子 Agent 处理较大的资料整理/提取/转换任务。必须传 prompt。"
        "工具会先返回 task_id；子 Agent 完成后，系统会把结果文件路径作为一次新请求回传。"
        "sources 支持 workspace 文件、工具调用 ID、合并转发 ID、本地历史、消息 ID、图片引用、"
        "内联文本/JSON、workspace glob 和目录；不支持直接传 URL。"
    ),
    args_model=StartAgentTaskArgs,
    category="platform",
    no_feedback=True,
)
async def start_agent_task(args: StartAgentTaskArgs, ctx: ToolContext) -> dict:
    if ctx.agent_task_cb is None:
        return {"ok": False, "error": "当前运行时不支持后台子 Agent 任务"}
    return await ctx.agent_task_cb(args.model_dump())
