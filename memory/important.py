"""重要记忆管理 —— 持久保存的关键信息。

数据格式：
    [
        {
            "timestamp": "2026-03-05 10:00:00",
            "content": "...",
            "scope": "global",
            "pinned": false
        },
        ...
    ]

特性：
    - 体量小（通常几十到几百条），用整体 JSON 存储
    - 缓存为文本形式（"[重要记忆]\n- ...\n- ..."），便于直接嵌入 system prompt
    - scope / pinned 只影响注入选择，不拆分全局存储
    - 添加时支持外部去重回调（用 flash 模型判断语义重复）
    - 删除支持关键词模糊匹配
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from utils.token_budget import TokenEstimator

from .store import JsonStore

logger = logging.getLogger(__name__)

# 去重回调签名：(已存在的条目, 新内容) -> True 表示重复
DuplicateChecker = Callable[[list[dict], str], Awaitable[bool]]

GLOBAL_SCOPE = "global"
_VALID_SCOPE_RE = re.compile(r"^(global|user:[^:\s]+|group:[^:\s]+)$")


def normalize_scope(scope: str | None) -> str:
    """归一化重要记忆 scope；非法值退回 global。"""
    raw = (scope or "").strip()
    if not raw:
        return GLOBAL_SCOPE
    if raw.startswith("private:"):
        raw = "user:" + raw.split(":", 1)[1]
    if _VALID_SCOPE_RE.match(raw):
        return raw
    logger.warning(f"重要记忆 scope 无效，已按 global 保存: {raw!r}")
    return GLOBAL_SCOPE


def scope_from_conversation_id(conversation_id: str | None) -> str | None:
    """把会话标签映射成记忆选择 scope。"""
    raw = (conversation_id or "").strip()
    if not raw or ":" not in raw:
        return None
    kind, ident = raw.split(":", 1)
    ident = ident.strip()
    if not ident:
        return None
    if kind == "private":
        return normalize_scope(f"user:{ident}")
    if kind == "group":
        return normalize_scope(f"group:{ident}")
    if kind == "user":
        return normalize_scope(raw)
    return None


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
        # 兼容旧版本的 important-memory RAG；Runtime 的 RAG 模式已改用
        # RagMemoryService，不再 attach 这里。
        self._embedding = None  # type: ignore[var-annotated]
        self._rag_store = None  # type: ignore[var-annotated]

    def attach_rag(self, embedding_service, rag_store) -> None:
        """注入旧版 important-memory RAG 组件。

        Runtime 不再调用此方法；保留给旧数据迁移/外部兼容。
        """
        self._embedding = embedding_service
        self._rag_store = rag_store

    @property
    def rag_enabled(self) -> bool:
        return self._embedding is not None and self._rag_store is not None

    async def retrieve_for_query(
        self,
        query: str,
        top_k: int = 5,
        *,
        conversation_id: str | None = None,
        token_budget: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> str:
        """旧版 RAG：按 query 召回 top-k 重要记忆，拼接成可直接注入 prompt 的文本。

        未启用 RAG / query 为空 / 索引为空 时退回到按 scope 筛选的文本注入。
        """
        if not self.rag_enabled or not query.strip() or len(self._rag_store) == 0:  # type: ignore[arg-type]
            return self.text_for_context(
                conversation_id,
                token_budget=token_budget,
                estimator=estimator,
            )
        try:
            qvec = await self._embedding.embed_one(query)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"RAG embed query 失败，退回到全部注入：{e}")
            return self.text_for_context(
                conversation_id,
                token_budget=token_budget,
                estimator=estimator,
            )

        scope = scope_from_conversation_id(conversation_id)
        item_by_id = {self._item_id(item): item for item in self._items}
        allowed_ids = {
            self._item_id(item)
            for item in self._items
            if not item.get("pinned") and self._scope_matches(item.get("scope"), scope)
        }
        raw_hits = self._rag_store.top_k(qvec, k=max(top_k * 4, top_k, 20))  # type: ignore[union-attr]
        hits = [
            (entry, score)
            for entry, score in raw_hits
            if entry.id in allowed_ids
        ][:top_k]

        pinned = [item for item in self._items if item.get("pinned")]
        if not hits and not pinned:
            return self.text_for_context(
                conversation_id,
                token_budget=token_budget,
                estimator=estimator,
            )

        lines = [f"[重要记忆 · RAG 召回 top-{len(hits)}]"]
        for item in pinned:
            lines.append(self._format_item_line(item))
        for entry, score in hits:
            item = item_by_id.get(entry.id)
            prefix = self._format_item_prefix(item or entry.meta)
            if prefix:
                lines.append(f"- ({score:.2f}) {prefix} {entry.text}")
            else:
                lines.append(f"- ({score:.2f}) {entry.text}")
        return self._fit_lines_to_budget(lines, token_budget, estimator)

    async def _index_in_rag(self, item: dict) -> None:
        """save 成功后异步索引一份。失败仅 warn 不影响主流程。"""
        if not self.rag_enabled:
            return
        try:
            from .rag_store import RagEntry
            vec = await self._embedding.embed_one(item["content"])  # type: ignore[union-attr]
            await self._rag_store.add(  # type: ignore[union-attr]
                RagEntry(
                    id=self._item_id(item),
                    text=item["content"],
                    vector=vec,
                    meta={
                        "timestamp": item["timestamp"],
                        "scope": item.get("scope", GLOBAL_SCOPE),
                        "pinned": bool(item.get("pinned")),
                    },
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"RAG 索引失败（不影响保存）：{e}")

    async def load(self) -> None:
        """从磁盘加载到内存缓存。必须先调用。"""
        data = await self._store.read(default=[])
        if not isinstance(data, list):
            logger.warning("重要记忆文件格式不是 list，重置为空")
            data = []
        self._items = [self._normalize_item(item) for item in data if isinstance(item, dict)]
        self._refresh_text_cache()
        self._loaded = True

    def text(self) -> str:
        """获取完整缓存文本。保留给管理界面/兼容调用；模型注入优先用 text_for_context。"""
        return self._cached_text

    def text_for_context(
        self,
        conversation_id: str | None = None,
        *,
        token_budget: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> str:
        """按当前会话 scope 选择要注入的记忆文本。

        记忆仍是一份全局存储；scope 只影响本轮优先 surface 哪些条目。
        pinned 条目永远注入，其余条目只注入 global 与当前 scope 匹配项。
        """
        scope = scope_from_conversation_id(conversation_id)
        selected = self._select_for_scope(scope)
        return self._format_items(selected, token_budget=token_budget, estimator=estimator)

    def items(self) -> list[dict]:
        """获取所有条目的副本。"""
        return list(self._items)

    async def save(
        self,
        content: str,
        check_dup: DuplicateChecker | None = None,
        *,
        scope: str | None = None,
        pinned: bool = False,
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

        item = self._normalize_item(
            {
                "timestamp": self._now_fn(),
                "content": content,
                "scope": normalize_scope(scope),
                "pinned": bool(pinned),
            }
        )
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
        keep, removed_ids = [], []
        for m in self._items:
            if keyword in (m.get("content") or ""):
                removed_ids.append(self._item_id(m))
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
                for entry_id in removed_ids:
                    if entry_id:
                        try:
                            await self._rag_store.remove_by_id(entry_id)  # type: ignore[union-attr]
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
            current_id = self._item_id(item)
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
        *,
        scope: str | None = None,
        pinned: bool = False,
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
        item = self._normalize_item(
            {
                "timestamp": self._now_fn(),
                "content": content,
                "source": f"keyword:{matched}",
                "scope": normalize_scope(scope),
                "pinned": bool(pinned),
            }
        )
        self._items.append(item)
        await self._store.write(self._items)
        self._refresh_text_cache()
        logger.info(f"关键词强制保存触发 ({matched}): {content[:50]}")
        await self._index_in_rag(item)
        return {"saved": True, "matched_keyword": matched, "content": content}

    async def update_metadata(
        self,
        item_id: str,
        *,
        scope: str | None = None,
        pinned: bool | None = None,
    ) -> bool:
        """更新一条记忆的 scope / pinned 元数据。"""
        if not self._loaded:
            raise RuntimeError("ImportantMemoryManager 尚未调用 load()")
        item_id = (item_id or "").strip()
        if not item_id:
            return False

        changed = False
        for item in self._items:
            if self._item_id(item) != item_id:
                continue
            if scope is not None:
                item["scope"] = normalize_scope(scope)
                changed = True
            if pinned is not None:
                item["pinned"] = bool(pinned)
                changed = True
            break
        else:
            return False

        if not changed:
            return True
        await self._store.write(self._items)
        self._refresh_text_cache()
        await self._rebuild_rag_index()
        return True

    async def replace_all(self, items: list[dict]) -> None:
        """整体替换（总结后用）。"""
        # 校验：每条至少应有 content
        cleaned: list[dict] = []
        for item in items:
            content = (item.get("content") or "").strip()
            if not content:
                continue
            normalized = self._normalize_item({**item, "content": content})
            cleaned.append(normalized)
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
                        id=self._item_id(item),
                        text=item["content"],
                        vector=vec,
                        meta={
                            "timestamp": item["timestamp"],
                            "scope": item.get("scope", GLOBAL_SCOPE),
                            "pinned": bool(item.get("pinned")),
                        },
                    )
                )
            await self._rag_store.replace_all(entries)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"RAG 索引重建失败（不影响重要记忆替换）：{e}")

    def _refresh_text_cache(self) -> None:
        if not self._items:
            self._cached_text = ""
            return
        self._cached_text = self._format_items(self._items)

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        content = str(item.get("content") or "").strip()
        normalized = dict(item)
        normalized["timestamp"] = str(item.get("timestamp") or self._now_fn())
        normalized["content"] = content
        normalized["scope"] = normalize_scope(str(item.get("scope") or GLOBAL_SCOPE))
        normalized["pinned"] = bool(item.get("pinned", False))
        return normalized

    def _item_id(self, item: dict[str, Any]) -> str:
        return str(item.get("id") or item.get("timestamp") or "")

    def _scope_matches(self, item_scope: str | None, current_scope: str | None) -> bool:
        scope = normalize_scope(item_scope)
        if scope == GLOBAL_SCOPE:
            return True
        return bool(current_scope and scope == current_scope)

    def _select_for_scope(self, current_scope: str | None) -> list[dict[str, Any]]:
        pinned = [item for item in self._items if item.get("pinned")]
        regular = [
            item
            for item in self._items
            if not item.get("pinned") and self._scope_matches(item.get("scope"), current_scope)
        ]

        def sort_key(item: dict[str, Any]) -> tuple[int, str]:
            scope = normalize_scope(item.get("scope"))
            exact = 1 if current_scope and scope == current_scope else 0
            return (exact, str(item.get("timestamp") or ""))

        pinned.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        regular.sort(key=sort_key, reverse=True)
        return pinned + regular

    def _format_items(
        self,
        items: list[dict[str, Any]],
        *,
        token_budget: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> str:
        if not items:
            return ""
        lines = ["[重要记忆]"]
        lines.extend(self._format_item_line(item) for item in items if item.get("content"))
        return self._fit_lines_to_budget(lines, token_budget, estimator)

    def _format_item_line(self, item: dict[str, Any]) -> str:
        prefix = self._format_item_prefix(item)
        content = str(item.get("content") or "")
        return f"- {prefix} {content}".rstrip() if prefix else f"- {content}"

    def _format_item_prefix(self, item: dict[str, Any] | None) -> str:
        if not item:
            return ""
        parts: list[str] = []
        if item.get("pinned"):
            parts.append("[置顶]")
        scope = normalize_scope(str(item.get("scope") or GLOBAL_SCOPE))
        if scope != GLOBAL_SCOPE:
            parts.append(f"[{scope}]")
        return "".join(parts)

    def _fit_lines_to_budget(
        self,
        lines: list[str],
        token_budget: int | None,
        estimator: TokenEstimator | None,
    ) -> str:
        if not lines:
            return ""
        if not token_budget or token_budget <= 0:
            return "\n".join(lines)
        estimator = estimator or TokenEstimator()
        header, rest = lines[0], lines[1:]
        protected = [line for line in rest if "[置顶]" in line]
        regular = [line for line in rest if "[置顶]" not in line]
        selected = [header, *protected]
        used = estimator.estimate_text("\n".join(selected))
        for line in regular:
            cost = estimator.estimate_text(line)
            if selected and used + cost > token_budget:
                continue
            selected.append(line)
            used += cost
        return "\n".join(selected)


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
