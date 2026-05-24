"""关键词强制保存联动入口。

入站消息处理时调用 try_save_from_user(text, important)，命中关键词时
自动写入重要记忆，不依赖 AI 主动调用 save_important_memory。

设计：
    - 纯函数，不绑死消息管道，便于复用
    - 是否启用由调用方决定（按 features.long_term_memory.keyword_force_save）
    - 关键词列表透传到 ImportantMemoryManager.force_save_from_keyword，
      不在这里重复一遍（避免两处定义漂移）
"""

from __future__ import annotations

import logging

from memory import ImportantMemoryManager

logger = logging.getLogger(__name__)


async def try_save_from_user(
    text: str,
    important: ImportantMemoryManager | None,
    *,
    enabled: bool = True,
    keywords: list[str] | None = None,
) -> dict | None:
    """如果命中关键词，强制保存到重要记忆。

    Args:
        text: 用户消息文本
        important: 重要记忆管理器（None / 未启用 keyword 时跳过）
        enabled: features.long_term_memory.keyword_force_save 的值
        keywords: 自定义关键词列表。None 表示用 ImportantMemoryManager 内置默认列表。

    Returns:
        命中并保存：{"saved": True, "matched_keyword": "...", "content": "..."}
        未命中或未启用：None
    """
    if not enabled or important is None or not text:
        return None

    try:
        result = await important.force_save_from_keyword(text, keywords)
    except RuntimeError as e:
        # ImportantMemoryManager 未 load
        logger.warning(f"关键词保存跳过（重要记忆未加载）: {e}")
        return None
    except Exception as e:
        logger.exception(f"关键词保存异常: {e}")
        return None

    if result.get("saved"):
        return result
    return None
