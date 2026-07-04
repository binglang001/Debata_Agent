from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from memory import EventJournal
from memory.debata_db import DebataDB
from memory.debata_stores import DebataEventStore


@pytest.mark.asyncio
async def test_debata_event_store_roundtrips_events_and_payload_metadata(tmp_path):
    store = DebataEventStore(tmp_path / "debata.db", "yuexi")
    payload = {
        "text": "你好",
        "items": [{"kind": "image", "path": "incoming/a.jpg"}],
        "nested": {"b": 2, "a": 1},
    }

    first_id = await store.append_event(
        event_type="message",
        event_uuid="uuid-1",
        conversation_id="private:1",
        session_id="session-1",
        turn_id=7,
        source="napcat",
        external_id="msg-1",
        tool_call_id="tool-1",
        payload=payload,
        timestamp_unix=100.5,
        created_at_unix=101.5,
        schema_version=3,
    )
    batch_ids = await store.append_events(
        [
            {
                "event_type": "tool",
                "event_uuid": "uuid-2",
                "conversation_id": "private:1",
                "payload": {"tool": "send"},
            },
            {
                "event_type": "message",
                "event_uuid": "uuid-3",
                "conversation_id": "group:1",
                "payload": {"text": "第三条"},
            },
        ]
    )

    assert [first_id, *batch_ids] == [1, 2, 3]
    assert await store.wait_projected(3, timeout=0.01)

    event = await store.get_event(first_id)
    assert event is not None
    assert event["event_id"] == 1
    assert event["event_type"] == "message"
    assert event["event_uuid"] == "uuid-1"
    assert event["conversation_id"] == "private:1"
    assert event["session_id"] == "session-1"
    assert event["turn_id"] == "7"
    assert event["source"] == "napcat"
    assert event["external_id"] == "msg-1"
    assert event["tool_call_id"] == "tool-1"
    assert event["timestamp_unix"] == 100.5
    assert event["created_at_unix"] == 101.5
    assert event["payload"] == payload
    assert event["payload_json"].startswith('{"text":')
    assert len(event["payload_hash"]) == 64
    assert event["schema_version"] == 3

    same_payload_different_order = await store.append_event(
        event_type="message",
        payload={"nested": {"a": 1, "b": 2}, "items": payload["items"], "text": "你好"},
    )
    same_hash = await store.get_event(same_payload_different_order)
    assert same_hash is not None
    assert same_hash["payload_hash"] == event["payload_hash"]

    batch = await store.get_events([3, 1, 999, 2, 1, 0])
    assert [item["event_id"] if item else None for item in batch] == [
        3,
        1,
        None,
        2,
        1,
        None,
    ]
    assert [item["event_id"] for item in await store.iter_events(limit=10)] == [1, 2, 3, 4]

    state = _projection_state_rows(store.db.path)
    assert state == [{"persona_id": "yuexi", "name": "last_projected_event_id", "value": "4"}]


@pytest.mark.asyncio
async def test_debata_event_store_idempotency_deduplicates_same_batch_and_reload(tmp_path):
    db_path = tmp_path / "debata.db"
    store = DebataEventStore(db_path, "yuexi")

    ids = await store.append_events(
        [
            {
                "event_type": "message",
                "payload": {"text": "原始"},
                "idempotency_key": "msg:1",
            },
            {
                "event_type": "message",
                "payload": {"text": "同批重复"},
                "idempotency_key": "msg:1",
            },
            {
                "event_type": "message",
                "payload": {"text": "新事件"},
                "idempotency_key": "msg:2",
            },
        ]
    )

    assert ids == [1, 1, 2]
    assert _event_count(db_path, "yuexi") == 2
    assert [event["payload"]["text"] for event in await store.iter_events(limit=10)] == [
        "原始",
        "新事件",
    ]

    reloaded = DebataEventStore(db_path, "yuexi")
    duplicate = await reloaded.append_event(
        event_type="message",
        payload={"text": "重载重复"},
        idempotency_key="msg:1",
    )

    assert duplicate == 1
    assert _event_count(db_path, "yuexi") == 2
    assert [event["payload"]["text"] for event in await reloaded.iter_events(limit=10)] == [
        "原始",
        "新事件",
    ]


@pytest.mark.asyncio
async def test_debata_event_store_persona_scopes_ids_and_idempotency_keys(tmp_path):
    db_path = tmp_path / "debata.db"
    yuexi = DebataEventStore(db_path, " yuexi ")
    jiu = DebataEventStore(db_path, "jiu")

    yuexi_ids = await yuexi.append_events(
        [
            {
                "event_type": "message",
                "payload": {"persona": "yuexi", "n": 1},
                "idempotency_key": "shared",
            },
            {
                "event_type": "message",
                "payload": {"persona": "yuexi", "n": 2},
            },
        ]
    )
    jiu_id = await jiu.append_event(
        event_type="message",
        payload={"persona": "jiu", "n": 1},
        idempotency_key="shared",
    )
    yuexi_duplicate = await yuexi.append_event(
        event_type="message",
        payload={"persona": "yuexi", "n": "duplicate"},
        idempotency_key="shared",
    )
    jiu_duplicate = await jiu.append_event(
        event_type="message",
        payload={"persona": "jiu", "n": "duplicate"},
        idempotency_key="shared",
    )

    assert yuexi_ids == [1, 2]
    assert jiu_id == 1
    assert yuexi_duplicate == 1
    assert jiu_duplicate == 1
    assert [event["payload"]["persona"] for event in await yuexi.iter_events(limit=10)] == [
        "yuexi",
        "yuexi",
    ]
    assert [event["payload"]["persona"] for event in await jiu.iter_events(limit=10)] == ["jiu"]
    assert _event_count(db_path, "yuexi") == 2
    assert _event_count(db_path, "jiu") == 1


@pytest.mark.asyncio
async def test_debata_event_store_conversation_and_type_pages(tmp_path):
    store = DebataEventStore(tmp_path / "debata.db", "yuexi")
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

    latest = await store.events_for_conversation("group:1", limit=2)
    assert [event["event_id"] for event in latest] == [ids[2], ids[3]]
    assert [event["payload"]["text"] for event in latest] == ["g1-b", "g1-c"]

    previous = await store.events_for_conversation(
        "group:1",
        limit=2,
        before_event_id=latest[0]["event_id"],
    )
    assert [event["event_id"] for event in previous] == [ids[0]]

    assert await store.events_for_conversation("  ", limit=2) == []

    page = await store.iter_events(limit=2, after_event_id=2)
    assert [event["payload"]["text"] for event in page] == ["g1-b", "g1-c"]

    desc_page = await store.iter_events(limit=2, before_event_id=5, order="desc")
    assert [event["event_id"] for event in desc_page] == [4, 3]

    message_page = await store.events_by_type("message", limit=2, after_event_id=1)
    assert [event["event_id"] for event in message_page] == [ids[1], ids[2]]

    message_desc = await store.events_by_type("message", limit=2, before_event_id=5, order="desc")
    assert [event["event_id"] for event in message_desc] == [3, 2]


@pytest.mark.asyncio
async def test_event_journal_can_wrap_debata_event_store(tmp_path):
    store = DebataEventStore(tmp_path / "debata.db", "yuexi")
    journal = EventJournal(store)

    await journal.start()
    try:
        event_id = await journal.append_event(
            event_type="journal",
            conversation_id="private:1",
            payload={"ok": True},
        )

        assert event_id == 1
        assert await journal.wait_projected(event_id, timeout=0.01)
        assert (await journal.get_event(event_id))["payload"] == {"ok": True}
        assert [
            event["payload"]["ok"]
            for event in await journal.events_for_conversation("private:1")
        ] == [True]
    finally:
        assert await journal.shutdown(timeout=0.01)


@pytest.mark.asyncio
async def test_debata_event_store_wait_projected_waits_for_future_event_id(tmp_path):
    store = DebataEventStore(tmp_path / "debata.db", "yuexi")

    async def append_later() -> int:
        await asyncio.sleep(0.03)
        await store.append_event(event_type="message", payload={"n": 1})
        return await store.append_event(event_type="message", payload={"n": 2})

    append_task = asyncio.create_task(append_later())
    started_at = time.perf_counter()
    projected = await store.wait_projected(2, timeout=1.0)
    elapsed = time.perf_counter() - started_at

    assert projected is True
    assert elapsed >= 0.02
    assert await append_task == 2
    assert (await store.get_event(2))["payload"] == {"n": 2}


@pytest.mark.asyncio
async def test_debata_event_store_wait_projected_times_out_for_missing_future_event_id(tmp_path):
    store = DebataEventStore(tmp_path / "debata.db", "yuexi")
    await store.append_event(event_type="message", payload={"n": 1})

    started_at = time.perf_counter()
    projected = await store.wait_projected(2, timeout=0.05)
    elapsed = time.perf_counter() - started_at

    assert projected is False
    assert elapsed >= 0.04
    assert await store.wait_projected(0, timeout=0.01)


@pytest.mark.asyncio
async def test_debata_event_store_rejects_appends_after_shutdown_but_reads_existing_events(tmp_path):
    store = DebataEventStore(tmp_path / "debata.db", "yuexi")
    event_id = await store.append_event(event_type="message", payload={"n": 1})

    assert await store.shutdown(timeout=0.01)

    with pytest.raises(RuntimeError, match="DebataEventStore is closed"):
        await store.append_event(event_type="message", payload={"n": 2})

    assert (await store.get_event(event_id))["payload"] == {"n": 1}
    assert [event["event_id"] for event in await store.iter_events(limit=10)] == [event_id]


@pytest.mark.asyncio
async def test_debata_event_store_concurrent_same_persona_instances_keep_unique_ids(tmp_path):
    db_path = tmp_path / "debata.db"
    stores = [DebataEventStore(db_path, "yuexi") for _ in range(4)]

    ids = await asyncio.gather(
        *(
            store.append_event(event_type="concurrent", payload={"index": index})
            for index, store in enumerate(stores)
        )
    )

    assert sorted(ids) == [1, 2, 3, 4]
    events = await DebataEventStore(db_path, "yuexi").events_by_type("concurrent", limit=10)
    assert [event["event_id"] for event in events] == [1, 2, 3, 4]
    assert {event["payload"]["index"] for event in events} == {0, 1, 2, 3}


@pytest.mark.asyncio
async def test_debata_event_store_stats_and_close_state(tmp_path):
    store = DebataEventStore(tmp_path / "debata.db", "yuexi")

    initial = await store.stats()
    assert initial == {
        "last_appended_event_id": 0,
        "last_projected_event_id": 0,
        "projection_lag": 0,
        "pending_count": 0,
        "projection_error_count": 0,
        "last_projection_error": None,
        "last_projection_error_event_id": None,
        "projection_running": False,
        "closed": False,
    }

    event_id = await store.append_event(event_type="message", payload={"n": 1})
    stats = await store.stats()
    assert event_id == 1
    assert stats["last_appended_event_id"] == 1
    assert stats["last_projected_event_id"] == 1
    assert stats["projection_lag"] == 0
    assert stats["pending_count"] == 0
    assert stats["projection_error_count"] == 0
    assert stats["last_projection_error"] is None
    assert stats["last_projection_error_event_id"] is None
    assert stats["projection_running"] is False
    assert stats["closed"] is False

    assert await store.shutdown(timeout=0.01)
    closed_stats = await store.stats()
    assert closed_stats["closed"] is True

    other = DebataEventStore(store.db.path, "jiu")
    assert await other.close(timeout=0.01)
    assert (await other.stats())["closed"] is True


def test_debata_event_store_rejects_empty_persona_id(tmp_path):
    with pytest.raises(ValueError, match="persona_id must not be empty"):
        DebataEventStore(tmp_path / "debata.db", "  ")


def test_memory_package_exports_debata_event_store():
    from memory import DebataEventStore as PackageDebataEventStore

    assert PackageDebataEventStore is DebataEventStore


def _event_count(db_path: Path, persona_id: str) -> int:
    db = DebataDB(db_path)
    try:
        db.load()
        return int(
            db.connect()
            .execute(
                "SELECT COUNT(*) FROM event_log WHERE persona_id = ?",
                (persona_id,),
            )
            .fetchone()[0]
        )
    finally:
        db.close()


def _projection_state_rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT persona_id, name, value
            FROM event_projection_state
            ORDER BY persona_id, name
            """
        ).fetchall()
    return [dict(row) for row in rows]
