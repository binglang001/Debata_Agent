from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from typing import Any

import pytest

from memory import EventStore


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
        self.fail_projection = True
        self.projection_failed = threading.Event()

    def _project_events_sync(self, events):  # noqa: ANN001
        if self.fail_projection:
            self.projection_failed.set()
            raise RuntimeError("sqlite projection failed")
        return super()._project_events_sync(events)


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
async def test_append_ack_does_not_wait_for_slow_sqlite_projection(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3")

    event_id = await asyncio.wait_for(
        store.append_event(event_type="message", payload={"text": "已落 append log"}),
        timeout=1.0,
    )

    assert event_id == 1
    await _wait_thread_event(store.projection_started)
    assert await store.get_event(event_id) is None

    store.release_projection.set()
    assert await store.wait_projected(event_id, timeout=1.0)
    assert (await store.get_event(event_id))["payload"] == {"text": "已落 append log"}
    await store.shutdown()


@pytest.mark.asyncio
async def test_sqlite_initialization_failure_does_not_block_append_log(tmp_path):
    store = RecoverableSqliteFailureStore(tmp_path / "events.sqlite3")

    event_id = await store.append_event(
        event_type="message",
        payload={"text": "sqlite 初始化失败也要落 append log"},
        idempotency_key="sqlite:init-fail",
    )
    await _wait_thread_event(store.sqlite_failed)

    assert event_id == 1
    records = [
        json.loads(line)
        for line in store.append_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["event_id"] == 1
    assert records[0]["idempotency_key"] == "sqlite:init-fail"
    stats = await store.stats()
    assert stats["last_appended_event_id"] == 1
    assert stats["last_projected_event_id"] == 0
    assert stats["projection_error_count"] >= 1
    assert "sqlite unavailable" in stats["last_projection_error"]

    store.fail_sqlite = False
    assert await store.wait_projected(event_id, timeout=1.0)
    assert (await store.get_event(event_id))["payload"] == {
        "text": "sqlite 初始化失败也要落 append log"
    }
    await store.shutdown()


@pytest.mark.asyncio
async def test_event_store_append_events_preserves_input_order(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")

    ids = await store.append_events(
        [
            {"event_type": "message", "payload": {"text": "第一条"}},
            {"event_type": "tool", "payload": {"name": "send"}},
            {"event_type": "message", "payload": {"text": "第三条"}},
        ]
    )

    assert ids == [1, 2, 3]
    assert await store.wait_projected(ids[-1], timeout=1.0)
    events = await store.iter_events(limit=10)
    assert [event["event_id"] for event in events] == [1, 2, 3]

    batch = await store.get_events([3, 1, 999, 2, 1])
    assert [event["event_id"] if event else None for event in batch] == [
        3,
        1,
        None,
        2,
        1,
    ]
    await store.shutdown()


@pytest.mark.asyncio
async def test_unprojected_idempotency_key_returns_existing_event_id(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3")

    first = await store.append_event(
        event_type="message",
        payload={"text": "原始事件"},
        idempotency_key="message:1",
    )
    await _wait_thread_event(store.projection_started)
    second = await store.append_event(
        event_type="message",
        payload={"text": "重复事件不应写入 append log"},
        idempotency_key="message:1",
    )

    assert second == first
    assert len(store.append_log_path.read_text(encoding="utf-8").splitlines()) == 1
    assert await store.iter_events(limit=10) == []

    store.release_projection.set()
    assert await store.wait_projected(first, timeout=1.0)
    events = await store.iter_events(limit=10)
    assert len(events) == 1
    assert events[0]["payload"] == {"text": "原始事件"}
    await store.shutdown()


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_bypasses_full_backpressure(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3", max_pending_events=1)

    first = await store.append_event(
        event_type="message",
        payload={"text": "原始事件"},
        idempotency_key="message:blocked-dupe",
    )
    await _wait_thread_event(store.projection_started)

    second = await asyncio.wait_for(
        store.append_event(
            event_type="message",
            payload={"text": "重复事件"},
            idempotency_key="message:blocked-dupe",
        ),
        timeout=0.2,
    )

    assert second == first
    assert len(store.append_log_path.read_text(encoding="utf-8").splitlines()) == 1
    assert (await store.stats())["pending_count"] == 1

    store.release_projection.set()
    assert await store.wait_projected(first, timeout=1.0)
    await store.shutdown()


@pytest.mark.asyncio
async def test_idempotency_index_rebuilds_from_append_log_before_projection(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = FailingProjectionStore(path)

    first = await store.append_event(
        event_type="message",
        payload={"text": "未投影事件"},
        idempotency_key="message:replay",
    )
    await _wait_thread_event(store.projection_failed)
    assert await store.get_event(first) is None
    assert not await store.shutdown(timeout=0.01)

    reloaded = EventStore(path, projection_retry_delay=0.01)
    duplicate = await reloaded.append_event(
        event_type="message",
        payload={"text": "重启后的重复事件"},
        idempotency_key="message:replay",
    )

    assert duplicate == first
    assert len(reloaded.append_log_path.read_text(encoding="utf-8").splitlines()) == 1
    assert await reloaded.wait_projected(first, timeout=1.0)
    events = await reloaded.iter_events(limit=10)
    assert len(events) == 1
    assert events[0]["payload"] == {"text": "未投影事件"}
    await reloaded.shutdown()


@pytest.mark.asyncio
async def test_projection_failure_does_not_fail_append_and_retries(tmp_path):
    store = FailingProjectionStore(tmp_path / "events.sqlite3")

    event_id = await store.append_event(event_type="message", payload={"text": "先失败"})
    await _wait_thread_event(store.projection_failed)

    assert event_id == 1
    assert await store.get_event(event_id) is None
    stats = await store.stats()
    assert stats["last_appended_event_id"] == 1
    assert stats["last_projected_event_id"] == 0
    assert stats["projection_error_count"] >= 1

    store.fail_projection = False
    assert await store.wait_projected(event_id, timeout=1.0)
    assert (await store.get_event(event_id))["payload"] == {"text": "先失败"}
    await store.shutdown()


@pytest.mark.asyncio
async def test_reads_do_not_implicitly_drain_projection(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3")

    event_id = await store.append_event(event_type="message", payload={"text": "慢投影"})
    await _wait_thread_event(store.projection_started)

    assert await store.get_event(event_id) is None
    assert await store.iter_events(limit=10) == []
    assert await store.events_by_type("message", limit=10) == []

    store.release_projection.set()
    assert await store.wait_projected(event_id, timeout=1.0)
    assert [event["event_id"] for event in await store.events_by_type("message")] == [event_id]
    await store.shutdown()


@pytest.mark.asyncio
async def test_shutdown_flushes_append_log_and_waits_projection(tmp_path):
    class SlowProjectionStore(EventStore):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.projection_started = threading.Event()

        def _project_events_sync(self, events):  # noqa: ANN001
            self.projection_started.set()
            time.sleep(0.05)
            return super()._project_events_sync(events)

    store = SlowProjectionStore(tmp_path / "events.sqlite3")
    event_id = await store.append_event(event_type="shutdown", payload={"ok": True})
    await _wait_thread_event(store.projection_started)

    assert await store.shutdown(timeout=1.0)
    assert (await store.get_event(event_id))["payload"] == {"ok": True}


@pytest.mark.asyncio
async def test_append_log_failure_is_returned_to_append_caller(tmp_path):
    store = FailingAppendLogStore(tmp_path / "events.sqlite3")

    with pytest.raises(OSError, match="append log failed"):
        await store.append_event(event_type="broken", payload={})

    stats = await store.stats()
    assert stats["last_appended_event_id"] == 0
    assert await store.iter_events(limit=10) == []
    await store.shutdown()


@pytest.mark.asyncio
async def test_backpressure_blocks_new_appends_when_projection_lag_reaches_limit(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3", max_pending_events=1)

    first = await store.append_event(event_type="message", payload={"n": 1})
    await _wait_thread_event(store.projection_started)
    second_task = asyncio.create_task(
        store.append_event(event_type="message", payload={"n": 2})
    )
    await asyncio.sleep(0.05)

    assert first == 1
    assert not second_task.done()
    assert (await store.stats())["pending_count"] == 1

    store.release_projection.set()
    assert await asyncio.wait_for(second_task, timeout=1.0) == 2
    assert await store.wait_projected(2, timeout=1.0)
    assert [event["payload"]["n"] for event in await store.iter_events(limit=10)] == [1, 2]
    await store.shutdown()


@pytest.mark.asyncio
async def test_append_events_counts_new_batch_size_for_backpressure(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3", max_pending_events=2)

    first = await store.append_event(event_type="message", payload={"n": 1})
    await _wait_thread_event(store.projection_started)
    batch_task = asyncio.create_task(
        store.append_events(
            [
                {"event_type": "message", "payload": {"n": 2}},
                {"event_type": "message", "payload": {"n": 3}},
            ]
        )
    )
    await asyncio.sleep(0.05)

    assert first == 1
    assert not batch_task.done()
    assert (await store.stats())["pending_count"] == 1

    store.release_projection.set()
    assert await asyncio.wait_for(batch_task, timeout=1.0) == [2, 3]
    assert await store.wait_projected(3, timeout=1.0)
    assert [event["payload"]["n"] for event in await store.iter_events(limit=10)] == [
        1,
        2,
        3,
    ]
    await store.shutdown()


@pytest.mark.asyncio
async def test_large_append_events_are_segmented_by_projection_capacity(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3", max_pending_events=2)

    batch_task = asyncio.create_task(
        store.append_events(
            [
                {"event_type": "message", "payload": {"n": index}}
                for index in range(5)
            ]
        )
    )
    await _wait_thread_event(store.projection_started)
    await asyncio.sleep(0.05)

    assert not batch_task.done()
    stats = await store.stats()
    assert stats["last_appended_event_id"] == 2
    assert stats["pending_count"] == 2
    assert len(store.append_log_path.read_text(encoding="utf-8").splitlines()) == 2

    store.release_projection.set()
    assert await asyncio.wait_for(batch_task, timeout=1.0) == [1, 2, 3, 4, 5]
    assert await store.wait_projected(5, timeout=1.0)
    assert [event["payload"]["n"] for event in await store.iter_events(limit=10)] == [
        0,
        1,
        2,
        3,
        4,
    ]
    await store.shutdown()


@pytest.mark.asyncio
async def test_non_positive_max_pending_events_uses_single_event_capacity(tmp_path):
    store = BlockingProjectionStore(tmp_path / "events.sqlite3", max_pending_events=0)

    first = await store.append_event(event_type="message", payload={"n": 1})
    await _wait_thread_event(store.projection_started)
    second_task = asyncio.create_task(
        store.append_event(event_type="message", payload={"n": 2})
    )
    await asyncio.sleep(0.05)

    assert first == 1
    assert not second_task.done()
    stats = await store.stats()
    assert stats["last_appended_event_id"] == 1
    assert stats["pending_count"] == 1

    store.release_projection.set()
    assert await asyncio.wait_for(second_task, timeout=1.0) == 2
    assert await store.wait_projected(2, timeout=1.0)
    await store.shutdown()


@pytest.mark.asyncio
async def test_event_store_payload_roundtrip_and_stable_hash(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    payload = {
        "text": "你好",
        "items": [{"kind": "image", "path": "incoming/a.jpg"}],
        "nested": {"b": 2, "a": 1},
    }

    event_id = await store.append_event(
        event_type="message",
        payload=payload,
        conversation_id="private:1",
        source="napcat",
    )
    assert await store.wait_projected(event_id, timeout=1.0)
    event = await store.get_event(event_id)

    assert event is not None
    assert event["payload"] == payload
    assert event["payload_json"].startswith('{"text":')
    assert len(event["payload_hash"]) == 64
    assert event["schema_version"] == 1
    assert event["source"] == "napcat"

    same_payload_different_order = await store.append_event(
        event_type="message",
        payload={"nested": {"a": 1, "b": 2}, "items": payload["items"], "text": "你好"},
    )
    assert await store.wait_projected(same_payload_different_order, timeout=1.0)
    assert (await store.get_event(same_payload_different_order))["payload_hash"] == event[
        "payload_hash"
    ]
    await store.shutdown()


@pytest.mark.asyncio
async def test_event_store_conversation_and_type_pages(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    ids = await store.append_events(
        [
            {
                "event_type": "message",
                "conversation_id": "group:1",
                "payload": {"text": "g1-a"},
            },
            {
                "event_type": "message",
                "conversation_id": "group:2",
                "payload": {"text": "g2-a"},
            },
            {
                "event_type": "message",
                "conversation_id": "group:1",
                "payload": {"text": "g1-b"},
            },
            {
                "event_type": "tool",
                "conversation_id": "group:1",
                "payload": {"text": "g1-c"},
            },
            {
                "event_type": "message",
                "conversation_id": "group:2",
                "payload": {"text": "g2-b"},
            },
        ]
    )
    assert await store.wait_projected(ids[-1], timeout=1.0)

    latest = await store.events_for_conversation("group:1", limit=2)
    assert [event["event_id"] for event in latest] == [ids[2], ids[3]]
    assert [event["payload"]["text"] for event in latest] == ["g1-b", "g1-c"]

    previous = await store.events_for_conversation(
        "group:1",
        limit=2,
        before_event_id=latest[0]["event_id"],
    )
    assert [event["event_id"] for event in previous] == [ids[0]]

    page = await store.iter_events(limit=2, after_event_id=2)
    assert [event["payload"]["text"] for event in page] == ["g1-b", "g1-c"]

    message_page = await store.events_by_type("message", limit=2, after_event_id=1)
    assert [event["event_id"] for event in message_page] == [ids[1], ids[2]]
    await store.shutdown()


@pytest.mark.asyncio
async def test_projected_sqlite_keeps_single_row_for_idempotency_key(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")

    first = await store.append_event(
        event_type="message",
        payload={"text": "原始事件"},
        idempotency_key="message:1",
    )
    second = await store.append_event(
        event_type="message",
        payload={"text": "重复事件不应覆盖"},
        idempotency_key="message:1",
    )

    assert second == first
    assert await store.wait_projected(first, timeout=1.0)
    events = await store.iter_events(limit=10)
    assert len(events) == 1
    assert events[0]["payload"] == {"text": "原始事件"}

    with sqlite3.connect(store.path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
    assert count == 1
    await store.shutdown()
