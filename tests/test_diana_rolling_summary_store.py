from __future__ import annotations

import sqlite3
from pathlib import Path

import orjson
import pytest

from memory.diana_db import DianaDB
from memory.diana_stores import DianaRollingSummaryStore


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_empty_load_defaults(tmp_path):
    db_path = tmp_path / "diana.db"
    store = DianaRollingSummaryStore(db_path, "yuexi")

    data = await store.load()

    assert data == {"summary_text": "", "archived_until": None, "updated_at": ""}
    assert store.text() == ""
    assert store.active_start_index() == 0
    assert _rolling_summary_rows(db_path) == []


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_update_roundtrip_and_columns(tmp_path):
    db = DianaDB(tmp_path / "diana.db")
    store = DianaRollingSummaryStore(db, "yuexi")
    archived_until = {
        "history_index": 12,
        "conversation_id": "private:42",
        "details": {"kept": True},
    }

    await store.update(
        "  摘要文本  ",
        archived_until=archived_until,
        updated_at="2026-06-18T12:00:00Z",
    )

    expected = {
        "summary_text": "摘要文本",
        "archived_until": archived_until,
        "updated_at": "2026-06-18T12:00:00Z",
    }
    assert await store.load() == expected
    assert store.text() == "摘要文本"
    assert store.active_start_index() == 0

    row = _rolling_summary_row(db.path, "yuexi")
    assert row["summary_text"] == "摘要文本"
    assert orjson.loads(row["archived_until_json"]) == archived_until
    assert row["active_start_index"] is None
    assert orjson.loads(row["summary_json"]) == expected
    assert row["updated_at"] == "2026-06-18T12:00:00Z"

    reloaded = DianaRollingSummaryStore(db.path, "yuexi")
    assert await reloaded.load() == expected


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_active_start_index_updates_archived_until(tmp_path):
    db_path = tmp_path / "diana.db"
    store = DianaRollingSummaryStore(db_path, "yuexi")

    await store.update(
        "摘要",
        archived_until={"last_compaction_count": 3},
        active_start_index=7,
    )

    data = await store.load()
    assert data["archived_until"] == {
        "last_compaction_count": 3,
        "active_start_index": 7,
    }
    assert store.active_start_index() == 7

    row = _rolling_summary_row(db_path, "yuexi")
    assert row["active_start_index"] == 7
    assert orjson.loads(row["archived_until_json"]) == data["archived_until"]
    assert orjson.loads(row["summary_json"]) == data


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_wraps_non_dict_archived_until_with_active_start(
    tmp_path,
):
    db_path = tmp_path / "diana.db"
    store = DianaRollingSummaryStore(db_path, "yuexi")

    await store.update("摘要", archived_until="legacy-marker", active_start_index=5)

    expected_archived_until = {
        "legacy_archived_until": "legacy-marker",
        "active_start_index": 5,
    }
    assert await store.load() == {
        "summary_text": "摘要",
        "archived_until": expected_archived_until,
        "updated_at": "",
    }
    assert store.active_start_index() == 5

    row = _rolling_summary_row(db_path, "yuexi")
    assert row["active_start_index"] == 5
    assert orjson.loads(row["archived_until_json"]) == expected_archived_until


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_preserves_none_archived_until(tmp_path):
    db_path = tmp_path / "diana.db"
    store = DianaRollingSummaryStore(db_path, "yuexi")

    await store.update("摘要", archived_until=None)

    assert await store.load() == {
        "summary_text": "摘要",
        "archived_until": None,
        "updated_at": "",
    }
    assert orjson.loads(_rolling_summary_row(db_path, "yuexi")["archived_until_json"]) is None


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_persona_isolation(tmp_path):
    db_path = tmp_path / "diana.db"
    yuexi = DianaRollingSummaryStore(db_path, "yuexi")
    jiu = DianaRollingSummaryStore(db_path, "jiu")

    await yuexi.update("月汐摘要", active_start_index=2)
    await jiu.update("玖摘要", active_start_index=9)

    assert (await yuexi.load())["summary_text"] == "月汐摘要"
    assert yuexi.active_start_index() == 2
    assert (await jiu.load())["summary_text"] == "玖摘要"
    assert jiu.active_start_index() == 9
    assert {row["persona_id"] for row in _rolling_summary_rows(db_path)} == {"yuexi", "jiu"}


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_falls_back_when_summary_json_is_broken(
    tmp_path,
    caplog,
):
    db_path = tmp_path / "diana.db"
    db = DianaDB(db_path)
    try:
        db.load()
        db.connect().execute(
            """
            INSERT INTO rolling_summary(
                persona_id, summary_text, archived_until_json,
                active_start_index, summary_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "yuexi",
                "列摘要",
                orjson.dumps({"last_compaction_count": 4}).decode("utf-8"),
                6,
                "{broken",
                "2026-06-18T13:00:00Z",
            ),
        )
        db.connect().commit()
    finally:
        db.close()

    store = DianaRollingSummaryStore(db_path, "yuexi")
    with caplog.at_level("WARNING"):
        data = await store.load()

    assert data == {
        "summary_text": "列摘要",
        "archived_until": {"last_compaction_count": 4, "active_start_index": 6},
        "updated_at": "2026-06-18T13:00:00Z",
    }
    assert store.active_start_index() == 6
    assert "summary_json" in caplog.text


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_merges_empty_summary_json_with_columns(tmp_path):
    db_path = tmp_path / "diana.db"
    _insert_rolling_summary_row(
        db_path,
        persona_id="yuexi",
        summary_text="列摘要",
        archived_until={"last_compaction_count": 4},
        active_start_index=6,
        summary_json={},
        updated_at="2026-06-18T14:00:00Z",
    )

    store = DianaRollingSummaryStore(db_path, "yuexi")

    assert await store.load() == {
        "summary_text": "列摘要",
        "archived_until": {"last_compaction_count": 4, "active_start_index": 6},
        "updated_at": "2026-06-18T14:00:00Z",
    }
    assert store.active_start_index() == 6


@pytest.mark.asyncio
async def test_diana_rolling_summary_store_partial_summary_json_overrides_columns(tmp_path):
    db_path = tmp_path / "diana.db"
    _insert_rolling_summary_row(
        db_path,
        persona_id="yuexi",
        summary_text="列摘要",
        archived_until={"last_compaction_count": 4},
        active_start_index=6,
        summary_json={"summary_text": "json 摘要"},
        updated_at="2026-06-18T15:00:00Z",
    )

    store = DianaRollingSummaryStore(db_path, "yuexi")

    assert await store.load() == {
        "summary_text": "json 摘要",
        "archived_until": {"last_compaction_count": 4, "active_start_index": 6},
        "updated_at": "2026-06-18T15:00:00Z",
    }
    assert store.text() == "json 摘要"
    assert store.active_start_index() == 6


def test_memory_package_exports_diana_rolling_summary_store():
    from memory import DianaRollingSummaryStore as PackageDianaRollingSummaryStore

    assert PackageDianaRollingSummaryStore is DianaRollingSummaryStore


def _rolling_summary_rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM rolling_summary
            ORDER BY persona_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _rolling_summary_row(db_path: Path, persona_id: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT *
            FROM rolling_summary
            WHERE persona_id = ?
            """,
            (persona_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _insert_rolling_summary_row(
    db_path: Path,
    *,
    persona_id: str,
    summary_text: str,
    archived_until: object,
    active_start_index: int | None,
    summary_json: object,
    updated_at: str,
) -> None:
    db = DianaDB(db_path)
    try:
        db.load()
        db.connect().execute(
            """
            INSERT INTO rolling_summary(
                persona_id, summary_text, archived_until_json,
                active_start_index, summary_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                persona_id,
                summary_text,
                orjson.dumps(archived_until).decode("utf-8"),
                active_start_index,
                orjson.dumps(summary_json).decode("utf-8"),
                updated_at,
            ),
        )
        db.connect().commit()
    finally:
        db.close()
