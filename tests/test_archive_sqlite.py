from __future__ import annotations

import sqlite3

import pytest

from memory import ArchiveStore
from tools import ToolContext, ToolRegistry, get_default_specs


def _approve_stub_tools(ctx: ToolContext, *names: str) -> None:
    approved = ctx.extras.setdefault("tool_search_approved_tools", set())
    approved.update(names)


def _chat_record(
    content: str,
    *,
    conversation_id: str,
    sender_id: str,
    sender_name: str,
    timestamp: str,
) -> dict:
    scope, target_id = conversation_id.split(":", 1)
    return {
        "role": "user",
        "content": content,
        "conversation_id": conversation_id,
        "metadata": {
            "timestamp": timestamp,
            "messages": [
                {
                    "scope": scope,
                    "target_id": target_id,
                    "group_id": target_id if scope == "group" else None,
                    "user_id": sender_id,
                    "nickname": sender_name,
                    "message_id": f"msg-{sender_id}-{timestamp}",
                    "timestamp": timestamp,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_archive_sqlite_starts_empty_and_ignores_legacy_jsonl(tmp_path):
    legacy = tmp_path / "archive.jsonl"
    legacy.write_text('{"role":"user","content":"旧 JSONL 记录"}\n', encoding="utf-8")

    archive = ArchiveStore(legacy)
    assert archive.path.name == "archive.sqlite3"
    assert await archive.load() == []

    await archive.append_many(
        [
            _chat_record(
                "sqlite 新记录",
                conversation_id="private:1",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            )
        ]
    )

    records = await archive.records()
    assert len(records) == 1
    assert records[0]["content"] == "sqlite 新记录"
    assert "旧 JSONL 记录" not in str(records)


@pytest.mark.asyncio
async def test_archive_append_records_short_ids_are_stable_and_compatible(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.load()
    await archive.append_many(
        [
            _chat_record(
                "第一条",
                conversation_id="private:1",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            {
                "role": "assistant",
                "content": "第二条回复",
                "conversation_id": "private:1",
                "metadata": {"timestamp": "2026-06-01 10:01:00"},
            },
        ]
    )

    records = await archive.records()
    ids = [record["archive_id"] for record in records]
    assert ids == ["a1", "a2"]
    assert all(len(archive_id) <= 8 for archive_id in ids)
    assert records[0]["role"] == "user"
    assert records[0]["conversation_id"] == "private:1"
    assert records[0]["metadata"]["timestamp"] == "2026-06-01 10:00:00"

    reloaded = ArchiveStore(tmp_path / "archive.sqlite3")
    assert [record["archive_id"] for record in await reloaded.records()] == ids


@pytest.mark.asyncio
async def test_archive_media_paths_and_search_text_strip_qq_urls(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    content = (
        "猫猫截图 [图片 workspace=incoming/img_1.jpg "
        "url=https://multimedia.nt.qq.com.cn/download?rkey=secret&clientkey=x]"
    )
    record = _chat_record(
        content,
        conversation_id="group:2",
        sender_id="2",
        sender_name="Bob",
        timestamp="2026-06-02 10:00:00",
    )
    record["metadata"]["text"] = content
    await archive.append_many(
        [
            record,
        ]
    )

    media = await archive.media_records()
    assert media[0]["media_type"] == "image"
    assert media[0]["workspace_path"] == "incoming/img_1.jpg"

    records = await archive.records()
    assert "workspace=incoming/img_1.jpg" in records[0]["content"]
    assert "multimedia.nt.qq.com.cn" not in records[0]["content"]
    with sqlite3.connect(archive.path) as conn:
        content_search = conn.execute(
            "SELECT content_search FROM archive_messages"
        ).fetchone()[0]
    assert "workspace=incoming/img_1.jpg" in content_search
    assert "multimedia.nt.qq.com.cn" not in content_search
    assert "rkey=" not in content_search
    assert await archive.search(keyword="multimedia.nt.qq.com.cn") == []
    assert await archive.search(keyword="rkey=secret") == []


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
async def test_filter_tool_and_recall_history_expand_archive_ids_same_conversation(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "别的会话前文",
                conversation_id="group:9",
                sender_id="9",
                sender_name="Other",
                timestamp="2026-06-01 09:00:00",
            ),
            _chat_record(
                "同会话前文",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "目标茶会记录",
                conversation_id="group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-01 10:01:00",
            ),
            _chat_record(
                "同会话后文",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:02:00",
            ),
            _chat_record(
                "别的会话后文",
                conversation_id="private:3",
                sender_id="3",
                sender_name="Carol",
                timestamp="2026-06-01 10:03:00",
            ),
        ]
    )

    ctx = ToolContext(archive=archive)
    _approve_stub_tools(ctx, "filter_archive_records")
    executor = ToolRegistry(get_default_specs()).get_executor(ctx)

    filtered = await executor(
        "filter_archive_records",
        {"keywords": ["目标茶会"], "limit": 5},
    )

    assert filtered["ok"] is True
    assert filtered["count"] == 1
    target_id = filtered["results"][0]["id"]
    assert "目标茶会记录" in filtered["results"][0]["content"]

    recalled = await executor(
        "recall_history",
        {"archive_ids": [target_id], "context_before": 1, "context_after": 1},
    )

    assert recalled["ok"] is True
    assert recalled["count"] == 3
    assert "同会话前文" in recalled["content"]
    assert "目标茶会记录" in recalled["content"]
    assert "同会话后文" in recalled["content"]
    assert "别的会话前文" not in recalled["content"]
    assert "别的会话后文" not in recalled["content"]
    assert recalled["results"][1]["id"] == target_id


@pytest.mark.asyncio
async def test_archive_context_missing_conversation_id_does_not_expand_unknown_bucket(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            {
                "role": "user",
                "content": "未知会话前文",
                "metadata": {"timestamp": "2026-06-01 10:00:00"},
            },
            {
                "role": "user",
                "content": "未知会话目标",
                "metadata": {"timestamp": "2026-06-01 10:01:00"},
            },
            {
                "role": "user",
                "content": "未知会话后文",
                "metadata": {"timestamp": "2026-06-01 10:02:00"},
            },
        ]
    )

    records = await archive.records()
    context = await archive.context_around(
        records[1]["archive_id"],
        before=1,
        after=1,
    )

    assert [record["content"] for record in context] == ["未知会话目标"]
