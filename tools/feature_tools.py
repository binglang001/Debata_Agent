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

import logging

from .base import ToolContext, tool
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
        description = await ctx.vision.describe(
            args.image_url, args.prompt or ""
        )
    except Exception as e:
        logger.warning(f"describe_image 失败: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True, "description": description}


@tool(
    name="web_search",
    description=(
        "搜索互联网获取实时信息。当需要查找当前新闻、事实核查、"
        "最新资讯时使用。返回相关结果的摘要。"
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
        "用本地 TTS 合成语音并发送到 QQ。仅在你想表达情绪比文字更直接、"
        "或对方明确说想听你声音时使用。合成耗时较长（数秒），慎用。"
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

    try:
        audio_path = await ctx.tts.synthesize(
            args.text,
            prompt=args.prompt or "",
        )
    except Exception as e:
        logger.warning(f"TTS 合成失败: {e}")
        return {"ok": False, "error": f"合成失败：{e}"}

    try:
        # adapter.send_voice 由 Phase 3 实装，签名约定:
        #   async def send_voice(target_type: str, target_id: int, audio_path: Path) -> str | None
        send_voice = getattr(ctx.adapter, "send_voice", None)
        if send_voice is None:
            return {"ok": False, "error": "当前 adapter 不支持发送语音"}
        msg_id = await send_voice(args.target_type, args.target_id, audio_path)
    except Exception as e:
        logger.warning(f"send_voice 失败: {e}")
        return {"ok": False, "error": f"发送失败：{e}"}

    return {"ok": True, "message_id": msg_id, "audio_path": str(audio_path)}
