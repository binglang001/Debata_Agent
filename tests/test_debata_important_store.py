from __future__ import annotations

import sqlite3
from pathlib import Path

import orjson
import pytest

from memory.debata_db import DebataDB
from memory.debata_stores import DebataImportantStore


@pytest.mark.asyncio
async def test_debata_important_store_list_roundtrip_and_extracted_columns(tmp_path):
    db = DebataDB(tmp_path / "debata.db")
    store = DebataImportantStore(db, "yuexi")
    items = [
        {
            "id": "mem_a",
            "timestamp": "2026-06-18 10:00:00",
            "content": "喜欢被直接说明结论",
            "scope": "global",
            "pinned": False,
            "updated_at": "2026-06-18T10:00:00Z",
            "meta": {"source": "test", "scores": [1, 2, 3]},
        },
        {
            "id": "mem_b",
            "timestamp": "2026-06-18 11:00:00",
            "content": "群 42 内优先使用简洁回复",
            "scope": "group:42",
            "pinned": True,
            "updated_at": "2026-06-18T11:00:00Z",
        },
    ]

    assert await store.read(default=[]) == []

    await store.write(items)

    assert await store.read(default=[]) == items
    rows = _important_rows(db.path, "yuexi")
    assert [row["memory_id"] for row in rows] == ["mem_a", "mem_b"]
    assert [row["timestamp"] for row in rows] == [
        "2026-06-18 10:00:00",
        "2026-06-18 11:00:00",
    ]
    assert [row["scope"] for row in rows] == ["global", "group:42"]
    assert [row["pinned"] for row in rows] == [0, 1]
    assert [row["content"] for row in rows] == [
        "喜欢被直接说明结论",
        "群 42 内优先使用简洁回复",
    ]
    assert [row["updated_at"] for row in rows] == [
        "2026-06-18T10:00:00Z",
        "2026-06-18T11:00:00Z",
    ]
    assert [orjson.loads(row["item_json"]) for row in rows] == items

    reloaded = DebataImportantStore(db.path, "yuexi")
    assert await reloaded.read(default=[]) == items


@pytest.mark.asyncio
async def test_debata_important_store_replaces_current_persona_only(tmp_path):
    db_path = tmp_path / "debata.db"
    yuexi = DebataImportantStore(db_path, "yuexi")
    jiu = DebataImportantStore(db_path, "jiu")
    original = [
        {"id": "mem_1", "timestamp": "t1", "content": "one", "scope": "global"},
        {"id": "mem_2", "timestamp": "t2", "content": "two", "scope": "user:1"},
    ]
    replacement = [
        {"id": "mem_3", "timestamp": "t3", "content": "three", "scope": "group:2"}
    ]
    other = [{"id": "other_1", "timestamp": "o1", "content": "other"}]

    await yuexi.write(original)
    await jiu.write(other)
    await yuexi.write(replacement)

    assert await yuexi.read(default=[]) == replacement
    assert await jiu.read(default=[]) == other
    assert [row["memory_id"] for row in _important_rows(db_path, "yuexi")] == ["mem_3"]
    assert [row["memory_id"] for row in _important_rows(db_path, "jiu")] == ["other_1"]


@pytest.mark.asyncio
async def test_debata_important_store_missing_id_fallback_is_stable(tmp_path):
    db_path = tmp_path / "debata.db"
    store = DebataImportantStore(db_path, "yuexi")
    item = {
        "timestamp": "2026-06-18 12:00:00",
        "content": "没有 id 的旧重要记忆",
        "scope": "global",
        "pinned": False,
    }

    await store.write([item])
    first_row = _important_rows(db_path, "yuexi")[0]
    await store.write([item])
    second_row = _important_rows(db_path, "yuexi")[0]

    assert first_row["memory_id"].startswith("fallback:")
    assert second_row["memory_id"] == first_row["memory_id"]
    assert orjson.loads(second_row["item_json"]) == item
    assert "id" not in orjson.loads(second_row["item_json"])
    assert await store.read(default=[]) == [item]


def test_memory_package_exports_debata_important_store():
    from memory import DebataImportantStore as PackageDebataImportantStore

    assert PackageDebataImportantStore is DebataImportantStore


def _important_rows(db_path: Path, persona_id: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM important_memories
            WHERE persona_id = ?
            ORDER BY id ASC
            """,
            (persona_id,),
        ).fetchall()
    return [dict(row) for row in rows]
