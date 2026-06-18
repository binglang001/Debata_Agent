from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import orjson

from memory.diana_db import DIANA_DB_SCHEMA_VERSION, DianaDB
from memory.diana_importers import (
    LegacyImportDomainResult,
    import_legacy_memory_files,
)
from memory.diana_stores import (
    DianaHistoryStore,
    DianaImportantStore,
    DianaRollingSummaryStore,
    DianaUsageStatsStore,
)


def test_import_legacy_memory_files_full_sample_roundtrip(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "db" / "diana.db"
    history_records = [
        {
            "role": "user",
            "content": "你好",
            "conversation_id": "private:42",
            "metadata": {"source": "sample"},
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "收到"}],
            "timestamp": "2026-06-18T12:00:00Z",
        },
    ]
    important_items = [
        {
            "id": "mem_a",
            "timestamp": "2026-06-18 10:00:00",
            "content": "喜欢直接说明结论",
            "scope": "global",
            "pinned": True,
            "extra": {"keep": ["nested", 1]},
        }
    ]
    rolling_summary = {
        "summary_text": "已完成第一轮压缩",
        "archived_until": {"history_index": 3},
        "updated_at": "2026-06-18T13:00:00Z",
        "active_start_index": 4,
    }
    usage_records = [
        {
            "ts": 1_782_000_000.5,
            "provider": "deepseek_main",
            "model": "deepseek-chat",
            "agent": "主模型",
            "operation": "agent_loop",
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "reasoning_tokens": 1,
            "cached_tokens": 5,
            "cache_creation_tokens": 3,
            "total_tokens": 12,
            "kv_message_count": 7,
        },
        {
            "ts": 1_782_000_010.0,
            "provider": "deepseek_fast",
            "model": "deepseek-chat",
            "agent": "总结",
            "operation": "summarize",
            "prompt_tokens": 20,
            "completion_tokens": 4,
            "total_tokens": 24,
        },
    ]
    _write_jsonl(source_dir / "history.jsonl", history_records)
    _write_json(source_dir / "important.json", important_items)
    _write_json(source_dir / "rolling_summary.json", rolling_summary)
    _write_jsonl(source_dir / "model_usage.jsonl", usage_records)

    result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.backup_path is None
    assert result.history == LegacyImportDomainResult(imported=2, skipped=0)
    assert result.important == LegacyImportDomainResult(imported=1, skipped=0)
    assert result.rolling_summary == LegacyImportDomainResult(imported=1, skipped=0)
    assert result.usage == LegacyImportDomainResult(imported=2, skipped=0)
    assert _load_history(db_path, "yuexi") == history_records
    assert _read_important(db_path, "yuexi") == important_items
    assert _load_rolling_summary(db_path, "yuexi") == {
        "summary_text": "已完成第一轮压缩",
        "archived_until": {"history_index": 3, "active_start_index": 4},
        "updated_at": "2026-06-18T13:00:00Z",
    }

    history_rows = _history_rows(db_path, "yuexi")
    important_rows = _important_rows(db_path, "yuexi")
    rolling_row = _rolling_summary_row(db_path, "yuexi")
    assert [orjson.loads(row["record_json"]) for row in history_rows] == history_records
    assert [orjson.loads(row["item_json"]) for row in important_rows] == important_items
    assert orjson.loads(rolling_row["summary_json"]) == rolling_summary
    assert rolling_row["summary_text"] == "已完成第一轮压缩"
    assert orjson.loads(rolling_row["archived_until_json"]) == {"history_index": 3}
    assert rolling_row["active_start_index"] == 4
    assert rolling_row["updated_at"] == "2026-06-18T13:00:00Z"

    rows = _usage_rows(db_path)
    assert [row["persona_id"] for row in rows] == ["yuexi", "yuexi"]
    assert [orjson.loads(row["record_json"]) for row in rows] == usage_records
    assert rows[0]["ts"] == usage_records[0]["ts"]
    assert rows[0]["provider"] == "deepseek_main"
    assert rows[0]["prompt_tokens"] == 10
    assert rows[0]["cached_tokens"] == 5

    usage_store = DianaUsageStatsStore(db_path, "yuexi")
    asyncio.run(usage_store.load())
    assert usage_store.count == 2
    assert usage_store.summarize("all").total_tokens == 36


def test_import_legacy_memory_files_is_idempotent(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    history_records = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]
    important_items = [{"id": "mem_a", "content": "keep"}]
    usage_records = [
        {
            "ts": 100.0,
            "agent": "主模型",
            "operation": "agent_loop",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
        }
    ]
    _write_jsonl(source_dir / "history.jsonl", history_records)
    _write_json(source_dir / "important.json", important_items)
    _write_json(source_dir / "rolling_summary.json", {"summary_text": "摘要"})
    _write_jsonl(source_dir / "model_usage.jsonl", usage_records)

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    second = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert first.usage == LegacyImportDomainResult(imported=1, skipped=0)
    assert second.usage == LegacyImportDomainResult(imported=0, skipped=1)
    assert _load_history(db_path, "yuexi") == history_records
    assert _read_important(db_path, "yuexi") == important_items
    assert _row_count(db_path, "history_records", "yuexi") == 2
    assert _row_count(db_path, "important_memories", "yuexi") == 1
    assert len(_usage_rows(db_path)) == 1


def test_import_legacy_memory_files_preserves_rolling_summary_raw_json(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    rolling_summary = {
        "summary_text": "  带额外字段的摘要  ",
        "archived_until": {"history_index": 9, "marker": {"nested": True}},
        "updated_at": "2026-06-18T18:00:00Z",
        "active_start_index": 10,
        "extra_field": {"kept": ["原样", 1, True]},
    }
    _write_json(source_dir / "rolling_summary.json", rolling_summary)

    result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.rolling_summary == LegacyImportDomainResult(imported=1, skipped=0)
    row = _rolling_summary_row(db_path, "yuexi")
    assert row["summary_text"] == "带额外字段的摘要"
    assert orjson.loads(row["archived_until_json"]) == {
        "history_index": 9,
        "marker": {"nested": True},
    }
    assert row["active_start_index"] == 10
    assert row["updated_at"] == "2026-06-18T18:00:00Z"
    assert orjson.loads(row["summary_json"]) == rolling_summary


def test_import_legacy_memory_files_backup_existing_database_only(tmp_path):
    source_dir = tmp_path / "legacy_missing_target"
    source_dir.mkdir()
    missing_db_path = tmp_path / "missing" / "diana.db"

    missing_result = import_legacy_memory_files(missing_db_path, source_dir, "yuexi")

    assert missing_result.backup_path is None
    assert not (missing_db_path.parent / "backups").exists()

    existing_source = tmp_path / "legacy_existing_target"
    existing_db_path = tmp_path / "existing" / "diana.db"
    _write_jsonl(existing_source / "history.jsonl", [{"role": "assistant", "content": "new"}])
    db = DianaDB(existing_db_path)
    try:
        db.load()
        db.connect().execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash, content_length, record_json
            )
            VALUES ('yuexi', 0, 'user', 'old-hash', 3, '{"role":"user","content":"old"}')
            """
        )
        db.connect().commit()
    finally:
        db.close()

    existing_result = import_legacy_memory_files(existing_db_path, existing_source, "yuexi")

    assert existing_result.backup_path is not None
    assert existing_result.backup_path.exists()
    with sqlite3.connect(existing_result.backup_path) as backup_conn:
        assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == DIANA_DB_SCHEMA_VERSION
        backup_record = backup_conn.execute(
            "SELECT record_json FROM history_records WHERE persona_id = 'yuexi'"
        ).fetchone()[0]
    assert orjson.loads(backup_record) == {"role": "user", "content": "old"}
    assert _load_history(existing_db_path, "yuexi") == [{"role": "assistant", "content": "new"}]


def test_import_legacy_memory_files_skips_missing_and_damaged_files(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    source_dir.mkdir()
    db_path = tmp_path / "diana.db"
    (source_dir / "history.jsonl").write_bytes(
        b'\n{"role":"user","content":"ok"}\n{broken\n[]\n'
    )
    (source_dir / "important.json").write_bytes(b'{"broken"')
    (source_dir / "rolling_summary.json").write_bytes(b'["not", "object"]')

    with caplog.at_level("WARNING"):
        result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.history == LegacyImportDomainResult(imported=1, skipped=3)
    assert result.important == LegacyImportDomainResult(imported=0, skipped=1)
    assert result.rolling_summary == LegacyImportDomainResult(imported=0, skipped=1)
    assert result.usage == LegacyImportDomainResult(imported=0, skipped=0)
    assert _load_history(db_path, "yuexi") == [{"role": "user", "content": "ok"}]
    assert _read_important(db_path, "yuexi") == []
    assert _load_rolling_summary(db_path, "yuexi") == {
        "summary_text": "",
        "archived_until": None,
        "updated_at": "",
    }
    assert "跳过空的旧 JSONL 行" in caplog.text
    assert "跳过损坏的旧 JSONL 行" in caplog.text
    assert "跳过损坏的旧 JSON 文件" in caplog.text


def test_import_legacy_memory_files_keeps_personas_isolated(tmp_path):
    db_path = tmp_path / "diana.db"
    first_source = tmp_path / "legacy_yuexi"
    second_source = tmp_path / "legacy_jiu"
    shared_usage = {
        "ts": 200.0,
        "agent": "共享调用",
        "prompt_tokens": 5,
        "completion_tokens": 1,
        "total_tokens": 6,
    }
    _write_jsonl(first_source / "history.jsonl", [{"role": "user", "content": "月汐"}])
    _write_json(first_source / "important.json", [{"id": "mem_y", "content": "月汐记忆"}])
    _write_json(first_source / "rolling_summary.json", {"summary_text": "月汐摘要"})
    _write_jsonl(first_source / "model_usage.jsonl", [shared_usage])
    _write_jsonl(second_source / "history.jsonl", [{"role": "assistant", "content": "玖"}])
    _write_json(second_source / "important.json", [{"id": "mem_j", "content": "玖记忆"}])
    _write_json(second_source / "rolling_summary.json", {"summary_text": "玖摘要"})
    _write_jsonl(second_source / "model_usage.jsonl", [shared_usage])

    import_legacy_memory_files(db_path, first_source, "yuexi")
    import_legacy_memory_files(db_path, second_source, "jiu")

    assert _load_history(db_path, "yuexi") == [{"role": "user", "content": "月汐"}]
    assert _load_history(db_path, "jiu") == [{"role": "assistant", "content": "玖"}]
    assert _read_important(db_path, "yuexi") == [{"id": "mem_y", "content": "月汐记忆"}]
    assert _read_important(db_path, "jiu") == [{"id": "mem_j", "content": "玖记忆"}]
    assert _load_rolling_summary(db_path, "yuexi")["summary_text"] == "月汐摘要"
    assert _load_rolling_summary(db_path, "jiu")["summary_text"] == "玖摘要"
    assert [(row["persona_id"], orjson.loads(row["record_json"])) for row in _usage_rows(db_path)] == [
        ("yuexi", shared_usage),
        ("jiu", shared_usage),
    ]


def test_memory_package_exports_legacy_importer():
    from memory import import_legacy_memory_files as package_importer

    assert package_importer is import_legacy_memory_files


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(orjson.dumps(record) + b"\n" for record in records))


def _load_history(db_path: Path, persona_id: str) -> list[dict]:
    return asyncio.run(DianaHistoryStore(db_path, persona_id).load(force_reload=True))


def _read_important(db_path: Path, persona_id: str) -> object:
    return asyncio.run(DianaImportantStore(db_path, persona_id).read(default=[]))


def _load_rolling_summary(db_path: Path, persona_id: str) -> dict:
    return asyncio.run(DianaRollingSummaryStore(db_path, persona_id).load())


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


def _row_count(db_path: Path, table: str, persona_id: str) -> int:
    if table == "history_records":
        sql = "SELECT COUNT(*) FROM history_records WHERE persona_id = ?"
    elif table == "important_memories":
        sql = "SELECT COUNT(*) FROM important_memories WHERE persona_id = ?"
    else:
        raise ValueError(f"unsupported table: {table}")
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                sql,
                (persona_id,),
            ).fetchone()[0]
        )
