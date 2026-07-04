from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import orjson
import pytest

from memory.debata_db import DebataDB
from memory.debata_stores import DebataHistoryStore


@pytest.mark.asyncio
async def test_debata_history_store_jsonl_equivalent_operations(tmp_path):
    db_path = tmp_path / "debata.db"
    store = DebataHistoryStore(db_path, "yuexi")
    records = [
        {"role": "user", "content": "one", "conversation_id": "private:1"},
        {"role": "assistant", "content": "two", "conversation_id": "group:2"},
        {"role": "tool", "content": {"ok": True}, "tool_call_id": "tc-1"},
    ]

    await store.append(records[0])
    await store.append_many(records[1:])

    assert await store.length() == 3
    assert await store.load() == records
    assert await store.get_slice(1) == records[1:]
    assert await store.get_slice(0, 2) == records[:2]

    reloaded = DebataHistoryStore(db_path, "yuexi")
    assert await reloaded.load(force_reload=True) == records

    remaining = await store.truncate_head(1)
    assert remaining == 2
    assert await store.load() == records[1:]
    assert [row["history_index"] for row in _history_rows(db_path, "yuexi")] == [0, 1]

    replacement = [{"role": "system", "content": "reset"}]
    await store.replace_all(replacement)
    assert await store.length() == 1
    assert await store.load() == replacement
    assert _history_rows(db_path, "yuexi")[0]["history_index"] == 0

    await store.clear()
    assert await store.length() == 0
    assert await store.load() == []
    assert _history_rows(db_path, "yuexi") == []


@pytest.mark.asyncio
async def test_debata_history_store_persona_isolation_and_global_stream(tmp_path):
    db_path = tmp_path / "debata.db"
    yuexi = DebataHistoryStore(db_path, "yuexi")
    jiu = DebataHistoryStore(db_path, "jiu")
    first = {"role": "user", "content": "private", "conversation_id": "private:1"}
    second = {"role": "assistant", "content": "group", "conversation_id": "group:2"}
    other = {"role": "user", "content": "other", "conversation_id": "private:1"}

    await yuexi.append(first)
    await jiu.append(other)
    await yuexi.append(second)

    assert await yuexi.load() == [first, second]
    assert await jiu.load() == [other]
    assert [
        (row["history_index"], row["conversation_id"])
        for row in _history_rows(db_path, "yuexi")
    ] == [(0, "private:1"), (1, "group:2")]
    assert [row["history_index"] for row in _history_rows(db_path, "jiu")] == [0]


@pytest.mark.asyncio
async def test_debata_history_store_preserves_record_json_and_extracted_columns(tmp_path):
    db = DebataDB(tmp_path / "debata.db")
    store = DebataHistoryStore(db, "yuexi")
    content = [{"type": "text", "text": "你好"}, {"type": "image", "url": "x.png"}]
    record = {
        "role": "assistant",
        "content": content,
        "conversation_id": "private:42",
        "timestamp": "2026-06-18T12:00:00Z",
        "metadata": {"nested": {"keep": True}},
        "tool_calls": [{"id": "tc-1", "type": "function"}],
    }

    await store.append(record)

    row = _history_rows(db.path, "yuexi")[0]
    expected_content = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    assert row["conversation_id"] == "private:42"
    assert row["role"] == "assistant"
    assert row["timestamp"] == "2026-06-18T12:00:00Z"
    assert row["content_length"] == len(expected_content)
    assert row["content_hash"] == hashlib.sha256(expected_content.encode("utf-8")).hexdigest()
    assert orjson.loads(row["record_json"]) == record


@pytest.mark.asyncio
async def test_debata_history_store_infers_conversation_id_from_legacy_metadata(tmp_path):
    db_path = tmp_path / "debata.db"
    store = DebataHistoryStore(db_path, "yuexi")
    record = {
        "role": "user",
        "content": "legacy",
        "metadata": {"messages": [{"scope": "group", "target_id": "10001"}]},
    }

    await store.append(record)

    assert _history_rows(db_path, "yuexi")[0]["conversation_id"] == "group:10001"
    assert await store.load() == [record]


@pytest.mark.asyncio
async def test_debata_history_store_skips_broken_records_on_load(tmp_path):
    db_path = tmp_path / "debata.db"
    store = DebataHistoryStore(db_path, "yuexi")
    await store.append({"role": "user", "content": "ok"})
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash, content_length, record_json
            )
            VALUES ('yuexi', 1, 'user', 'broken', 6, '{broken')
            """
        )
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash, content_length, record_json
            )
            VALUES ('yuexi', 2, 'user', 'list', 2, '[]')
            """
        )

    reloaded = DebataHistoryStore(db_path, "yuexi")

    assert await reloaded.load(force_reload=True) == [{"role": "user", "content": "ok"}]


@pytest.mark.asyncio
async def test_debata_history_store_concurrent_append_many_keeps_contiguous_indexes(tmp_path):
    db_path = tmp_path / "debata.db"
    store = DebataHistoryStore(db_path, "yuexi")

    await asyncio.gather(
        store.append_many([{"role": "user", "content": f"a-{index}"} for index in range(5)]),
        store.append_many([{"role": "assistant", "content": f"b-{index}"} for index in range(5)]),
    )

    rows = _history_rows(db_path, "yuexi")
    assert [row["history_index"] for row in rows] == list(range(10))
    assert await store.length() == 10


def test_memory_package_exports_debata_history_store():
    from memory import DebataHistoryStore as PackageDebataHistoryStore

    assert PackageDebataHistoryStore is DebataHistoryStore


def _history_rows(db_path: Path, persona_id: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM history_records
            WHERE persona_id = ?
            ORDER BY history_index ASC
            """,
            (persona_id,),
        ).fetchall()
    return [dict(row) for row in rows]
