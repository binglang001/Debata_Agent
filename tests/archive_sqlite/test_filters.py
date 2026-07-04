from __future__ import annotations

import pytest

from memory import ArchiveStore

from .helpers import _chat_record, _spy_archive_sql


@pytest.mark.asyncio
async def test_archive_filter_records_multi_ranges_and_match_modes(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "精确短句",
                conversation_id="private:1",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 09:00:00",
            ),
            _chat_record(
                "猫猫截图",
                conversation_id="group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-02 10:00:00",
            ),
            _chat_record(
                "茶会安排周五",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-03 10:00:00",
            ),
            _chat_record(
                "无关内容",
                conversation_id="group:3",
                sender_id="3",
                sender_name="Carol",
                timestamp="2026-07-01 10:00:00",
            ),
        ]
    )

    exact = await archive.filter_records(
        {"keywords": ["精确短句"], "keyword_match": "exact", "order": "asc"}
    )
    assert [item["content"] for item in exact["results"]] == ["精确短句"]

    contains = await archive.filter_records(
        {
            "conversation_ids": ["group:2"],
            "sender_names": ["Alice"],
            "keywords": ["茶会"],
            "time_ranges": [
                {"start": "2026-06-03 00:00:00", "end": "2026-06-03 23:59:59"}
            ],
            "order": "asc",
        }
    )
    assert contains["total"] == 1
    assert contains["results"][0]["sender"] == "Alice(1)"

    fuzzy = await archive.filter_records(
        {
            "keywords": ["茶會安排周五"],
            "keyword_match": "fuzzy",
            "sender_names": ["Alic"],
            "sender_match": "fuzzy",
        }
    )
    assert fuzzy["total"] == 1
    assert fuzzy["results"][0]["content"] == "茶会安排周五"

    paged = await archive.filter_records(
        {"conversation_ids": ["group:2"], "limit": 1, "offset": 1, "order": "asc"}
    )
    assert paged["total"] == 2
    assert paged["count"] == 1
    assert paged["results"][0]["content"] == "茶会安排周五"

@pytest.mark.asyncio
async def test_archive_filter_records_pushes_exact_conversation_and_time_sql(
    tmp_path,
    monkeypatch,
):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "范围前",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 09:00:00",
            ),
            _chat_record(
                "范围内 10",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "范围内 11",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 11:00:00",
            ),
            _chat_record(
                "范围内 12",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 12:00:00",
            ),
            _chat_record(
                "别的会话同时间",
                conversation_id="group:3",
                sender_id="3",
                sender_name="Carol",
                timestamp="2026-06-01 11:00:00",
            ),
            _chat_record(
                "范围后",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-02 10:00:00",
            ),
        ]
    )
    sql_log = _spy_archive_sql(archive, monkeypatch)

    asc = await archive.filter_records(
        {
            "conversation_ids": ["group:2"],
            "time_ranges": [
                {"start": "2026-06-01 10:00:00", "end": "2026-06-01 12:00:00"}
            ],
            "limit": 2,
            "offset": 1,
            "order": "asc",
        }
    )

    assert asc["total"] == 3
    assert [item["content"] for item in asc["results"]] == ["范围内 11", "范围内 12"]
    asc_selects = [entry for entry in sql_log if entry["sql"].startswith("SELECT *")]
    asc_raw_select = next(
        entry
        for entry in asc_selects
        if "conversation_id IN" in entry["sql"]
        and "conversation_id NOT IN" not in entry["sql"]
    )
    asc_fallback_select = next(
        entry for entry in asc_selects if "conversation_id NOT IN" in entry["sql"]
    )
    assert "LOWER(conversation_id)" not in asc_raw_select["sql"]
    assert "timestamp_unix >=" in asc_raw_select["sql"]
    assert "timestamp_unix <=" in asc_raw_select["sql"]
    assert "LIMIT ? OFFSET ?" not in asc_raw_select["sql"]
    assert asc_raw_select["row_count"] == 3
    assert asc_fallback_select["row_count"] == 1

    sql_log.clear()
    desc = await archive.filter_records(
        {
            "conversation_ids": ["group:2"],
            "time_ranges": [
                {"start": "2026-06-01 10:00:00", "end": "2026-06-01 12:00:00"}
            ],
            "limit": 2,
            "offset": 1,
            "order": "desc",
        }
    )

    assert desc["total"] == 3
    assert [item["content"] for item in desc["results"]] == ["范围内 11", "范围内 10"]
    desc_selects = [entry for entry in sql_log if entry["sql"].startswith("SELECT *")]
    desc_raw_select = next(
        entry
        for entry in desc_selects
        if "conversation_id IN" in entry["sql"]
        and "conversation_id NOT IN" not in entry["sql"]
    )
    assert (
        "ORDER BY COALESCE(timestamp_unix, -1) DESC, rowid DESC"
        in desc_raw_select["sql"]
    )
    assert desc_raw_select["row_count"] == 3

@pytest.mark.asyncio
async def test_archive_filter_records_exact_conversation_keeps_case_variants(
    tmp_path,
    monkeypatch,
):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "原始小写会话",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "大小写变体会话",
                conversation_id="Group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-01 11:00:00",
            ),
            _chat_record(
                "别的会话",
                conversation_id="group:3",
                sender_id="3",
                sender_name="Carol",
                timestamp="2026-06-01 12:00:00",
            ),
        ]
    )
    sql_log = _spy_archive_sql(archive, monkeypatch)

    result = await archive.filter_records(
        {"conversation_ids": ["group:2"], "order": "asc", "limit": 10}
    )

    assert result["total"] == 2
    assert [item["content"] for item in result["results"]] == [
        "原始小写会话",
        "大小写变体会话",
    ]
    select_sql = "\n".join(
        entry["sql"] for entry in sql_log if entry["sql"].startswith("SELECT *")
    )
    assert "conversation_id IN" in select_sql
    assert "conversation_id NOT IN" in select_sql
    assert "LOWER(conversation_id)" not in select_sql

@pytest.mark.asyncio
async def test_archive_filter_records_exact_sender_name_uses_python_unicode_lower(
    tmp_path,
    monkeypatch,
):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "Unicode 名字命中",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Élodie",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "无关名字",
                conversation_id="group:2",
                sender_id="2",
                sender_name="Alice",
                timestamp="2026-06-01 11:00:00",
            ),
        ]
    )
    sql_log = _spy_archive_sql(archive, monkeypatch)

    result = await archive.filter_records(
        {"sender_names": ["élodie"], "order": "asc", "limit": 10}
    )

    assert result["total"] == 1
    assert result["results"][0]["content"] == "Unicode 名字命中"
    select_sql = "\n".join(
        entry["sql"] for entry in sql_log if entry["sql"].startswith("SELECT *")
    )
    assert "LOWER(sender_name)" not in select_sql

@pytest.mark.asyncio
async def test_archive_filter_records_python_fallback_keeps_total_after_slice(
    tmp_path,
    monkeypatch,
):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "非命中 0",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 09:00:00",
            ),
            _chat_record(
                "KEEP 一",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "六月安排",
                conversation_id="group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-01 11:00:00",
            ),
            _chat_record(
                "KEEP 二",
                conversation_id="group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-01 12:00:00",
            ),
            _chat_record(
                "KEEP 三 六月",
                conversation_id="group:2",
                sender_id="3",
                sender_name="Carol",
                timestamp="2026-06-01 13:00:00",
            ),
            _chat_record(
                "别的会话 KEEP",
                conversation_id="group:9",
                sender_id="9",
                sender_name="Other",
                timestamp="2026-06-01 14:00:00",
            ),
        ]
    )
    sql_log = _spy_archive_sql(archive, monkeypatch)

    keyword = await archive.filter_records(
        {
            "conversation_ids": ["group:2"],
            "keywords": ["KEEP"],
            "limit": 1,
            "offset": 1,
            "order": "asc",
        }
    )

    assert keyword["total"] == 3
    assert keyword["count"] == 1
    assert keyword["results"][0]["content"] == "KEEP 二"
    keyword_selects = [entry for entry in sql_log if entry["sql"].startswith("SELECT *")]
    keyword_raw_select = next(
        entry
        for entry in keyword_selects
        if "conversation_id IN" in entry["sql"]
        and "conversation_id NOT IN" not in entry["sql"]
    )
    keyword_fallback_select = next(
        entry for entry in keyword_selects if "conversation_id NOT IN" in entry["sql"]
    )
    assert "LIMIT ? OFFSET ?" not in keyword_raw_select["sql"]
    assert keyword_raw_select["row_count"] == 5
    assert keyword_fallback_select["row_count"] == 1

    contains = await archive.filter_records(
        {
            "conversation_ids": ["group:"],
            "conversation_match": "contains",
            "limit": 2,
            "offset": 4,
            "order": "asc",
        }
    )
    assert contains["total"] == 6
    assert [item["content"] for item in contains["results"]] == [
        "KEEP 三 六月",
        "别的会话 KEEP",
    ]

    unparsed_time = await archive.filter_records(
        {
            "time_ranges": [{"start": "六月"}],
            "limit": 1,
            "offset": 1,
            "order": "asc",
        }
    )
    assert unparsed_time["total"] == 2
    assert unparsed_time["results"][0]["content"] == "KEEP 三 六月"

@pytest.mark.asyncio
async def test_archive_filter_records_sql_order_matches_empty_and_duplicate_timestamps(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            {
                "role": "user",
                "content": "无时间",
                "conversation_id": "group:2",
                "metadata": {
                    "messages": [
                        {
                            "scope": "group",
                            "target_id": "2",
                            "group_id": "2",
                            "user_id": "1",
                            "nickname": "Alice",
                            "message_id": "msg-no-time",
                        }
                    ]
                },
            },
            _chat_record(
                "同秒一",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "同秒二",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "更晚",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 11:00:00",
            ),
        ]
    )

    asc = await archive.filter_records(
        {"conversation_ids": ["group:2"], "order": "asc", "limit": 10}
    )
    assert [item["content"] for item in asc["results"]] == [
        "无时间",
        "同秒一",
        "同秒二",
        "更晚",
    ]

    desc = await archive.filter_records(
        {"conversation_ids": ["group:2"], "order": "desc", "limit": 10}
    )
    assert [item["content"] for item in desc["results"]] == [
        "更晚",
        "同秒二",
        "同秒一",
        "无时间",
    ]
