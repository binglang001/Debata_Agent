"""可选功能工具：图像理解 / 联网搜索 / 天气查询。

设计：
    - 工具始终注册（让 LLM schema 稳定，避免在 file/rag/启用切换时大幅重排）
    - 但运行时检查 ctx 是否注入对应 service，未注入则返回 {"ok": false, "error": "未启用..."}
    - 这样 build_default_registry 决定的"启用 vs 不启用"通过 ctx 字段控制，
      ToolRegistry 本身的 schema 不变化（缓存友好）

⚠️ 严格来说"schema 稳定"和"按 feature 启用工具列表"有矛盾。
本项目采用折中：默认按 feature.enabled 决定注册（影响 schema），
但允许调用方自行决定是否启用。详见 build_default_registry。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Any

from adapters.types import Target
from utils.token_budget import TokenEstimator

from .base import ToolContext, tool
from .result_shrink import add_condensed_marker, tool_budget
from .schemas import (
    DescribeImageArgs,
    GetWeatherArgs,
    SendVoiceMessageArgs,
    WebSearchArgs,
)

logger = logging.getLogger(__name__)


@tool(
    name="describe_image",
    description=(
        "理解图片内容。传入图片 URL 和可选的提示词，返回图片的文字描述。"
        "当用户发送图片时，先调用此工具获取图片内容再回复。"
        "可直接传消息里的 workspace 相对路径（如 incoming/a.jpg），工具会自动转成 base64。"
        "可根据场景自定义 prompt，如识别文字、分析表情、判断场景等。"
    ),
    args_model=DescribeImageArgs,
    category="feature",
    feature="vision",
)
async def describe_image(args: DescribeImageArgs, ctx: ToolContext) -> dict:
    if ctx.vision is None:
        return {"ok": False, "error": "未启用图像理解功能"}

    try:
        image_url = _normalize_image_input(args.image_url, ctx)
        question = (args.question or args.prompt or "").strip()
        raw = await ctx.vision.describe(image_url, question)
    except Exception as e:
        logger.warning(f"describe_image 失败: {e}")
        return {"ok": False, "error": str(e)}

    parsed = _vision_result(raw)
    description = parsed["description"]
    summary = parsed["summary"]
    result: dict[str, Any] = {"ok": True, "summary": summary}
    threshold = tool_budget("describe_image", ctx).artifact_threshold
    if question:
        threshold = max(threshold, tool_budget("describe_image", ctx).inline * 2)

    if TokenEstimator().estimate_text(description) > threshold:
        saved = _save_image_description(args.image_url, description, ctx)
        if saved:
            result["full_saved"] = saved
            add_condensed_marker(
                result,
                reason="图片描述过长，已保存完整描述",
                full=f"用 read_file 读取 {saved} 可查看完整描述；也可带 question 重调 describe_image。",
            )
        else:
            result["description"] = description[:2000]
            add_condensed_marker(
                result,
                reason="图片描述过长且 workspace 不可写，已截断",
                full="配置 workspace 后可保存完整描述；也可带 question 重调 describe_image。",
            )
    else:
        result["description"] = description
    return result


def _normalize_image_input(image_url: str, ctx: ToolContext) -> str:
    """把 workspace 图片路径转换成视觉模型可接受的 data URL。"""
    value = (image_url or "").strip()
    if "workspace=" in value:
        value = value.split("workspace=", 1)[1].split("]", 1)[0].strip()
    if value.startswith(("http://", "https://", "data:")):
        return value
    if value.startswith("workspace="):
        value = value.split("=", 1)[1].strip()

    from .workspace import WorkspaceError, resolve_in_workspace

    try:
        path = resolve_in_workspace(value, ctx.workspace_dir)
    except WorkspaceError:
        return image_url
    if not path.is_file():
        return image_url

    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _vision_result(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        description = str(raw.get("description") or raw.get("content") or "").strip()
        summary = str(raw.get("summary") or "").strip()
    else:
        description = str(raw or "").strip()
        summary = ""
    if not description:
        description = "（模型未返回内容）"
    if not summary:
        first_line = next((line.strip() for line in description.splitlines() if line.strip()), description)
        summary = first_line[:80]
    return {"summary": summary, "description": description}


def _save_image_description(image_ref: str, description: str, ctx: ToolContext) -> str | None:
    if ctx.workspace_dir is None:
        return None
    workspace = ctx.workspace_dir.resolve(strict=False)
    try:
        if "workspace=" in image_ref:
            image_ref = image_ref.split("workspace=", 1)[1].split("]", 1)[0].strip()
        if not image_ref.startswith(("http://", "https://", "data:")):
            rel = image_ref.strip().lstrip("/\\")
            target = (workspace / rel).with_suffix(".desc.md")
            target = target.resolve(strict=False)
            if workspace not in target.parents and target != workspace:
                target = workspace / ".run" / _hashed_desc_name(image_ref)
        else:
            target = workspace / ".run" / _hashed_desc_name(image_ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(description, encoding="utf-8")
        return str(target.relative_to(workspace)).replace("\\", "/")
    except OSError as e:
        logger.warning(f"保存图片完整描述失败: {e}")
    except ValueError:
        return None
    return None


def _hashed_desc_name(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"image_{digest}.desc.md"


@tool(
    name="web_search",
    description=(
        "搜索互联网获取实时信息。当需要查找当前新闻、事实核查、"
        "最新资讯、网页资料或不确定事实时使用。query 要具体，返回相关结果摘要；"
        "如果用户只是闲聊或问题不依赖实时信息，不要调用。"
    ),
    args_model=WebSearchArgs,
    category="feature",
    feature="web_search",
)
async def web_search(args: WebSearchArgs, ctx: ToolContext) -> dict:
    if ctx.web_search is None:
        return {"ok": False, "error": "未启用联网搜索功能"}

    try:
        result = await ctx.web_search.search(args.query)
    except Exception as e:
        logger.warning(f"web_search 失败: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "query": args.query, "result": result}


@tool(
    name="get_weather",
    description=(
        "查询指定城市的天气。支持实时天气和多日预报。"
        "参数 city 为城市名称，days 为预报天数（1-7，默认 1）。"
    ),
    args_model=GetWeatherArgs,
    category="feature",
    feature="weather",
)
async def get_weather(args: GetWeatherArgs, ctx: ToolContext) -> dict:
    if ctx.weather is None:
        return {"ok": False, "error": "未启用天气查询功能"}

    try:
        result = await ctx.weather.query(args.city, args.days)
    except Exception as e:
        logger.warning(f"get_weather 失败: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "result": result}


@tool(
    name="send_voice_message",
    description=(
        "用本地 TTS 合成语音并立即发送到 QQ。仅在你想表达情绪比文字更直接、"
        "或对方明确说想听你声音时使用。合成耗时较长（数秒），慎用。"
        "调用时必须填写 prompt，用一句话描述语气、音色和节奏；"
        "语音发出后如果还需要文字说明，再另行调用 send_*。"
    ),
    args_model=SendVoiceMessageArgs,
    category="feature",
    feature="tts",
)
async def send_voice_message(args: SendVoiceMessageArgs, ctx: ToolContext) -> dict:
    if ctx.tts is None:
        return {"ok": False, "error": "未启用 TTS 语音合成"}
    if ctx.adapter is None:
        return {"ok": False, "error": "adapter 未就绪"}
    send_voice = getattr(ctx.adapter, "send_voice", None)
    if send_voice is None:
        return {"ok": False, "error": "当前适配器不支持发送语音"}

    try:
        prompt = _voice_prompt_or_default(args.text, args.prompt)
        audio_path = await ctx.tts.synthesize(
            args.text,
            prompt=prompt,
        )
    except Exception as e:
        logger.warning(f"TTS 合成失败: {e}")
        return {"ok": False, "error": f"合成失败：{e}"}

    target = Target(
        adapter=ctx.adapter.name,
        scope=args.target_type,
        target_id=str(args.target_id),
    )

    if ctx.send_actions_cb is not None:
        return await ctx.send_actions_cb(
            [
                {
                    "kind": "voice",
                    "order": 1,
                    "target_scope": args.target_type,
                    "target_id": str(args.target_id),
                    "content": "",
                    "label": f"[语音] {args.text}",
                    "delay": 0.0,
                    "audio_path": str(audio_path),
                }
            ],
            "send_voice_message",
        )

    try:
        msg_id = await send_voice(target, Path(audio_path))
        if ctx.activity_cb is not None:
            ctx.activity_cb()
    except Exception as e:
        logger.warning(f"语音发送失败: {e}")
        return {"ok": False, "error": f"发送失败：{e}", "audio_path": str(audio_path)}

    return {
        "ok": True,
        "sent": {
            "target_type": args.target_type,
            "target_id": str(args.target_id),
            "msg_id": str(msg_id) if msg_id is not None else None,
        },
        "audio_path": str(audio_path),
    }


def _voice_prompt_or_default(text: str, prompt: str | None) -> str:
    prompt = (prompt or "").strip()
    if prompt:
        return prompt
    return "年轻女性，自然口语，语速中等，尾音自然，避免电音和机械感"
