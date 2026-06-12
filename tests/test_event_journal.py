from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import pytest

from memory import EventJournal, EventStore, HistoryManager


async def _wait_thread_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.wait_for(
        asyncio.to_thread(event.wait, timeout),
        timeout=timeout + 0.2,
    )


class BlockingProjectionStore(EventStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.projection_started = threading.Event()
        self.release_projection = threading.Event()

    def _project_events_sync(self, events):  # noqa: ANN001
        self.projection_started.set()
        if not self.release_projection.wait(timeout=2.0):
            raise RuntimeError("projection test timeout")
        return super()._project_events_sync(events)


class FailingProjectionStore(EventStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, projection_retry_delay=0.01, **kwargs)
        self.projection_failed = threading.Event()

    def _project_events_sync(self, events):  # noqa: ANN001
        self.projection_failed.set()
        raise RuntimeError("sqlite projection failed")


class SlowAppendLogStore(EventStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.write_started = threading.Event()

    def _append_log_events_sync(self, events):  # noqa: ANN001
        self.write_started.set()
        time.sleep(0.05)
        return super()._append_log_events_sync(events)


class FailingAppendLogStore(EventStore):
    def _append_log_events_sync(self, events):  # noqa: ANN001
        raise OSError("append log failed")


class RecoverableSqliteFailureStore(EventStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, projection_retry_delay=0.01, **kwargs)
        self.fail_sqlite = True
        self.sqlite_failed = threading.Event()

    def _connect(self, *, ensure_schema: bool = True):  # noqa: ANN201
        if self.fail_sqlite:
            self.sqlite_failed.set()
            raise RuntimeError("sqlite unavailable")
        return super()._connect(ensure_schema=ensure_schema)


@pytest.mark.asyncio
async def test_event_journal_append_ack_does_not_wait_for_sqlite_projection(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3")
    journal = EventJournal(store)
    await journal.start()
    try:
        event_id = await asyncio.wait_for(
            journal.append_event(
                event_type="single",
                payload={"n": 1},
                conversation_id="private:1",
            ),
            timeout=1.0,
        )

        assert event_id == 1
        await _wait_thread_event(store.projection_started)
        assert await journal.get_event(event_id) is None

        store.release_projection.set()
        assert await journal.wait_projected(event_id, timeout=1.0)
        assert (await journal.get_event(event_id))["payload"] == {"n": 1}
    finally:
        store.release_projection.set()
        await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_sqlite_initialization_failure_does_not_block_append(tmp_path):
    store = RecoverableSqliteFailureStore(tmp_path / "events.sqlite3")
    journal = EventJournal(store)
    await journal.start()

    event_id = await journal.append_event(
        event_type="sqlite-fail",
        payload={"ok": True},
        idempotency_key="journal:sqlite-fail",
    )
    await _wait_thread_event(store.sqlite_failed)

    assert event_id == 1
    assert len(store.append_log_path.read_text(encoding="utf-8").splitlines()) == 1
    stats = await journal.stats()
    assert stats["last_appended_event_id"] == 1
    assert stats["last_projected_event_id"] == 0
    assert stats["projection_error_count"] >= 1

    store.fail_sqlite = False
    assert await journal.wait_projected(event_id, timeout=1.0)
    assert (await journal.get_event(event_id))["payload"] == {"ok": True}
    await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_writes_in_append_order_after_projection(tmp_path):
    journal = EventJournal(EventStore(tmp_path / "events.sqlite3"))
    await journal.start()
    try:
        first_id = await journal.append_event(
            event_type="single",
            payload={"n": 1},
            conversation_id="private:1",
        )
        batch_ids = await journal.append_events(
            [
                {"event_type": "batch", "payload": {"n": 2}},
                {"event_type": "batch", "payload": {"n": 3}},
            ]
        )

        assert [first_id, *batch_ids] == [1, 2, 3]
        assert await journal.wait_projected(batch_ids[-1], timeout=1.0)
        events = await journal.iter_events(limit=10)
        assert [event["event_id"] for event in events] == [1, 2, 3]
        assert [event["payload"]["n"] for event in events] == [1, 2, 3]
    finally:
        await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_concurrent_appends_keep_all_events(tmp_path):
    journal = EventJournal(EventStore(tmp_path / "events.sqlite3"))
    await journal.start()
    try:
        ids = await asyncio.gather(
            *(
                journal.append_event(event_type="concurrent", payload={"index": index})
                for index in range(50)
            )
        )

        assert sorted(ids) == list(range(1, 51))
        assert await journal.wait_projected(max(ids), timeout=1.0)
        events = await journal.events_by_type("concurrent", limit=100)
        assert [event["event_id"] for event in events] == list(range(1, 51))
        assert {event["payload"]["index"] for event in events} == set(range(50))
    finally:
        await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_read_api_uses_projected_snapshot_without_drain(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3")
    journal = EventJournal(store)
    await journal.start()
    try:
        event_id = await journal.append_event(event_type="type-a", payload={"name": "a1"})
        await _wait_thread_event(store.projection_started)

        assert await journal.get_event(event_id) is None
        assert await journal.iter_events(limit=10) == []
        assert await journal.events_by_type("type-a") == []

        store.release_projection.set()
        assert await journal.wait_projected(event_id, timeout=1.0)
        assert (await journal.get_event(event_id))["payload"]["name"] == "a1"
    finally:
        store.release_projection.set()
        await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_read_api_proxies_after_wait_projected(tmp_path):
    journal = EventJournal(EventStore(tmp_path / "events.sqlite3"))
    await journal.start()
    try:
        ids = await journal.append_events(
            [
                {
                    "event_type": "type-a",
                    "conversation_id": "group:1",
                    "payload": {"name": "a1"},
                },
                {
                    "event_type": "type-b",
                    "conversation_id": "group:1",
                    "payload": {"name": "b1"},
                },
                {
                    "event_type": "type-a",
                    "conversation_id": "private:1",
                    "payload": {"name": "a2"},
                },
            ]
        )
        assert await journal.wait_projected(ids[-1], timeout=1.0)

        assert (await journal.get_event(2))["payload"]["name"] == "b1"
        batch = await journal.get_events([3, 1, 999, 2])
        assert [event["payload"]["name"] if event else None for event in batch] == [
            "a2",
            "a1",
            None,
            "b1",
        ]
        assert [event["payload"]["name"] for event in await journal.events_by_type("type-a")] == [
            "a1",
            "a2",
        ]
        assert [
            event["payload"]["name"]
            for event in await journal.events_for_conversation("group:1")
        ] == ["a1", "b1"]
        assert [
            event["payload"]["name"]
            for event in await journal.iter_events(limit=2, order="desc")
        ] == ["a2", "b1"]
    finally:
        await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_debug_metrics_do_not_log_payload(tmp_path, caplog):
    journal = EventJournal(EventStore(tmp_path / "events.sqlite3"))
    await journal.start()
    try:
        with caplog.at_level(logging.DEBUG, logger="memory.event_journal"):
            event_id = await journal.append_event(
                event_type="debug-metric",
                payload={"content": "journal-secret-body"},
                conversation_id="private:1",
            )
            await journal.wait_projected(event_id, timeout=1.0)

        log_text = caplog.text
        assert "EventJournal append_events 指标" in log_text
        assert "event_count=1" in log_text
        assert "append_log_ms=" in log_text
        assert "pending_count=" in log_text
        assert "projection_error_count=" in log_text
        assert "EventJournal drain 指标" in log_text
        assert "journal-secret-body" not in log_text
    finally:
        await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_shutdown_flushes_submitted_append_log_write(tmp_path):
    store = SlowAppendLogStore(tmp_path / "events.sqlite3")
    journal = EventJournal(store)
    await journal.start()

    append_task = asyncio.create_task(
        journal.append_event(event_type="shutdown", payload={"ok": True})
    )
    await _wait_thread_event(store.write_started)
    assert await journal.shutdown(timeout=1.0)

    assert await append_task == 1
    assert (await journal.get_event(1))["payload"] == {"ok": True}


@pytest.mark.asyncio
async def test_event_journal_append_log_failure_returns_to_append_caller(tmp_path):
    journal = EventJournal(FailingAppendLogStore(tmp_path / "events.sqlite3"))
    await journal.start()
    try:
        with pytest.raises(OSError, match="append log failed"):
            await journal.append_event(event_type="broken", payload={})
    finally:
        await journal.shutdown()


@pytest.mark.asyncio
async def test_event_journal_projection_failure_does_not_fail_append(tmp_path):
    store = FailingProjectionStore(tmp_path / "events.sqlite3")
    journal = EventJournal(store)
    await journal.start()

    event_id = await journal.append_event(event_type="projection-broken", payload={"ok": True})
    await _wait_thread_event(store.projection_failed)

    assert event_id == 1
    assert await journal.get_event(event_id) is None
    stats = await journal.stats()
    assert stats["last_appended_event_id"] == 1
    assert stats["last_projected_event_id"] == 0
    assert stats["projection_error_count"] >= 1
    assert not await journal.shutdown(timeout=0.01)


@pytest.mark.asyncio
async def test_history_manager_with_event_journal_keeps_records_shape_and_light_mirror(tmp_path):
    journal = EventJournal(EventStore(tmp_path / "events.sqlite3"))
    await journal.start()
    try:
        history = HistoryManager(tmp_path / "history.jsonl", event_store=journal)
        large_content = "大" * 10000
        records = [
            {
                "role": "user",
                "content": large_content,
                "metadata": {"scope": "private", "target_id": "123"},
                "conversation_id": "private:123",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function"}],
                "conversation_id": "private:123",
            },
        ]

        await history.add_records(records)

        assert await history.records() == records
        raw_jsonl = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
        raw_records = [
            json.loads(line)
            for line in raw_jsonl.splitlines()
            if line.strip()
        ]
        assert raw_records == records
        assert large_content in raw_jsonl
        assert "content" in raw_jsonl
        assert "metadata" in raw_jsonl
        assert "tool_calls" in raw_jsonl
        assert await journal.wait_projected(timeout=1.0)
        events = await journal.events_by_type("history_record_appended", limit=10)
        assert [event["event_id"] for event in events] == [1, 2]
        assert [event["payload"]["role"] for event in events] == ["user", "assistant"]
        assert events[0]["payload"]["content_length"] == len(large_content)
        assert "record" not in events[0]["payload"]
        assert "record" not in events[1]["payload"]
        assert "content" not in events[0]["payload"]
        assert events[0]["payload"]["record_keys"] == [
            "role",
            "content",
            "metadata",
            "conversation_id",
        ]
        assert events[1]["payload"]["record_keys"] == [
            "role",
            "content",
            "tool_calls",
            "conversation_id",
        ]
    finally:
        await journal.shutdown()
