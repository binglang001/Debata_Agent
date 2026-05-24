"""
Diana-agent 插件
- 被动响应私聊和群聊消息（多轮工具调用循环）
- 定时主动问候（间隔见 config.yaml）
- DeepSeek AI 驱动，KV 缓存感知的上下文分区
- 完整对话历史记录（含工具调用、主动思考）
- 图片理解（豆包视觉模型）+ 语音消息自动转写（NapCat STT）
"""

import os

from dotenv import load_dotenv
from nonebot import get_driver
from nonebot.log import logger

# 加载项目根目录的 .env 文件（override=True 确保 .env 覆盖系统环境变量）
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

from . import handler  # noqa: F401
from .memory import init_memory

driver = get_driver()


@driver.on_startup
async def _init():
    # 启动配置校验
    required = (
        "DEEPSEEK_API_KEY",
        "VOLCENGINE_API_KEY",
        "QWEATHER_KEY",
        "QWEATHER_HOST",
        "ONEBOT_ACCESS_TOKEN",
    )
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error(f"缺少必需配置: {', '.join(missing)}，请在 .env 中设置")

    await init_memory()
