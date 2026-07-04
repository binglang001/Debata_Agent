"""记忆仓储协议测试。"""

from __future__ import annotations

from typing import Any

import pytest

from memory import (
    ArchiveStore,
    ArchiveStoreLike,
    DebataArchiveStore,
    DebataEventStore,
    DebataHistoryStore,
    DebataImportantStore,
    DebataRollingSummaryStore,
    DebataUsageStatsStore,
    EventAppenderLike,
    EventJournal,
    EventStoreLike,
    HistoryManager,
    ImportantMemoryManager,
    JsonlStore,
    JsonlStoreLike,
    JsonStore,
    JsonStoreLike,
    RollingSummaryStore,
    RollingSummaryStoreLike,
    UsageStatsStoreLike,
)


class MemoryJsonStore:
    def __init__(self, initial: Any = None) -> None:
        self.data = initial
        self.writes: list[Any] = []

    async def read(self, default: Any = None) -> Any:
        if self.data is None:
            return default if default is not None else {}
        return self.data

    async def write(self, data: Any) -> None:
        self.data = data
        self.writes.append(data)


class MemoryJsonlStore:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def load(self, force_reload: bool = False) -> list[dict]:
        del force_reload
        return list(self.records)

    async def append(self, record: dict) -> None:
        self.records.append(record)

    async def append_many(self, records: list[dict]) -> None:
        self.records.extend(records)

    async def length(self) -> int:
        return len(self.records)

    async def get_slice(self, start: int = 0, end: int | None = None) -> list[dict]:
        return list(self.records[start:end])

    async def truncate_head(self, cut_point: int) -> int:
        self.records = self.records[cut_point:]
        return len(self.records)

    async def replace_all(self, records: list[dict]) -> None:
        self.records = list(records)

    async def clear(self) -> None:
        self.records = []


class MemoryArchiveStore:
    def __init__(self) -> None:
        self.records_data: list[dict] = []

    async def load(self, force_reload: bool = False) -> list[dict]:
        del force_reload
        return list(self.records_data)

    async def append_many(self, records: list[dict[str, Any]]) -> None:
        self.records_data.extend(records)

    async def records(self) -> list[dict]:
        return list(self.records_data)

    async def search(
        self,
        *,
        conversation_id: str | None = None,
        keyword: str | None = None,
        time_range: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        del conversation_id, keyword, time_range
        return list(self.records_data[:limit])

    async def filter_records(self, query: Any) -> dict[str, Any]:
        return {"query": query, "items": list(self.records_data)}

    async def get_by_ids(self, archive_ids: list[str]) -> list[dict]:
        requested = set(archive_ids)
        return [
            record
            for record in self.records_data
            if str(record.get("archive_id") or "") in requested
        ]

    async def context_around(
        self,
        archive_id: str,
        before: int,
        after: int,
    ) -> list[dict]:
        del before, after
        return await self.get_by_ids([archive_id])

    async def rag_records(self) -> list[dict]:
        return list(self.records_data)

    async def media_records(self, archive_id: str | None = None) -> list[dict[str, Any]]:
        if archive_id is None:
            return []
        return [{"archive_id": archive_id, "media": True}]


class MemoryEventStore:
    def __init__(self) -> None:
        self.started = False
        self.shutdown_timeout: float | None = None
        self.appended_events: list[Any] = []

    async def start_projection(self) -> None:
        self.started = True

    async def shutdown(self, *, timeout: float | None = 5.0) -> bool:
        self.shutdown_timeout = timeout
        return True

    async def wait_projected(
        self,
        event_id: int | None = None,
        *,
        timeout: float | None = None,
    ) -> bool:
        del event_id, timeout
        return True

    async def append_event(
        self,
        *,
        event_type: str,
        payload: Any,
        event_uuid: str | None = None,
        conversation_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | int | None = None,
        source: str | None = None,
        external_id: str | None = None,
        tool_call_id: str | None = None,
        parent_event_id: int | None = None,
        idempotency_key: str | None = None,
        timestamp_unix: float | int | None = None,
        created_at_unix: float | int | None = None,
        schema_version: int = 1,
    ) -> int:
        del (
            event_type,
            payload,
            event_uuid,
            conversation_id,
            session_id,
            turn_id,
            source,
            external_id,
            tool_call_id,
            parent_event_id,
            idempotency_key,
            timestamp_unix,
            created_at_unix,
            schema_version,
        )
        return 1

    async def append_events(self, events: list[Any]) -> list[int]:
        self.appended_events.extend(events)
        return list(range(1, len(events) + 1))

    async def stats(self) -> dict[str, Any]:
        return {"pending_count": 0}

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        return {"event_id": event_id}

    async def get_events(self, event_ids: list[int]) -> list[dict[str, Any] | None]:
        return [{"event_id": event_id} for event_id in event_ids]

    async def iter_events(
        self,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        del after_event_id, before_event_id, order
        return [{"event_id": idx + 1} for idx in range(limit)]

    async def events_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        before_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        del before_event_id
        return [{"conversation_id": conversation_id} for _ in range(limit)]

    async def events_by_type(
        self,
        event_type: str,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        del after_event_id, before_event_id, order
        return [{"event_type": event_type} for _ in range(limit)]


class MemoryUsageStatsStore:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def load(self) -> None:
        return None

    async def record(
        self,
        usage: Any,
        *,
        provider: str = "",
        model: str = "",
        agent: str = "",
        operation: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.records.append((usage, provider, model, agent, operation, extra))

    def summarize(self, range_name: Any = "today") -> dict[str, Any]:
        return {"range": range_name, "count": len(self.records)}

    @property
    def count(self) -> int:
        return len(self.records)


def test_store_protocols_are_exported_and_runtime_checkable(tmp_path):
    assert isinstance(JsonStore(tmp_path / "x.json"), JsonStoreLike)
    assert isinstance(JsonlStore(tmp_path / "x.jsonl"), JsonlStoreLike)
    assert isinstance(MemoryArchiveStore(), ArchiveStoreLike)
    assert isinstance(RollingSummaryStore(tmp_path / "summary.json"), RollingSummaryStoreLike)
    assert isinstance(MemoryEventStore(), EventAppenderLike)
    assert isinstance(MemoryEventStore(), EventStoreLike)
    assert isinstance(MemoryUsageStatsStore(), UsageStatsStoreLike)


def test_debata_stores_match_store_protocols(tmp_path):
    db_path = tmp_path / "debata.db"

    assert isinstance(DebataHistoryStore(db_path, "yuexi"), JsonlStoreLike)
    assert isinstance(DebataImportantStore(db_path, "yuexi"), JsonStoreLike)
    assert isinstance(DebataArchiveStore(db_path, "yuexi"), ArchiveStoreLike)
    assert isinstance(DebataRollingSummaryStore(db_path, "yuexi"), RollingSummaryStoreLike)
    assert isinstance(DebataEventStore(db_path, "yuexi"), EventStoreLike)
    assert isinstance(DebataUsageStatsStore(db_path, "yuexi"), UsageStatsStoreLike)


@pytest.mark.asyncio
async def test_history_manager_uses_jsonl_store_protocol(tmp_path):
    store = MemoryJsonlStore()
    history = HistoryManager(tmp_path / "unused.jsonl", store=store)

    await history.add_user_message("hi")
    await history.add_assistant_message("hello")

    assert await history.length() == 2
    assert await history.records() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


@pytest.mark.asyncio
async def test_important_manager_uses_json_store_protocol(tmp_path):
    store = MemoryJsonStore([])
    manager = ImportantMemoryManager(
        tmp_path / "unused.json",
        now_fn=lambda: "T1",
        store=store,
    )

    await manager.load()
    saved = await manager.save("协议注入记忆", scope="group:42")

    assert saved["saved"] is True
    assert store.writes[-1][0]["content"] == "协议注入记忆"
    assert "[group:42]" in manager.text_for_context("group:42")


@pytest.mark.asyncio
async def test_archive_store_uses_archive_store_protocol(tmp_path):
    backend = MemoryArchiveStore()
    archive = ArchiveStore(tmp_path / "unused.sqlite3", store=backend)

    await archive.append_many([{"archive_id": "a1", "content": "hello"}])

    assert await archive.records() == [{"archive_id": "a1", "content": "hello"}]
    assert await archive.get_by_ids(["a1"]) == [{"archive_id": "a1", "content": "hello"}]
    assert await archive.media_records("a1") == [{"archive_id": "a1", "media": True}]


@pytest.mark.asyncio
async def test_rolling_summary_uses_json_store_protocol(tmp_path):
    store = MemoryJsonStore({"summary_text": "旧摘要", "updated_at": "T0"})
    summary = RollingSummaryStore(tmp_path / "unused.json", store=store)

    assert await summary.load() == {
        "summary_text": "旧摘要",
        "archived_until": None,
        "updated_at": "T0",
    }

    await summary.update("新摘要", active_start_index=5, updated_at="T1")

    assert summary.text() == "新摘要"
    assert summary.active_start_index() == 5
    assert store.writes[-1]["archived_until"]["active_start_index"] == 5


@pytest.mark.asyncio
async def test_event_journal_uses_event_store_protocol():
    store = MemoryEventStore()
    journal = EventJournal(store)

    await journal.start()
    ids = await journal.append_events([{"event_type": "sample", "payload": {}}])
    stats = await journal.stats()
    projected = await journal.shutdown(timeout=0.1)

    assert store.started is True
    assert ids == [1]
    assert stats["pending_count"] == 0
    assert projected is True
    assert store.shutdown_timeout == 0.1
    assert store.appended_events == [{"event_type": "sample", "payload": {}}]
