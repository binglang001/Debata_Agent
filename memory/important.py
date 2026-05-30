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
import re
from pathlib import Path
from typing import Awaitable, Callable

from .store import JsonStore

logger = logging.getLogger(__name__)

# 去重回调签名：(已存在的条目, 新内容) -> True 表示重复
DuplicateChecker = Callable[[list[dict], str], Awaitable[bool]]


# 默认的"记住"类关键词。命中即强制保存（绕过 AI 主动判断）
DEFAULT_FORCE_SAVE_KEYWORDS: list[str] = [
    "记住", "请记住", "一定要记住", "记一下", "记下", "帮我记", "帮我记一下",
    "记一笔", "记：", "记:", "重要的是",
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
        # RAG 模式可选：runtime 装配时调 attach_rag(embedding, rag_store)
        self._embedding = None  # type: ignore[var-annotated]
        self._rag_store = None  # type: ignore[var-annotated]

    def attach_rag(self, embedding_service, rag_store) -> None:
        """注入 RAG 组件。runtime 在 long_term_memory.mode='rag' 时调用。

        attach 后，每次 save() 会异步索引一份向量；retrieve_for_query 走 cosine top-k。
        """
        self._embedding = embedding_service
        self._rag_store = rag_store

    @property
    def rag_enabled(self) -> bool:
        return self._embedding is not None and self._rag_store is not None

    async def retrieve_for_query(self, query: str, top_k: int = 5) -> str:
        """RAG 模式下按 query 召回 top-k 重要记忆，拼接成可直接注入 prompt 的文本。

        未启用 RAG / query 为空 / 索引为空 时退回到 text()（即全部条目）。
        """
        if not self.rag_enabled or not query.strip() or len(self._rag_store) == 0:  # type: ignore[arg-type]
            return self.text()
        try:
            qvec = await self._embedding.embed_one(query)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"RAG embed query 失败，退回到全部注入：{e}")
            return self.text()
        hits = self._rag_store.top_k(qvec, k=top_k)  # type: ignore[union-attr]
        if not hits:
            return self.text()
        lines = ["[重要记忆 · RAG 召回 top-{}]".format(len(hits))]
        for entry, score in hits:
            lines.append(f"- ({score:.2f}) {entry.text}")
        return "\n".join(lines)

    async def _index_in_rag(self, item: dict) -> None:
        """save 成功后异步索引一份。失败仅 warn 不影响主流程。"""
        if not self.rag_enabled:
            return
        try:
            from .rag_store import RagEntry
            vec = await self._embedding.embed_one(item["content"])  # type: ignore[union-attr]
            await self._rag_store.add(RagEntry(  # type: ignore[union-attr]
                id=item["timestamp"], text=item["content"],
                vector=vec, meta={"timestamp": item["timestamp"]},
            ))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"RAG 索引失败（不影响保存）：{e}")

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

        item = {"timestamp": self._now_fn(), "content": content}
        self._items.append(item)
        await self._store.write(self._items)
        self._refresh_text_cache()
        logger.info(f"重要记忆已保存: {content}")
        await self._index_in_rag(item)
        return {"saved": True, "duplicate": False}

    async def delete_by_keyword(self, keyword: str) -> int:
        """按关键词模糊匹配删除。返回删除数。"""
        if not self._loaded:
            raise RuntimeError("ImportantMemoryManager 尚未调用 load()")
        if not keyword:
            return 0

        before = len(self._items)
        keep, removed_ts = [], []
        for m in self._items:
            if keyword in (m.get("content") or ""):
                removed_ts.append(m.get("timestamp"))
            else:
                keep.append(m)
        self._items = keep
        deleted = before - len(self._items)
        if deleted > 0:
            await self._store.write(self._items)
            self._refresh_text_cache()
            logger.info(f"重要记忆删除 {deleted} 条 (关键词={keyword})")
            # 同步从 RAG 索引移除
            if self.rag_enabled:
                for ts in removed_ts:
                    if ts:
                        try:
                            await self._rag_store.remove_by_id(ts)  # type: ignore[union-attr]
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"RAG 索引移除失败：{e}")
        return deleted

    async def delete_by_id(self, item_id: str) -> bool:
        """按 timestamp/id 精确删除一条记忆，返回是否删除。"""
        if not self._loaded:
            raise RuntimeError("ImportantMemoryManager 尚未调用 load()")
        item_id = (item_id or "").strip()
        if not item_id:
            return False

        keep: list[dict] = []
        removed: dict | None = None
        for item in self._items:
            current_id = str(item.get("id") or item.get("timestamp") or "")
            if removed is None and current_id == item_id:
                removed = item
                continue
            keep.append(item)

        if removed is None:
            return False

        self._items = keep
        await self._store.write(self._items)
        self._refresh_text_cache()
        logger.info(f"重要记忆删除 1 条 (id={item_id})")
        if self.rag_enabled:
            try:
                await self._rag_store.remove_by_id(item_id)  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001
                logger.warning(f"RAG 索引移除失败：{e}")
        return True

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

        content = _strip_memory_keyword(text, matched)
        item = {
            "timestamp": self._now_fn(),
            "content": content,
            "source": f"keyword:{matched}",
        }
        self._items.append(item)
        await self._store.write(self._items)
        self._refresh_text_cache()
        logger.info(f"关键词强制保存触发 ({matched}): {content[:50]}")
        await self._index_in_rag(item)
        return {"saved": True, "matched_keyword": matched, "content": content}

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
        await self._rebuild_rag_index()

    async def _rebuild_rag_index(self) -> None:
        """整体替换重要记忆后重建 RAG 索引。失败仅 warn，不影响 JSON 记忆。"""
        if not self.rag_enabled:
            return
        try:
            from .rag_store import RagEntry

            entries: list[RagEntry] = []
            for item in self._items:
                vec = await self._embedding.embed_one(item["content"])  # type: ignore[union-attr]
                entries.append(
                    RagEntry(
                        id=item["timestamp"],
                        text=item["content"],
                        vector=vec,
                        meta={"timestamp": item["timestamp"]},
                    )
                )
            await self._rag_store.replace_all(entries)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"RAG 索引重建失败（不影响重要记忆替换）：{e}")

    def _refresh_text_cache(self) -> None:
        if not self._items:
            self._cached_text = ""
            return
        lines = [f"- {m['content']}" for m in self._items if m.get("content")]
        self._cached_text = "[重要记忆]\n" + "\n".join(lines)


def _default_now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _strip_memory_keyword(text: str, keyword: str) -> str:
    """去掉用户显式“记住/记一下”等引导词，保存真正的事实内容。"""
    content = text.strip()
    if keyword and content.startswith(keyword):
        content = content[len(keyword):]
        content = re.sub(r"^[\s:：,，。.!！-]+", "", content).strip()
    return content or text.strip()
