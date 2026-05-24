"""消息构造辅助 —— 从旧 handler.py 迁移并去 NoneBot 化。

职责：
    - 拼装文本/图片混合消息（content + image 二选一 → 发送用 CQ 字符串）
    - 表情包文件读取（base64 内联）
    - 打字延迟估算
    - 禁用标签检测（避免 AI 输出元信息泄漏给用户）

设计要点：
    - 路径用 pathlib.Path，跨平台
    - 文件 IO 用 aiofiles（旧版用 run_in_executor 同步打开）
    - 表情包目录由调用方注入（ToolContext.emoji_dir），不再写死项目相对路径
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import aiofiles

logger = logging.getLogger(__name__)


# 禁止 AI 输出的"元信息泄漏"标签。命中即拒绝发送。
FORBIDDEN_TAGS: tuple[str, ...] = (
    "[私聊给",
    "[群聊",
    "[TO:",
    "我给 QQ",
    "我在群",
)


def contains_forbidden(text: str) -> bool:
    """检测文本是否包含禁止标签。"""
    return any(tag in text for tag in FORBIDDEN_TAGS)


def typing_delay(
    text: str,
    *,
    chars_per_second: float = 3.0,
    max_delay: float = 2.0,
) -> float:
    """模拟真人打字延迟。"""
    if not text:
        return 0.0
    if chars_per_second <= 0:
        return max_delay
    return min(len(text) / chars_per_second, max_delay)


def list_emoji_files(emoji_dir: Path | None) -> list[str]:
    """列出表情包目录下的图片文件（按文件名排序）。

    支持的扩展名：jpg / jpeg / png / gif。
    目录不存在或读不到时返回 []。
    """
    if emoji_dir is None or not emoji_dir.exists():
        return []
    try:
        return sorted(
            p.name
            for p in emoji_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif"}
        )
    except OSError as e:
        logger.warning(f"读取表情包目录失败 {emoji_dir}: {e}")
        return []


def build_emoji_hint(emoji_dir: Path | None) -> str:
    """构造给 LLM 的"可用表情包"提示。空目录时返回 ""。"""
    files = list_emoji_files(emoji_dir)
    if not files:
        return ""
    return "可用表情包：" + "、".join(files)


async def build_image_cq(
    image_name: str,
    emoji_dir: Path | None,
) -> str | None:
    """把表情包文件名转成 CQ:image base64 编码字符串。

    Args:
        image_name: 表情包文件名（不含目录）
        emoji_dir: 表情包目录

    Returns:
        CQ:image 字符串；文件不存在或读取失败时返回 None。
    """
    if not image_name:
        return None
    if emoji_dir is None:
        logger.warning("emoji_dir 未配置，无法构造 CQ:image")
        return None

    # 防止路径穿越：image_name 必须只是文件名，不能含 / 或 ..
    if "/" in image_name or "\\" in image_name or ".." in image_name:
        logger.warning(f"非法表情包路径：{image_name!r}")
        return None

    image_path = emoji_dir / image_name
    if not image_path.exists() or not image_path.is_file():
        logger.error(f"表情包不存在: {image_path}")
        return None

    try:
        async with aiofiles.open(image_path, "rb") as f:
            data = await f.read()
    except OSError as e:
        logger.error(f"读取表情包失败 {image_path}: {e}")
        return None

    b64 = base64.b64encode(data).decode("ascii")
    return f"[CQ:image,file=base64://{b64}]"


async def build_message(
    content: str | None,
    image: str | None,
    emoji_dir: Path | None,
) -> tuple[str | None, str | None]:
    """把 content/image 二选一的输入拼成 (发送字符串, 日志标签)。

    Args:
        content: 文本内容
        image: 表情包文件名
        emoji_dir: 表情包目录

    Returns:
        (msg, label):
            msg: 实际发送的字符串（CQ 码 / 普通文本）；构造失败时为 None
            label: 写入历史记录时的可读标签（如 "[表情包: smile.png]" 或文本本身）
    """
    if image:
        cq = await build_image_cq(image, emoji_dir)
        if cq is None:
            return None, None
        return cq, f"[表情包: {image}]"
    if content:
        return content, content
    return None, None
