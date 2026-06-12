"""永久归档存储。

ArchiveStore 保存从活跃 history 中驱逐出去的原始记录。它是 append-only JSONL，
不按会话拆文件；conversation_id 只是检索标签。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import JsonlStore


class ArchiveStore:
    """全局统一历史归档。"""

    def __init__(self, path: Path) -> None:
        self._store = JsonlStore(path)

    async def load(self, force_reload: bool = False) -> list[dict]:
        return await self._store.load(force_reload=force_reload)

    async def append_many(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        await self._store.append_many(records)

    async def records(self) -> list[dict]:
        return await self._store.load()

    async def search(
        self,
        *,
        conversation_id: str | None = None,
        keyword: str | None = None,
        time_range: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """按会话和关键词检索归档记录，返回最新的 limit 条。"""
        keyword = (keyword or "").strip()
        time_range = (time_range or "").strip()
        matched: list[dict] = []
        for record in await self.records():
            if conversation_id and record.get("conversation_id") != conversation_id:
                continue
            text = _record_text(record)
            if keyword and keyword not in text:
                continue
            if time_range and time_range not in text:
                continue
            matched.append(record)
        return matched[-max(1, limit):]


def _record_text(record: dict) -> str:
    parts = [str(record.get("content") or "")]
    meta = record.get("metadata")
    if isinstance(meta, dict):
        parts.append(str(meta))
    return "\n".join(parts)
