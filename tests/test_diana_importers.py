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
    DianaArchiveStore,
    DianaEventStore,
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
    assert result.events == LegacyImportDomainResult(imported=0, skipped=0)
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
    assert result.events == LegacyImportDomainResult(imported=0, skipped=0)
    assert _load_history(db_path, "yuexi") == [{"role": "user", "content": "ok"}]
    assert _read_important(db_path, "yuexi") == []
    assert _load_rolling_summary(db_path, "yuexi") == {
        "summary_text": "",
        "archived_until": None,
        "updated_at": "",
    }
    assert _projection_state_value(db_path, "yuexi") is None
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


def test_import_legacy_archive_preserves_messages_media_and_store_reads(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    first = _legacy_archive_message(
        3,
        archive_id="old-3",
        content="第一条",
        content_search="第一条 keyword",
        timestamp_unix=100,
    )
    second = _legacy_archive_message(
        8,
        archive_id="old-8",
        content="第二条 [图片 workspace=img/a.jpg name=a.jpg]",
        content_search="第二条 keyword [图片 workspace=img/a.jpg name=a.jpg]",
        timestamp_unix=110,
    )
    media = _legacy_archive_media(12, archive_id="old-8", workspace_path="img/a.jpg")
    _write_legacy_archive_sqlite(source_dir / "archive.sqlite3", [first, second], [media])

    result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.archive == LegacyImportDomainResult(imported=3, skipped=0)
    assert _archive_message_rows(db_path, "yuexi") == [
        {"persona_id": "yuexi", **first},
        {"persona_id": "yuexi", **second},
    ]
    assert _archive_media_rows(db_path, "yuexi") == [
        {"persona_id": "yuexi", **media},
    ]

    store = DianaArchiveStore(db_path, "yuexi")
    assert [record["archive_id"] for record in asyncio.run(store.records())] == [
        "old-3",
        "old-8",
    ]
    assert [item["id"] for item in asyncio.run(store.media_records())] == [12]
    assert [item["archive_id"] for item in asyncio.run(store.media_records("old-8"))] == [
        "old-8",
    ]
    assert [record["content"] for record in asyncio.run(store.search(keyword="keyword"))] == [
        "第一条",
        "第二条 [图片 workspace=img/a.jpg name=a.jpg]",
    ]
    filtered = asyncio.run(
        store.filter_records(
            {
                "conversation_ids": ["private:1"],
                "keywords": ["keyword"],
                "limit": 10,
                "order": "asc",
            }
        )
    )
    assert [item["id"] for item in filtered["results"]] == ["old-3", "old-8"]
    assert [record["archive_id"] for record in asyncio.run(store.get_by_ids(["old-8"]))] == [
        "old-8",
    ]
    assert [
        record["archive_id"]
        for record in asyncio.run(store.context_around("old-8", before=1, after=1))
    ] == ["old-3", "old-8"]
    assert [record["archive_id"] for record in asyncio.run(store.rag_records())] == [
        "old-3",
        "old-8",
    ]


def test_import_legacy_archive_is_idempotent(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    message = _legacy_archive_message(1, archive_id="same-1", content="重复归档")
    media = _legacy_archive_media(1, archive_id="same-1", workspace_path="same.jpg")
    _write_legacy_archive_sqlite(source_dir / "archive.sqlite3", [message], [media])

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    second = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert first.archive == LegacyImportDomainResult(imported=2, skipped=0)
    assert second.archive == LegacyImportDomainResult(imported=0, skipped=2)
    assert _archive_message_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **message}]
    assert _archive_media_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **media}]


def test_import_legacy_archive_message_conflict_warns_and_keeps_existing(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    conflict_source = tmp_path / "legacy_conflict"
    db_path = tmp_path / "diana.db"
    original = _legacy_archive_message(1, archive_id="old-1", content="原始")
    rowid_conflict = _legacy_archive_message(1, archive_id="other-1", content="冲突 rowid")
    archive_id_conflict = _legacy_archive_message(2, archive_id="old-1", content="冲突 id")
    _write_legacy_archive_sqlite(source_dir / "archive.sqlite3", [original], [])
    _write_legacy_archive_sqlite(
        conflict_source / "archive.sqlite3",
        [rowid_conflict, archive_id_conflict],
        [],
    )

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    with caplog.at_level("WARNING"):
        second = import_legacy_memory_files(db_path, conflict_source, "yuexi")

    assert first.archive == LegacyImportDomainResult(imported=1, skipped=0)
    assert second.archive == LegacyImportDomainResult(imported=0, skipped=2)
    assert _archive_message_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **original}]
    assert "跳过冲突的旧归档消息 rowid=1 archive_id=other-1" in caplog.text
    assert "跳过冲突的旧归档消息 rowid=2 archive_id=old-1" in caplog.text


def test_import_legacy_archive_media_orphan_and_conflict_warn(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    conflict_source = tmp_path / "legacy_conflict"
    db_path = tmp_path / "diana.db"
    message = _legacy_archive_message(1, archive_id="old-1", content="有媒体")
    original_media = _legacy_archive_media(1, archive_id="old-1", workspace_path="keep.jpg")
    orphan_media = _legacy_archive_media(2, archive_id="missing", workspace_path="orphan.jpg")
    media_conflict = _legacy_archive_media(1, archive_id="old-1", workspace_path="changed.jpg")
    _write_legacy_archive_sqlite(source_dir / "archive.sqlite3", [message], [original_media])
    _write_legacy_archive_sqlite(
        conflict_source / "archive.sqlite3",
        [message],
        [orphan_media, media_conflict],
    )

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    with caplog.at_level("WARNING"):
        second = import_legacy_memory_files(db_path, conflict_source, "yuexi")

    assert first.archive == LegacyImportDomainResult(imported=2, skipped=0)
    assert second.archive == LegacyImportDomainResult(imported=0, skipped=3)
    assert _archive_media_rows(db_path, "yuexi") == [
        {"persona_id": "yuexi", **original_media},
    ]
    assert "跳过孤儿旧归档媒体 id=2 archive_id=missing" in caplog.text
    assert "跳过冲突的旧归档媒体 id=1 archive_id=old-1" in caplog.text


def test_import_legacy_archive_without_record_json_uses_store_fallback(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    message = _legacy_archive_message(4, archive_id="no-record", content="无原始 JSON")
    _write_legacy_archive_sqlite(
        source_dir / "archive.sqlite3",
        [message],
        [],
        include_record_json=False,
    )

    result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.archive == LegacyImportDomainResult(imported=1, skipped=0)
    row = _archive_message_rows(db_path, "yuexi")[0]
    assert row["record_json"] is None
    loaded = asyncio.run(DianaArchiveStore(db_path, "yuexi").records())
    assert loaded == [
        {
            "role": "user",
            "content": "无原始 JSON",
            "conversation_id": "private:1",
            "metadata": {"source": "legacy-test"},
            "archive_id": "no-record",
        }
    ]


def test_import_legacy_archive_missing_required_message_column_skips_media(
    tmp_path,
    caplog,
):
    source_dir = tmp_path / "legacy"
    broken_source = tmp_path / "broken"
    db_path = tmp_path / "diana.db"
    existing_message = _legacy_archive_message(1, archive_id="shared", content="已有归档")
    existing_media = _legacy_archive_media(
        1,
        archive_id="shared",
        workspace_path="existing.jpg",
    )
    broken_message = _legacy_archive_message(2, archive_id="shared", content="坏库归档")
    broken_media = _legacy_archive_media(
        2,
        archive_id="shared",
        workspace_path="wrongly-attached.jpg",
    )
    _write_legacy_archive_sqlite(
        source_dir / "archive.sqlite3",
        [existing_message],
        [existing_media],
    )
    _write_legacy_archive_sqlite_missing_message_column(
        broken_source / "archive.sqlite3",
        [broken_message],
        [broken_media],
    )

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    with caplog.at_level("WARNING"):
        second = import_legacy_memory_files(db_path, broken_source, "yuexi")

    assert first.archive == LegacyImportDomainResult(imported=2, skipped=0)
    assert second.archive == LegacyImportDomainResult(imported=0, skipped=1)
    assert _archive_message_rows(db_path, "yuexi") == [
        {"persona_id": "yuexi", **existing_message},
    ]
    assert _archive_media_rows(db_path, "yuexi") == [
        {"persona_id": "yuexi", **existing_media},
    ]
    assert "archive_messages 缺少必要列，跳过归档导入" in caplog.text
    assert "missing=content" in caplog.text


def test_import_legacy_archive_skips_bad_rows_without_aborting(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    valid = _legacy_archive_message(1, archive_id="valid", content="有效行")
    bad_message = _legacy_archive_message(2, archive_id=" ", content="缺 archive_id")
    bad_media = _legacy_archive_media(1, archive_id=" ", workspace_path="bad.jpg")
    _write_legacy_archive_sqlite(
        source_dir / "archive.sqlite3",
        [valid, bad_message],
        [bad_media],
    )

    with caplog.at_level("WARNING"):
        result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.archive == LegacyImportDomainResult(imported=1, skipped=2)
    assert _archive_message_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **valid}]
    assert _archive_media_rows(db_path, "yuexi") == []
    assert "跳过缺少关键字段的旧归档消息行" in caplog.text
    assert "跳过缺少关键字段的旧归档媒体行" in caplog.text


def test_import_legacy_archive_missing_file_and_tables_do_not_crash(tmp_path, caplog):
    source_dir = tmp_path / "missing"
    source_dir.mkdir()
    db_path = tmp_path / "diana.db"

    missing_result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert missing_result.archive == LegacyImportDomainResult(imported=0, skipped=0)

    no_messages_dir = tmp_path / "no_messages"
    sqlite_path = no_messages_dir / "archive.sqlite3"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")

    with caplog.at_level("WARNING"):
        no_messages_result = import_legacy_memory_files(db_path, no_messages_dir, "yuexi")

    assert no_messages_result.archive == LegacyImportDomainResult(imported=0, skipped=0)
    assert "旧 archive.sqlite3 缺少 archive_messages 表" in caplog.text

    no_media_dir = tmp_path / "no_media"
    message = _legacy_archive_message(1, archive_id="message-only", content="只有消息")
    _write_legacy_archive_sqlite(
        no_media_dir / "archive.sqlite3",
        [message],
        [],
        include_media_table=False,
    )

    with caplog.at_level("WARNING"):
        no_media_result = import_legacy_memory_files(db_path, no_media_dir, "yuexi")

    assert no_media_result.archive == LegacyImportDomainResult(imported=1, skipped=0)
    assert "旧 archive.sqlite3 缺少 archive_message_media 表" in caplog.text


def test_import_legacy_archive_keeps_personas_isolated(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    message = _legacy_archive_message(1, archive_id="shared", content="共享旧归档")
    media = _legacy_archive_media(1, archive_id="shared", workspace_path="shared.jpg")
    _write_legacy_archive_sqlite(source_dir / "archive.sqlite3", [message], [media])

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    second = import_legacy_memory_files(db_path, source_dir, "jiu")

    assert first.archive == LegacyImportDomainResult(imported=2, skipped=0)
    assert second.archive == LegacyImportDomainResult(imported=2, skipped=0)
    assert _archive_message_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **message}]
    assert _archive_message_rows(db_path, "jiu") == [{"persona_id": "jiu", **message}]
    assert _archive_media_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **media}]
    assert _archive_media_rows(db_path, "jiu") == [{"persona_id": "jiu", **media}]
    assert asyncio.run(DianaArchiveStore(db_path, "yuexi").records())[0]["archive_id"] == "shared"
    assert asyncio.run(DianaArchiveStore(db_path, "jiu").records())[0]["archive_id"] == "shared"


def test_import_legacy_events_sqlite_preserves_fields_and_store_reads(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    payload = {"text": "旧事件", "nested": {"a": 1}}
    event = _legacy_event(
        7,
        payload=payload,
        payload_hash="legacy-hash-7",
        schema_version=4,
        idempotency_key="legacy:7",
    )
    _write_legacy_event_sqlite(source_dir / "events.sqlite3", [event])

    result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.events == LegacyImportDomainResult(imported=1, skipped=0)
    row = _event_rows(db_path, "yuexi")[0]
    for key, value in event.items():
        assert row[key] == value

    store = DianaEventStore(db_path, "yuexi")
    loaded = asyncio.run(store.get_event(7))
    assert loaded is not None
    assert loaded["payload"] == payload
    assert loaded["payload_json"] == event["payload_json"]
    assert loaded["payload_hash"] == "legacy-hash-7"
    assert loaded["schema_version"] == 4
    assert [item["event_id"] for item in asyncio.run(store.iter_events(limit=10))] == [7]
    assert asyncio.run(store.wait_projected(7, timeout=0.01))
    stats = asyncio.run(store.stats())
    assert stats["last_appended_event_id"] == 7
    assert stats["last_projected_event_id"] == 7
    assert _projection_state_value(db_path, "yuexi") == "7"


def test_import_legacy_events_append_log_preserves_event_id_and_projection(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    event = _legacy_event(9, payload={"append": True}, idempotency_key="append:9")
    _write_jsonl(source_dir / "events.sqlite3.append.jsonl", [event])

    result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.events == LegacyImportDomainResult(imported=1, skipped=0)
    loaded = asyncio.run(DianaEventStore(db_path, "yuexi").get_event(9))
    assert loaded is not None
    assert loaded["event_id"] == 9
    assert loaded["payload"] == {"append": True}
    assert _projection_state_value(db_path, "yuexi") == "9"


def test_import_legacy_events_union_deduplicates_and_rerun_is_idempotent(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    event = _legacy_event(5, payload={"same": "event"}, idempotency_key="same:5")
    _write_legacy_event_sqlite(source_dir / "events.sqlite3", [event])
    _write_jsonl(source_dir / "events.sqlite3.append.jsonl", [event])

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    second = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert first.events == LegacyImportDomainResult(imported=1, skipped=1)
    assert second.events == LegacyImportDomainResult(imported=0, skipped=2)
    assert _event_rows(db_path, "yuexi") == [
        {"persona_id": "yuexi", **event},
    ]


def test_import_legacy_events_same_id_same_hash_different_row_warns(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    original = _legacy_event(
        6,
        event_uuid="uuid-original",
        payload={"text": "原始"},
        payload_hash="same-hash",
        schema_version=1,
        idempotency_key="msg:6",
    )
    conflict = {
        **original,
        "event_uuid": "uuid-conflict",
        "payload_json": orjson.dumps({"text": "变更"}).decode("utf-8"),
        "schema_version": 2,
    }
    _write_legacy_event_sqlite(source_dir / "events.sqlite3", [original])
    _write_jsonl(source_dir / "events.sqlite3.append.jsonl", [conflict])

    with caplog.at_level("WARNING"):
        result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.events == LegacyImportDomainResult(imported=1, skipped=1)
    assert _event_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **original}]
    loaded = asyncio.run(DianaEventStore(db_path, "yuexi").get_event(6))
    assert loaded["event_uuid"] == "uuid-original"
    assert loaded["payload"] == {"text": "原始"}
    assert loaded["schema_version"] == 1
    assert "跳过冲突的旧事件 event_id=6" in caplog.text


def test_import_legacy_events_conflicts_skip_without_overwrite(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    original = _legacy_event(
        1,
        event_type="message",
        payload={"text": "原始"},
        payload_hash="hash-original",
        idempotency_key="msg:1",
    )
    event_id_conflict = _legacy_event(
        1,
        event_type="tool",
        payload={"text": "冲突"},
        payload_hash="hash-conflict",
        idempotency_key="msg:conflict",
    )
    idempotency_conflict = _legacy_event(
        2,
        event_type="message",
        payload={"text": "重复键"},
        payload_hash="hash-duplicate-key",
        idempotency_key="msg:1",
    )
    _write_legacy_event_sqlite(source_dir / "events.sqlite3", [original])
    _write_jsonl(
        source_dir / "events.sqlite3.append.jsonl",
        [event_id_conflict, idempotency_conflict],
    )

    with caplog.at_level("WARNING"):
        result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.events == LegacyImportDomainResult(imported=1, skipped=2)
    assert _event_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **original}]
    assert asyncio.run(DianaEventStore(db_path, "yuexi").get_event(2)) is None
    assert "跳过冲突的旧事件 event_id=1" in caplog.text
    assert "跳过冲突的旧事件 idempotency_key=msg:1" in caplog.text


def test_import_legacy_events_skips_bad_append_log_lines(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    append_log_path = source_dir / "events.sqlite3.append.jsonl"
    append_log_path.parent.mkdir(parents=True, exist_ok=True)
    valid = _legacy_event(3, payload={"ok": True})
    bad_payload = _legacy_event(4, payload={"bad": True})
    bad_payload["payload_json"] = "{broken"
    append_log_path.write_bytes(
        b"{broken\n"
        b"[]\n"
        b'{"event_id":4}\n'
        + orjson.dumps(bad_payload)
        + b"\n"
        + orjson.dumps(valid)
        + b"\n"
    )

    with caplog.at_level("WARNING"):
        result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.events == LegacyImportDomainResult(imported=1, skipped=4)
    assert _event_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **valid}]
    assert "跳过损坏的旧事件 append log 行" in caplog.text
    assert "跳过非对象旧事件 append log 行" in caplog.text
    assert "跳过缺少关键字段的旧事件行" in caplog.text
    assert "跳过 payload_json 损坏的旧事件行" in caplog.text


def test_import_legacy_events_sqlite_without_event_log_is_missing_domain(tmp_path, caplog):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    sqlite_path = source_dir / "events.sqlite3"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")

    with caplog.at_level("WARNING"):
        result = import_legacy_memory_files(db_path, source_dir, "yuexi")

    assert result.events == LegacyImportDomainResult(imported=0, skipped=0)
    assert _event_rows(db_path, "yuexi") == []
    assert _projection_state_value(db_path, "yuexi") is None
    assert "旧 events.sqlite3 缺少 event_log 表" in caplog.text


def test_import_legacy_events_keeps_personas_isolated(tmp_path):
    source_dir = tmp_path / "legacy"
    db_path = tmp_path / "diana.db"
    event = _legacy_event(11, payload={"shared": True}, idempotency_key="shared:11")
    _write_legacy_event_sqlite(source_dir / "events.sqlite3", [event])

    first = import_legacy_memory_files(db_path, source_dir, "yuexi")
    second = import_legacy_memory_files(db_path, source_dir, "jiu")

    assert first.events == LegacyImportDomainResult(imported=1, skipped=0)
    assert second.events == LegacyImportDomainResult(imported=1, skipped=0)
    assert _event_rows(db_path, "yuexi") == [{"persona_id": "yuexi", **event}]
    assert _event_rows(db_path, "jiu") == [{"persona_id": "jiu", **event}]
    assert asyncio.run(DianaEventStore(db_path, "yuexi").get_event(11))["payload"] == {
        "shared": True,
    }
    assert asyncio.run(DianaEventStore(db_path, "jiu").get_event(11))["payload"] == {
        "shared": True,
    }
    assert _projection_state_value(db_path, "yuexi") == "11"
    assert _projection_state_value(db_path, "jiu") == "11"


def test_memory_package_exports_legacy_importer():
    from memory import import_legacy_memory_files as package_importer

    assert package_importer is import_legacy_memory_files


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(data))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(orjson.dumps(record) + b"\n" for record in records))


def _write_legacy_event_sqlite(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE event_log (
                event_id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                event_uuid TEXT NOT NULL,
                conversation_id TEXT,
                session_id TEXT,
                turn_id TEXT,
                source TEXT,
                external_id TEXT,
                tool_call_id TEXT,
                parent_event_id INTEGER,
                idempotency_key TEXT UNIQUE,
                timestamp_unix REAL NOT NULL,
                created_at_unix REAL NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO event_log (
                event_id, event_type, event_uuid, conversation_id, session_id,
                turn_id, source, external_id, tool_call_id, parent_event_id,
                idempotency_key, timestamp_unix, created_at_unix, payload_json,
                payload_hash, schema_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_legacy_event_sql_values(row) for row in rows],
        )


def _write_legacy_archive_sqlite(
    path: Path,
    messages: list[dict],
    media: list[dict],
    *,
    include_record_json: bool = True,
    include_media_table: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        record_json_column = "record_json TEXT," if include_record_json else ""
        conn.execute(
            f"""
            CREATE TABLE archive_messages (
                rowid INTEGER PRIMARY KEY,
                archive_id TEXT UNIQUE NOT NULL,
                timestamp TEXT,
                timestamp_unix INTEGER,
                date_key TEXT,
                month_key TEXT,
                conversation_id TEXT,
                conversation_type TEXT,
                target_id TEXT,
                sender_id TEXT,
                sender_name TEXT,
                sender_role TEXT,
                direction TEXT,
                message_kind TEXT,
                content TEXT,
                content_search TEXT,
                original_msg_id TEXT,
                reply_to_msg_id TEXT,
                metadata_json TEXT,
                {record_json_column}
                created_at TEXT
            )
            """
        )
        if include_record_json:
            conn.executemany(
                """
                INSERT INTO archive_messages (
                    rowid, archive_id, timestamp, timestamp_unix, date_key,
                    month_key, conversation_id, conversation_type, target_id,
                    sender_id, sender_name, sender_role, direction, message_kind,
                    content, content_search, original_msg_id, reply_to_msg_id,
                    metadata_json, record_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_legacy_archive_message_sql_values(row) for row in messages],
            )
        else:
            conn.executemany(
                """
                INSERT INTO archive_messages (
                    rowid, archive_id, timestamp, timestamp_unix, date_key,
                    month_key, conversation_id, conversation_type, target_id,
                    sender_id, sender_name, sender_role, direction, message_kind,
                    content, content_search, original_msg_id, reply_to_msg_id,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_legacy_archive_message_sql_values_without_record_json(row) for row in messages],
            )
        if include_media_table:
            conn.execute(
                """
                CREATE TABLE archive_message_media (
                    id INTEGER PRIMARY KEY,
                    archive_id TEXT NOT NULL,
                    media_type TEXT,
                    workspace_path TEXT,
                    original_name TEXT,
                    metadata_json TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO archive_message_media (
                    id, archive_id, media_type, workspace_path, original_name, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [_legacy_archive_media_sql_values(row) for row in media],
            )


def _write_legacy_archive_sqlite_missing_message_column(
    path: Path,
    messages: list[dict],
    media: list[dict],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE archive_messages (
                rowid INTEGER PRIMARY KEY,
                archive_id TEXT UNIQUE NOT NULL,
                timestamp TEXT,
                timestamp_unix INTEGER,
                date_key TEXT,
                month_key TEXT,
                conversation_id TEXT,
                conversation_type TEXT,
                target_id TEXT,
                sender_id TEXT,
                sender_name TEXT,
                sender_role TEXT,
                direction TEXT,
                message_kind TEXT,
                content_search TEXT,
                original_msg_id TEXT,
                reply_to_msg_id TEXT,
                metadata_json TEXT,
                record_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO archive_messages (
                rowid, archive_id, timestamp, timestamp_unix, date_key,
                month_key, conversation_id, conversation_type, target_id,
                sender_id, sender_name, sender_role, direction, message_kind,
                content_search, original_msg_id, reply_to_msg_id, metadata_json,
                record_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_legacy_archive_message_sql_values_without_content(row) for row in messages],
        )
        conn.execute(
            """
            CREATE TABLE archive_message_media (
                id INTEGER PRIMARY KEY,
                archive_id TEXT NOT NULL,
                media_type TEXT,
                workspace_path TEXT,
                original_name TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO archive_message_media (
                id, archive_id, media_type, workspace_path, original_name, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [_legacy_archive_media_sql_values(row) for row in media],
        )


def _legacy_event(
    event_id: int,
    *,
    event_type: str = "message",
    event_uuid: str | None = None,
    payload: dict | None = None,
    payload_hash: str | None = None,
    schema_version: int = 2,
    conversation_id: str | None = "private:1",
    session_id: str | None = "session-1",
    turn_id: str | None = None,
    source: str | None = "runtime",
    external_id: str | None = None,
    tool_call_id: str | None = None,
    parent_event_id: int | None = None,
    idempotency_key: str | None = None,
) -> dict:
    payload_data = {"event_id": event_id} if payload is None else payload
    return {
        "event_id": event_id,
        "event_type": event_type,
        "event_uuid": event_uuid or f"uuid-{event_id}",
        "conversation_id": conversation_id,
        "session_id": session_id,
        "turn_id": turn_id or str(event_id),
        "source": source,
        "external_id": external_id or f"external-{event_id}",
        "tool_call_id": tool_call_id,
        "parent_event_id": parent_event_id,
        "idempotency_key": idempotency_key,
        "timestamp_unix": float(1000 + event_id),
        "created_at_unix": float(2000 + event_id),
        "payload_json": orjson.dumps(payload_data).decode("utf-8"),
        "payload_hash": payload_hash or f"hash-{event_id}",
        "schema_version": schema_version,
    }


def _legacy_event_sql_values(row: dict) -> tuple:
    return (
        row["event_id"],
        row["event_type"],
        row["event_uuid"],
        row["conversation_id"],
        row["session_id"],
        row["turn_id"],
        row["source"],
        row["external_id"],
        row["tool_call_id"],
        row["parent_event_id"],
        row["idempotency_key"],
        row["timestamp_unix"],
        row["created_at_unix"],
        row["payload_json"],
        row["payload_hash"],
        row["schema_version"],
    )


def _legacy_archive_message(
    rowid: int,
    *,
    archive_id: str | None = None,
    content: str = "旧归档",
    content_search: str | None = None,
    timestamp: str = "2026-06-18 12:00:00",
    timestamp_unix: int = 1_782_000_000,
    conversation_id: str = "private:1",
    metadata: dict | None = None,
    record: dict | None = None,
) -> dict:
    metadata_data = {"source": "legacy-test"} if metadata is None else metadata
    record_data = (
        {
            "role": "user",
            "content": content,
            "conversation_id": conversation_id,
            "metadata": metadata_data,
        }
        if record is None
        else record
    )
    return {
        "rowid": rowid,
        "archive_id": archive_id or f"old-{rowid}",
        "timestamp": timestamp,
        "timestamp_unix": timestamp_unix,
        "date_key": "2026-06-18",
        "month_key": "2026-06",
        "conversation_id": conversation_id,
        "conversation_type": "private",
        "target_id": "1",
        "sender_id": "user-1",
        "sender_name": "Alice",
        "sender_role": "user",
        "direction": "inbound",
        "message_kind": "text",
        "content": content,
        "content_search": content if content_search is None else content_search,
        "original_msg_id": f"msg-{rowid}",
        "reply_to_msg_id": None,
        "metadata_json": orjson.dumps(metadata_data).decode("utf-8"),
        "record_json": orjson.dumps(record_data).decode("utf-8"),
        "created_at": "2026-06-18 12:00:01",
    }


def _legacy_archive_media(
    media_id: int,
    *,
    archive_id: str,
    media_type: str = "image",
    workspace_path: str = "image.jpg",
    original_name: str = "image.jpg",
    metadata: dict | None = None,
) -> dict:
    return {
        "id": media_id,
        "archive_id": archive_id,
        "media_type": media_type,
        "workspace_path": workspace_path,
        "original_name": original_name,
        "metadata_json": orjson.dumps(metadata or {"source": "legacy-test"}).decode("utf-8"),
    }


def _legacy_archive_message_sql_values(row: dict) -> tuple:
    return (
        row["rowid"],
        row["archive_id"],
        row["timestamp"],
        row["timestamp_unix"],
        row["date_key"],
        row["month_key"],
        row["conversation_id"],
        row["conversation_type"],
        row["target_id"],
        row["sender_id"],
        row["sender_name"],
        row["sender_role"],
        row["direction"],
        row["message_kind"],
        row["content"],
        row["content_search"],
        row["original_msg_id"],
        row["reply_to_msg_id"],
        row["metadata_json"],
        row["record_json"],
        row["created_at"],
    )


def _legacy_archive_message_sql_values_without_record_json(row: dict) -> tuple:
    values = _legacy_archive_message_sql_values(row)
    return (*values[:19], values[20])


def _legacy_archive_message_sql_values_without_content(row: dict) -> tuple:
    values = _legacy_archive_message_sql_values(row)
    return (*values[:14], *values[15:])


def _legacy_archive_media_sql_values(row: dict) -> tuple:
    return (
        row["id"],
        row["archive_id"],
        row["media_type"],
        row["workspace_path"],
        row["original_name"],
        row["metadata_json"],
    )


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


def _event_rows(db_path: Path, persona_id: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM event_log
            WHERE persona_id = ?
            ORDER BY event_id ASC
            """,
            (persona_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _archive_message_rows(db_path: Path, persona_id: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM archive_messages
            WHERE persona_id = ?
            ORDER BY rowid ASC
            """,
            (persona_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _archive_media_rows(db_path: Path, persona_id: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM archive_message_media
            WHERE persona_id = ?
            ORDER BY id ASC
            """,
            (persona_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _projection_state_value(db_path: Path, persona_id: str) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT value
            FROM event_projection_state
            WHERE persona_id = ? AND name = 'last_projected_event_id'
            """,
            (persona_id,),
        ).fetchone()
    return None if row is None else str(row[0])


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
