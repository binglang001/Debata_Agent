"""永久归档存储。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .archive_sqlite import SqliteArchiveStore


class ArchiveStore:
    """全局统一历史归档，外部接口兼容旧 JSONL 版本。"""

    def __init__(self, path: Path) -> None:
        self._store = SqliteArchiveStore(path)

    @property
    def path(self) -> Path:
        return self._store.path

    async def load(self, force_reload: bool = False) -> list[dict]:
        return await self._store.load(force_reload=force_reload)

    async def append_many(self, records: list[dict[str, Any]]) -> None:
        await self._store.append_many(records)

    async def records(self) -> list[dict]:
        return await self._store.records()

    async def search(
        self,
        *,
        conversation_id: str | None = None,
        keyword: str | None = None,
        time_range: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        return await self._store.search(
            conversation_id=conversation_id,
            keyword=keyword,
            time_range=time_range,
            limit=limit,
        )

    async def filter_records(self, query: Any) -> dict[str, Any]:
        return await self._store.filter_records(query)

    async def get_by_ids(self, archive_ids: list[str]) -> list[dict]:
        return await self._store.get_by_ids(archive_ids)

    async def context_around(
        self,
        archive_id: str,
        before: int,
        after: int,
    ) -> list[dict]:
        return await self._store.context_around(archive_id, before, after)

    async def rag_records(self) -> list[dict]:
        return await self._store.rag_records()

    async def media_records(self, archive_id: str | None = None) -> list[dict[str, Any]]:
        return await self._store.media_records(archive_id)
