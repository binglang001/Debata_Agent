from __future__ import annotations

import pytest

from memory import ArchiveStore
from tools import ToolContext, ToolRegistry, get_default_specs

from .helpers import (
    _approve_stub_tools,
    _chat_record,
    _HistoryStub,
    _insert_legacy_archive_row,
    _runtime_records,
    _send_tool_result_record,
)


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
    assert "content" not in filtered["results"][0]
    assert "目标茶会记录" in filtered["results"][0]["snippet"]
    assert filtered["results"][0]["conversation_id"] == "group:2"
    assert filtered["results"][0]["sender_id"] == "2"
    assert "recall_history" in filtered["next"]

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
async def test_archive_read_paths_filter_legacy_runtime_pollution(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.append_many(
        [
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
        ]
    )
    records = await archive.records()
    target_id = records[1]["archive_id"]

    _insert_legacy_archive_row(
        archive,
        rowid=4,
        archive_id="ztool",
        role="tool",
        content='{"ok": true, "brief": "delete memory result"}',
        conversation_id="group:2",
    )
    _insert_legacy_archive_row(
        archive,
        rowid=5,
        archive_id="zctx",
        role="user",
        content="<task_context>运行时上下文污染</task_context>",
        conversation_id="group:2",
        metadata={"kind": "task_context_snapshot"},
    )
    _insert_legacy_archive_row(
        archive,
        rowid=6,
        archive_id="zasst",
        role="assistant",
        content="旧库内部 assistant 文本污染",
        conversation_id="group:2",
        direction="outbound",
        message_kind="text",
        metadata={"timestamp": "2026-06-01 10:03:00"},
    )

    assert "delete memory result" not in str(await archive.records())
    assert "旧库内部 assistant 文本污染" not in str(await archive.records())
    assert await archive.search(keyword="delete memory result", limit=10) == []
    assert await archive.search(keyword="旧库内部 assistant", limit=10) == []
    filtered = await archive.filter_records(
        {"keywords": ["运行时上下文污染"], "limit": 10}
    )
    assert filtered["count"] == 0
    assistant_filtered = await archive.filter_records(
        {"keywords": ["旧库内部 assistant"], "limit": 10}
    )
    assert assistant_filtered["count"] == 0
    scoped = await archive.filter_records(
        {"conversation_ids": ["group:2"], "limit": 10, "order": "asc"}
    )
    assert [item["content"] for item in scoped["results"]] == [
        "同会话前文",
        "目标茶会记录",
        "同会话后文",
    ]
    assert await archive.get_by_ids(["ztool", "zctx", "zasst"]) == []
    assert "旧库内部 assistant 文本污染" not in str(await archive.rag_records())

    context = await archive.context_around(target_id, before=1, after=5)
    assert [record["content"] for record in context] == [
        "同会话前文",
        "目标茶会记录",
        "同会话后文",
    ]

    ctx = ToolContext(archive=archive)
    executor = ToolRegistry(get_default_specs()).get_executor(ctx)
    recalled = await executor(
        "recall_history",
        {"archive_ids": [target_id], "context_before": 1, "context_after": 5},
    )
    assert recalled["count"] == 3
    assert "目标茶会记录" in recalled["content"]
    assert "delete memory result" not in recalled["content"]
    assert "task_context" not in recalled["content"]
    assert "旧库内部 assistant 文本污染" not in recalled["content"]

@pytest.mark.asyncio
async def test_recall_history_filters_active_runtime_and_derives_sent_outbound(tmp_path):
    archive = ArchiveStore(tmp_path / "archive.sqlite3")
    await archive.load()
    history = _HistoryStub(
        [
            _chat_record(
                "活跃真实入站 KEEP",
                conversation_id="private:1",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _send_tool_result_record(
                "活跃机器人已发送 KEEP",
                conversation_id="private:1",
                msg_id="bot-active",
            ),
            *_runtime_records("private:1"),
        ]
    )
    ctx = ToolContext(archive=archive, history=history)
    executor = ToolRegistry(get_default_specs()).get_executor(ctx)

    recalled = await executor(
        "recall_history",
        {"conversation_id": "private:1", "keyword": "KEEP", "limit": 10},
    )

    assert recalled["count"] == 2
    assert "活跃真实入站 KEEP" in recalled["content"]
    assert "活跃机器人已发送 KEEP" in recalled["content"]
    assert all(result["role"] in {"user", "assistant"} for result in recalled["results"])
    assert "delete memory result" not in recalled["content"]
    assert "task_context" not in recalled["content"]
    assert "send_status" not in recalled["content"]
    assert "send_receipt" not in recalled["content"]

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
