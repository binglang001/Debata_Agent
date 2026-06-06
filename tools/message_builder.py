"""消息构造辅助 —— 从旧 handler.py 迁移并去 NoneBot 化。

职责：
    - 拼装文本 / 表情包 / 图片消息的可发送动作
    - 表情包名称解析（emoji 名称 → emoji 目录下的真实图片文件）
    - 打字延迟估算
    - 禁用标签检测（避免 AI 输出元信息泄漏给用户）

设计要点：
    - 路径用 pathlib.Path，跨平台
    - 文件 IO 用 aiofiles（旧版用 run_in_executor 同步打开）
    - 表情包目录由调用方注入（ToolContext.emoji_dir），不再写死项目相对路径
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".gif"})


class MessageBuildError(Exception):
    """消息构造失败，错误文本会返回给模型作为工具错误。"""


# 禁止 AI 输出的"元信息泄漏"标签。命中即拒绝发送。
FORBIDDEN_TAGS: tuple[str, ...] = (
    "[私聊给",
    "[群聊",
    "[TO:",
    "我给 QQ",
    "我在群",
    "思考过程",
    "<retrieved_conversation_context",
    "</retrieved_conversation_context>",
    "<task_context",
    "</task_context>",
    "<agent_task_result",
    "</agent_task_result>",
    "<send_receipt",
    "</send_receipt>",
    "工具结果 ·",
    "调用了：",
    "RAG里提到",
    "RAG 里提到",
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
            if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTS
        )
    except OSError as e:
        logger.warning(f"读取表情包目录失败 {emoji_dir}: {e}")
        return []


def list_emoji_names(emoji_dir: Path | None) -> list[str]:
    """列出表情包名称（不含扩展名），供 LLM 使用。"""
    names: list[str] = []
    seen: set[str] = set()
    for filename in list_emoji_files(emoji_dir):
        name = Path(filename).stem
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def build_emoji_hint(emoji_dir: Path | None) -> str:
    """构造给 LLM 的"可用表情包"提示。空目录时返回 ""。"""
    names = list_emoji_names(emoji_dir)
    if not names:
        return ""
    return "可用表情包：" + "、".join(names)


def resolve_emoji_path(emoji_name: str, emoji_dir: Path | None) -> Path:
    """把不带后缀的表情包名称解析成真实文件。"""
    emoji_name = (emoji_name or "").strip()
    if not emoji_name:
        raise MessageBuildError("表情包名称为空")
    if emoji_dir is None:
        raise MessageBuildError("表情包目录未配置")

    if "/" in emoji_name or "\\" in emoji_name or ".." in emoji_name:
        raise MessageBuildError("表情包名称不能包含路径")

    matches = [
        p
        for p in emoji_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_IMAGE_EXTS
        and p.stem == emoji_name
    ] if emoji_dir.exists() else []
    if not matches:
        raise MessageBuildError(f"表情包不存在：{emoji_name}")
    if len(matches) > 1:
        choices = "、".join(p.name for p in sorted(matches, key=lambda p: p.name))
        raise MessageBuildError(f"表情包名称有多个同名文件：{choices}")
    return matches[0]


def resolve_send_image_ref(
    image_ref: str,
    workspace_dir: Path | None,
) -> tuple[Path | None, str | None, str]:
    """解析 send_* 的 image 字段。

    image 是通用图片发送，不指向 emoji 目录。允许 http(s) URL 或 workspace 内路径。
    """
    value = (image_ref or "").strip()
    if not value:
        raise MessageBuildError("图片引用为空")

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return None, value, "[图片]"
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise MessageBuildError("image 只支持 http(s) URL 或 workspace 内路径")

    from .workspace import WorkspaceError, relative_to_workspace, resolve_in_workspace

    try:
        image_path = resolve_in_workspace(value, workspace_dir)
    except WorkspaceError as e:
        raise MessageBuildError(str(e)) from e
    if not image_path.exists() or not image_path.is_file():
        raise MessageBuildError("图片文件不存在")
    if image_path.suffix.lower() not in SUPPORTED_IMAGE_EXTS:
        raise MessageBuildError("image 只支持 jpg/jpeg/png/gif 图片")
    label = f"[图片: {relative_to_workspace(image_path, workspace_dir)}]"
    return image_path, None, label


def build_message_action(
    content: str | None,
    emoji: str | None,
    image: str | None,
    emoji_dir: Path | None,
    workspace_dir: Path | None,
) -> dict:
    """把 content/emoji/image 三选一输入拼成发送动作片段。

    返回字段可直接 merge 到发送 action。
    """
    values = [value for value in (content, emoji, image) if str(value or "").strip()]
    if len(values) != 1:
        raise MessageBuildError("content、emoji、image 必须且只能填写一个")

    if content and content.strip():
        return {
            "kind": "text",
            "content": content,
            "label": content,
        }
    if emoji and emoji.strip():
        path = resolve_emoji_path(emoji, emoji_dir)
        return {
            "kind": "emoji",
            "image_path": str(path),
            "content": "",
            "label": f"[表情包: {emoji.strip()}]",
        }
    if image and image.strip():
        image_path, image_url, label = resolve_send_image_ref(image, workspace_dir)
        return {
            "kind": "image",
            "image_path": str(image_path) if image_path is not None else "",
            "image_url": image_url or "",
            "content": "",
            "label": label,
        }
    raise MessageBuildError("内容为空")
