from __future__ import annotations

import sqlite3

import pytest

from memory import ArchiveStore
from tools import ToolContext, ToolRegistry, get_default_specs

from .helpers import _chat_record, _runtime_records, _send_tool_result_record


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
            _send_tool_result_record(
                "第二条回复",
                conversation_id="private:1",
                msg_id="bot-2",
                timestamp="2026-06-01 10:01:00",
            ),
        ]
    )

    records = await archive.records()
    ids = [record["archive_id"] for record in records]
    assert ids == ["a1", "a2"]
    assert all(len(archive_id) <= 8 for archive_id in ids)
    assert records[0]["role"] == "user"
    assert records[0]["conversation_id"] == "private:1"
    assert records[0]["metadata"]["timestamp"] == "2026-06-01 10:00:00"
    assert records[1]["role"] == "assistant"
    assert records[1]["metadata"]["source"] == "send_result"

    reloaded = ArchiveStore(tmp_path / "archive.sqlite3")
    assert [record["archive_id"] for record in await reloaded.records()] == ids

@pytest.mark.asyncio
async def test_archive_append_many_skips_exact_duplicate_records(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    record = _chat_record(
        "重复图片 [图片 workspace=incoming/dup.jpg]",
        conversation_id="private:1",
        sender_id="1",
        sender_name="Alice",
        timestamp="2026-06-01 10:00:00",
    )
    next_record = _chat_record(
        "重复后新增",
        conversation_id="private:1",
        sender_id="1",
        sender_name="Alice",
        timestamp="2026-06-01 10:01:00",
    )

    await archive.append_many([record, record])
    reloaded = ArchiveStore(tmp_path / "archive.sqlite3")
    await reloaded.append_many([record, next_record])

    records = await reloaded.records()
    assert [item["archive_id"] for item in records] == ["a1", "a2"]
    assert [item["content"] for item in records] == [
        "重复图片 [图片 workspace=incoming/dup.jpg]",
        "重复后新增",
    ]
    media = await reloaded.media_records()
    assert len(media) == 1
    assert media[0]["archive_id"] == "a1"
    assert media[0]["workspace_path"] == "incoming/dup.jpg"

@pytest.mark.asyncio
async def test_archive_append_many_keeps_same_content_with_different_metadata(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    first = _chat_record(
        "同文本不同消息",
        conversation_id="private:1",
        sender_id="1",
        sender_name="Alice",
        timestamp="2026-06-01 10:00:00",
    )
    second = _chat_record(
        "同文本不同消息",
        conversation_id="private:1",
        sender_id="1",
        sender_name="Alice",
        timestamp="2026-06-01 10:01:00",
    )

    await archive.append_many([first, second])

    records = await archive.records()
    assert [item["archive_id"] for item in records] == ["a1", "a2"]
    assert [item["content"] for item in records] == [
        "同文本不同消息",
        "同文本不同消息",
    ]
    assert records[0]["metadata"]["timestamp"] == "2026-06-01 10:00:00"
    assert records[1]["metadata"]["timestamp"] == "2026-06-01 10:01:00"

@pytest.mark.asyncio
async def test_archive_append_many_keeps_only_real_chat_and_sent_outbound(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "真实入站图片 [图片 workspace=incoming/cat.jpg]",
                conversation_id="group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-01 10:00:00",
            ),
            _send_tool_result_record(
                "机器人实际发出的回复",
                conversation_id="group:2",
                msg_id="bot-1",
            ),
            *_runtime_records("group:2"),
        ]
    )

    records = await archive.records()
    assert [record["role"] for record in records] == ["user", "assistant"]
    assert [record["content"] for record in records] == [
        "真实入站图片 [图片 workspace=incoming/cat.jpg]",
        "机器人实际发出的回复",
    ]
    assert records[1]["metadata"]["qq_visible"] is True
    assert records[1]["metadata"]["source"] == "send_result"
    assert records[1]["original_msg_id"] == "bot-1"

    joined = "\n".join(record["content"] for record in records)
    assert "delete memory result" not in joined
    assert "task_context" not in joined
    assert "send_status" not in joined
    assert "send_receipt" not in joined
    assert "思考过程" not in joined
    assert "内部最终说明" not in joined

@pytest.mark.asyncio
async def test_archive_rejects_internal_assistant_text_from_write_and_queries(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
            _chat_record(
                "真实用户消息",
                conversation_id="private:1",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            {
                "role": "assistant",
                "content": "内部最终说明未发送到 QQ",
                "conversation_id": "private:1",
                "metadata": {"timestamp": "2026-06-01 10:01:00"},
            },
        ]
    )

    records = await archive.records()
    assert [record["content"] for record in records] == ["真实用户消息"]
    assert await archive.search(keyword="内部最终说明", limit=10) == []

    filtered = await archive.filter_records({"keywords": ["内部最终说明"], "limit": 10})
    assert filtered["count"] == 0

    ctx = ToolContext(archive=archive)
    executor = ToolRegistry(get_default_specs()).get_executor(ctx)
    recalled = await executor(
        "recall_history",
        {"conversation_id": "private:1", "keyword": "内部最终说明", "limit": 10},
    )
    assert recalled["count"] == 0
    assert "内部最终说明" not in recalled["content"]

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
