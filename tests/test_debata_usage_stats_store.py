from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import orjson
import pytest

from memory.debata_db import DebataDB
from memory.debata_stores import DebataUsageStatsStore
from providers.base import Usage


@pytest.mark.asyncio
async def test_debata_usage_stats_store_records_and_summarizes_ranges(tmp_path, monkeypatch):
    db_path = tmp_path / "debata.db"
    now = time.mktime((2026, 6, 4, 12, 0, 0, 3, 155, -1))
    current_time = now - 2 * 86400
    monkeypatch.setattr(time, "time", lambda: current_time)

    store = DebataUsageStatsStore(db_path, "yuexi")
    await store.load()
    await store.record(
        Usage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="older_call",
    )

    current_time = now
    await store.record(
        Usage(
            prompt_tokens=100,
            completion_tokens=20,
            reasoning_tokens=5,
            cached_tokens=80,
            cache_creation_tokens=10,
            total_tokens=120,
        ),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="agent_loop",
    )

    today = store.summarize("today")
    assert today.request_count == 1
    assert today.prompt_tokens == 100
    assert today.completion_tokens == 20
    assert today.reasoning_tokens == 5
    assert today.cached_tokens == 80
    assert today.cache_creation_tokens == 10
    assert today.total_tokens == 120
    assert today.cache_hit_rate == 0.8

    all_time = store.summarize("all")
    assert all_time.request_count == 2
    assert all_time.prompt_tokens == 150
    assert all_time.completion_tokens == 30
    assert all_time.total_tokens == 180

    reloaded = DebataUsageStatsStore(db_path, "yuexi")
    await reloaded.load()
    assert reloaded.count == 2
    assert reloaded.summarize("all") == all_time


@pytest.mark.asyncio
async def test_debata_usage_stats_store_zero_tokens_do_not_write(tmp_path):
    db_path = tmp_path / "debata.db"
    store = DebataUsageStatsStore(db_path, "yuexi")
    await store.load()

    await store.record(
        Usage(),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="agent_loop",
        extra={"kv_message_count": 3},
    )

    assert store.count == 0
    assert _usage_rows(db_path) == []


@pytest.mark.asyncio
async def test_debata_usage_stats_store_preserves_extra_and_extracted_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "debata.db"
    now = time.mktime((2026, 6, 4, 12, 0, 0, 3, 155, -1))
    monkeypatch.setattr(time, "time", lambda: now)
    store = DebataUsageStatsStore(db_path, "  yuexi  ")

    await store.record(
        Usage(prompt_tokens=10, completion_tokens=1, total_tokens=0),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="agent_loop",
        extra={"kv_message_count": 3, "kv_prefix_8k_hash": "abc123"},
    )

    rows = _usage_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["persona_id"] == "yuexi"
    assert row["ts"] == now
    assert row["provider"] == "deepseek_main"
    assert row["model"] == "deepseek-chat"
    assert row["agent"] == "主模型"
    assert row["operation"] == "agent_loop"
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 1
    assert row["reasoning_tokens"] == 0
    assert row["cached_tokens"] == 0
    assert row["cache_creation_tokens"] == 0
    assert row["total_tokens"] == 11
    assert orjson.loads(row["record_json"]) == {
        "ts": now,
        "provider": "deepseek_main",
        "model": "deepseek-chat",
        "agent": "主模型",
        "operation": "agent_loop",
        "prompt_tokens": 10,
        "completion_tokens": 1,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 11,
        "kv_message_count": 3,
        "kv_prefix_8k_hash": "abc123",
    }


@pytest.mark.asyncio
async def test_debata_usage_stats_store_persona_scope_loads_all_or_one(tmp_path):
    db_path = tmp_path / "debata.db"
    yuexi = DebataUsageStatsStore(db_path, "yuexi")
    jiu = DebataUsageStatsStore(db_path, "jiu")
    unscoped = DebataUsageStatsStore(db_path, "")

    await yuexi.record(Usage(prompt_tokens=1), agent="yuexi")
    await jiu.record(Usage(prompt_tokens=2), agent="jiu")
    await unscoped.record(Usage(prompt_tokens=3), agent="global")

    all_records = DebataUsageStatsStore(db_path)
    await all_records.load()
    assert all_records.count == 3
    assert all_records.summarize("all").prompt_tokens == 6

    yuexi_only = DebataUsageStatsStore(db_path, "yuexi")
    await yuexi_only.load()
    assert yuexi_only.count == 1
    assert yuexi_only.summarize("all").prompt_tokens == 1

    rows = _usage_rows(db_path)
    assert [row["persona_id"] for row in rows] == ["yuexi", "jiu", None]


@pytest.mark.asyncio
async def test_debata_usage_stats_store_skips_broken_record_json_on_load(tmp_path, caplog):
    db_path = tmp_path / "debata.db"
    store = DebataUsageStatsStore(db_path, "yuexi")
    await store.record(Usage(prompt_tokens=10), agent="ok")
    _insert_usage_record_json(db_path, persona_id="yuexi", record_json="{broken")
    _insert_usage_record_json(db_path, persona_id="yuexi", record_json="[]")

    reloaded = DebataUsageStatsStore(db_path, "yuexi")
    with caplog.at_level("WARNING"):
        await reloaded.load()

    assert reloaded.count == 1
    assert reloaded.summarize("all").prompt_tokens == 10
    assert "usage record_json" in caplog.text


def test_memory_package_exports_debata_usage_stats_store():
    from memory import DebataUsageStatsStore as PackageDebataUsageStatsStore

    assert PackageDebataUsageStatsStore is DebataUsageStatsStore


def test_memory_import_exports_debata_usage_stats_store_without_core_cycle():
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import memory; from memory import DebataUsageStatsStore",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _usage_rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM usage_records
            ORDER BY id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _insert_usage_record_json(
    db_path: Path,
    *,
    persona_id: str | None,
    record_json: str,
) -> None:
    db = DebataDB(db_path)
    try:
        db.load()
        db.connect().execute(
            """
            INSERT INTO usage_records(
                persona_id, ts, provider, model, agent, operation, record_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (persona_id, 0, "", "", "", "", record_json),
        )
        db.connect().commit()
    finally:
        db.close()
