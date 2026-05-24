"""重要记忆管理 —— 持久保存的关键信息。

数据格式：
    [
        {"timestamp": "2026-03-05 10:00:00", "content": "..."},
        ...
    ]

特性：
    - 体量小（通常几十到几百条），用整体 JSON 存储
    - 缓存为文本形式（"[重要记忆]\n- ...\n- ..."），便于直接嵌入 system prompt
    - 添加时支持外部去重回调（用 flash 模型判断语义重复）
    - 删除支持关键词模糊匹配
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from .store import JsonStore

logger = logging.getLogger(__name__)

# 去重回调签名：(已存在的条目, 新内容) -> True 表示重复
DuplicateChecker = Callable[[list[dict], str], Awaitable[bool]]


# 默认的"记住"类关键词。命中即强制保存（绕过 AI 主动判断）
DEFAULT_FORCE_SAVE_KEYWORDS: list[str] = [
    "记住", "请记住", "一定要记住", "重要的是",
    "约定", "约好", "承诺",
    "我叫", "我是", "我的名字",
    "我的 QQ", "我的qq", "QQ 是", "qq是",
]


class ImportantMemoryManager:
    """重要记忆管理器。"""

    def __init__(self, path: Path, now_fn: Callable[[], str] | None = None) -> None:
        self._store = JsonStore(path)
        self._items: list[dict] = []
        self._cached_text: str = ""
        self._loaded: bool = False
        self._now_fn = now_fn or _default_now

    async def load(self) -> None:
        """从磁盘加载到内存缓存。必须先调用。"""
        data = await self._store.read(default=[])
        if not isinstance(data, list):
            logger.warning(f"重要记忆文件格式不是 list，重置为空")
            data = []
        self._items = data
        self._refresh_text_cache()
        self._loaded = True

    def text(self) -> str:
        """获取缓存的文本表示。供 build_messages 直接嵌入。"""
        return self._cached_text

    def items(self) -> list[dict]:
        """获取所有条目的副本。"""
        return list(self._items)

    async def save(
        self,
        content: str,
        check_dup: DuplicateChecker | None = None,
    ) -> dict:
        """保存一条重要记忆。

        Args:
            content: 记忆内容（一句话概括）
            check_dup: 可选的去重检查器（async）

        Returns:
            {"saved": bool, "duplicate": bool}
        """
        if not self._loaded:
            raise RuntimeError("ImportantMemoryManager 尚未调用 load()")

        content = content.strip()
        if not content:
            return {"saved": False, "duplicate": False}

        if check_dup and self._items:
            try:
                is_dup = await check_dup(self._items, content)
                if is_dup:
                    logger.info(f"重要记忆去重跳过: {content[:40]}")
                    return {"saved": False, "duplicate": True}
            except Exception as e:
                logger.warning(f"去重检查失败，继续保存: {e}")

        self._items.append({"timestamp": self._now_fn(), "content": content})
        await self._store.write(self._items)
        self._refresh_text_cache()
        logger.info(f"重要记忆已保存: {content}")
        return {"saved": True, "duplicate": False}

    async def delete_by_keyword(self, keyword: str) -> int:
        """按关键词模糊匹配删除。返回删除数。"""
        if not self._loaded:
            raise RuntimeError("ImportantMemoryManager 尚未调用 load()")
        if not keyword:
            return 0

        before = len(self._items)
        self._items = [
            m for m in self._items if keyword not in (m.get("content") or "")
        ]
        deleted = before - len(self._items)
        if deleted > 0:
            await self._store.write(self._items)
            self._refresh_text_cache()
            logger.info(f"重要记忆删除 {deleted} 条 (关键词={keyword})")
        return deleted

    async def force_save_from_keyword(
        self,
        text: str,
        keywords: list[str] | None = None,
    ) -> dict:
        """关键词触发的强制保存（不走去重检查，用于"记住 X"/"我叫 X"等场景）。

        Args:
            text: 待提取的原始消息文本
            keywords: 关键词列表，命中任一即触发保存。None 时使用默认列表

        Returns:
            {"saved": bool, "matched_keyword": str | None, "content": str}
        """
        if not self._loaded:
            raise RuntimeError("ImportantMemoryManager 尚未调用 load()")

        if keywords is None:
            keywords = DEFAULT_FORCE_SAVE_KEYWORDS

        text = (text or "").strip()
        if not text:
            return {"saved": False, "matched_keyword": None, "content": ""}

        matched = next((k for k in keywords if k in text), None)
        if matched is None:
            return {"saved": False, "matched_keyword": None, "content": ""}

        self._items.append(
            {
                "timestamp": self._now_fn(),
                "content": text,
                "source": f"keyword:{matched}",
            }
        )
        await self._store.write(self._items)
        self._refresh_text_cache()
        logger.info(f"关键词强制保存触发 ({matched}): {text[:50]}")
        return {"saved": True, "matched_keyword": matched, "content": text}

    async def replace_all(self, items: list[dict]) -> None:
        """整体替换（总结后用）。"""
        # 校验：每条至少应有 content
        cleaned: list[dict] = []
        for item in items:
            content = (item.get("content") or "").strip()
            if not content:
                continue
            cleaned.append(
                {
                    "timestamp": item.get("timestamp") or self._now_fn(),
                    "content": content,
                }
            )
        self._items = cleaned
        await self._store.write(self._items)
        self._refresh_text_cache()
        self._loaded = True
        logger.info(f"重要记忆整体替换为 {len(cleaned)} 条")

    def _refresh_text_cache(self) -> None:
        if not self._items:
            self._cached_text = ""
            return
        lines = [f"- {m['content']}" for m in self._items if m.get("content")]
        self._cached_text = "[重要记忆]\n" + "\n".join(lines)


def _default_now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
