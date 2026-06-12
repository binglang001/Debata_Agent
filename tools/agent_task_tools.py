"""后台子 Agent 任务工具。"""

from __future__ import annotations

from pathlib import PurePath

from .base import ToolContext, tool
from .schemas import AgentTaskSource, StartAgentTaskArgs

_IMAGE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}

_IMAGE_TASK_HINTS = (
    "看图",
    "看看图",
    "这张图",
    "图里",
    "图中",
    "图上",
    "图的内容",
    "图片内容",
    "图像内容",
    "截图内容",
    "照片内容",
    "表情内容",
    "画面",
    "识别",
    "描述",
    "是什么",
    "什么内容",
    "ocr",
    "describe",
    "recognize",
    "read text",
    "what",
    "content",
    "scene",
    "visual",
    "screenshot content",
)


@tool(
    name="start_agent_task",
    description=(
        "启动一个子 Agent 处理较大的资料整理/提取/转换任务。必须传 prompt。"
        "工具会等待子 Agent 完成，并在本次工具结果中返回内容摘要和结果文件路径。"
        "sources 支持 workspace 文件、工具调用 ID、合并转发 ID、本地历史、消息 ID、资料附带图片引用、"
        "内联文本/JSON、workspace glob 和目录；不支持直接传 URL。"
        "不能用此工具绕过或重试失败的图片理解。"
        "如果工具结果中的 content 被截断，可用 read_file 读取 result_file。"
    ),
    args_model=StartAgentTaskArgs,
    category="platform",
)
async def start_agent_task(args: StartAgentTaskArgs, ctx: ToolContext) -> dict:
    if ctx.agent_task_cb is None:
        return {"ok": False, "error": "当前运行时不支持后台子 Agent 任务"}
    vision_unavailable_reason = ""
    if ctx.vision is None:
        vision_unavailable_reason = "当前没有可用图片理解能力"
    elif ctx.extras.get("vision_unavailable_this_turn"):
        vision_unavailable_reason = str(ctx.extras["vision_unavailable_this_turn"])

    if vision_unavailable_reason and _needs_image_understanding(args):
        return {
            "ok": False,
            "status": "failed",
            "brief": "当前没有可用图片理解能力。",
            "error": f"{vision_unavailable_reason}，不能启动子 Agent 代替看图",
            "next": "不要继续尝试看图；根据聊天场景说明看不了，或直接 no_action。",
        }
    return await ctx.agent_task_cb(args.model_dump())


def _needs_image_understanding(args: StartAgentTaskArgs) -> bool:
    image_sources = [source for source in args.sources if _source_looks_like_image(source)]
    if not image_sources:
        return False
    if any(source.type == "image_ref" for source in image_sources):
        return True

    prompt = args.prompt.lower()
    return any(hint in prompt for hint in _IMAGE_TASK_HINTS)


def _source_looks_like_image(source: AgentTaskSource) -> bool:
    if source.type == "image_ref":
        return True
    if source.type not in {"workspace_path", "tool_result_file"}:
        return False
    value = str(source.value or "").strip()
    if not value:
        return False
    value = value.split("?", 1)[0].split("#", 1)[0].strip("[] ")
    return PurePath(value.replace("\\", "/")).suffix.lower() in _IMAGE_SUFFIXES
