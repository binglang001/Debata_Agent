"""旧版 JSONL 向量存储 + cosine top-k 检索。

新的 RAG 模式使用 rag_memory.SqliteVectorStore 索引会话历史。此模块保留给旧测试、
旧数据迁移和 cosine_similarity 复用。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiofiles
import orjson

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RagEntry:
    """单条 RAG 条目：原文 + 向量 + 元数据。"""

    id: str
    """与 ImportantMemoryManager 中条目同步的标识（这里用 timestamp + 内容前 N 字 hash 也行；先用 timestamp）"""

    text: str
    """原文（注入 prompt 时用的）"""

    vector: list[float]
    """embedding 向量"""

    meta: dict[str, Any] = field(default_factory=dict)
    """额外元信息（timestamp 等，注入 prompt 时不用）"""


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算 cosine 相似度。向量为空或维度不一致返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class RagStore:
    """旧版 RAG 向量存储。

    jsonl 文件每行一个 entry。加载到内存做检索，新增/删除即时落盘。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[RagEntry] = []
        self._loaded: bool = False

    async def load(self) -> None:
        """从 jsonl 文件加载到内存。"""
        if not self.path.exists():
            self._loaded = True
            return
        async with aiofiles.open(self.path, "rb") as f:
            raw = await f.read()
        loaded = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = orjson.loads(line)
                self._entries.append(RagEntry(
                    id=d["id"], text=d["text"],
                    vector=list(d.get("vector") or []),
                    meta=d.get("meta") or {},
                ))
                loaded += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"RagStore 跳过无效行：{e}")
        self._loaded = True
        logger.info(f"RagStore 加载 {loaded} 条向量条目")

    async def add(self, entry: RagEntry) -> None:
        """追加一条向量记忆。"""
        self._entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self.path, "ab") as f:
            await f.write(orjson.dumps({
                "id": entry.id, "text": entry.text,
                "vector": entry.vector, "meta": entry.meta,
            }) + b"\n")

    async def remove_by_id(self, entry_id: str) -> int:
        """按 id 删除条目（可能命中多条）。返回删除数。"""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        removed = before - len(self._entries)
        if removed > 0:
            await self._rewrite()
        return removed

    async def remove_by_predicate(self, pred) -> int:
        """按谓词删除，pred(entry) -> bool 为 True 的会被删。"""
        before = len(self._entries)
        self._entries = [e for e in self._entries if not pred(e)]
        removed = before - len(self._entries)
        if removed > 0:
            await self._rewrite()
        return removed

    async def replace_all(self, entries: list[RagEntry]) -> None:
        """用给定条目整体替换索引并重写磁盘。"""
        self._entries = list(entries)
        await self._rewrite()

    def top_k(self, query_vec: list[float], k: int = 5) -> list[tuple[RagEntry, float]]:
        """同步检索 top-k 最相似条目。返回 [(entry, score), ...] 按 score 降序。

        同步是因为 cosine 计算极快，无需 await。
        """
        if not query_vec or not self._entries:
            return []
        scored = [(e, cosine_similarity(query_vec, e.vector)) for e in self._entries]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: max(1, k)]

    def all_entries(self) -> list[RagEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    async def _rewrite(self) -> None:
        """全量重写 jsonl 文件。"""
        lines = [
            orjson.dumps({
                "id": e.id, "text": e.text,
                "vector": e.vector, "meta": e.meta,
            }) + b"\n"
            for e in self._entries
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(self.path, "wb") as f:
            await f.write(b"".join(lines))


__all__ = ["RagEntry", "RagStore", "cosine_similarity"]
