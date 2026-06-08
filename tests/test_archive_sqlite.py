from __future__ import annotations

import json
import sqlite3

import pytest

from memory import ArchiveStore
from tools import ToolContext, ToolRegistry, get_default_specs


def _approve_stub_tools(ctx: ToolContext, *names: str) -> None:
    approved = ctx.extras.setdefault("tool_search_approved_tools", set())
    approved.update(names)


class _HistoryStub:
    def __init__(self, records: list[dict]) -> None:
        self._records = records

    async def records(self) -> list[dict]:
        return list(self._records)


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


def _send_tool_result_record(
    content: str,
    *,
    conversation_id: str,
    msg_id: str,
    timestamp: str = "2026-06-01 10:01:00",
) -> dict:
    return {
        "role": "tool",
        "tool_call_id": "tc-send",
        "content": json.dumps(
            {
                "ok": True,
                "status": "sent",
                "qq_visible": True,
                "send_id": "send-1",
                "count": 1,
                "sent": [
                    {
                        "conversation_id": conversation_id,
                        "target_type": conversation_id.split(":", 1)[0],
                        "target_id": conversation_id.split(":", 1)[1],
                        "msg_id": msg_id,
                        "content": content,
                        "time": timestamp,
                        "qq_visible": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        "conversation_id": conversation_id,
    }


def _runtime_records(conversation_id: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc-send",
                    "type": "function",
                    "function": {"name": "send_group_message", "arguments": "{}"},
                }
            ],
            "conversation_id": conversation_id,
        },
        {
            "role": "tool",
            "tool_call_id": "tc-memory",
            "content": json.dumps(
                {"ok": True, "status": "done", "brief": "delete memory result"},
                ensure_ascii=False,
            ),
            "conversation_id": conversation_id,
        },
        {
            "role": "user",
            "content": "<task_context>task_context 不该归档</task_context>",
            "conversation_id": conversation_id,
            "metadata": {"kind": "task_context_snapshot"},
        },
        {
            "role": "user",
            "content": "<send_status>send_status 不该归档</send_status>",
            "conversation_id": conversation_id,
            "metadata": {"kind": "send_done_snapshot"},
        },
        {
            "role": "user",
            "content": "<send_receipt>{\"interrupted\": true}</send_receipt>",
            "conversation_id": conversation_id,
        },
        {
            "role": "assistant",
            "content": "思考过程不该归档",
            "reasoning_content": "内部推理",
            "conversation_id": conversation_id,
        },
        {
            "role": "assistant",
            "content": "内部最终说明不该归档",
            "conversation_id": conversation_id,
        },
        {
            "role": "system",
            "content": "系统消息不该归档",
            "conversation_id": conversation_id,
        },
    ]


def _insert_legacy_archive_row(
    archive: ArchiveStore,
    *,
    rowid: int,
    archive_id: str,
    role: str,
    content: str,
    conversation_id: str,
    direction: str = "runtime",
    message_kind: str = "runtime",
    metadata: dict | None = None,
) -> None:
    metadata = dict(metadata or {})
    record = {
        "role": role,
        "content": content,
        "conversation_id": conversation_id,
        "metadata": metadata,
    }
    with sqlite3.connect(archive.path) as conn:
        conn.execute(
            """
            INSERT INTO archive_messages (
                rowid, archive_id, timestamp, timestamp_unix, date_key, month_key,
                conversation_id, conversation_type, target_id, sender_id,
                sender_name, sender_role, direction, message_kind, content,
                content_search, original_msg_id, reply_to_msg_id, metadata_json,
                record_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rowid,
                archive_id,
                metadata.get("timestamp"),
                None,
                None,
                None,
                conversation_id,
                conversation_id.split(":", 1)[0],
                conversation_id.split(":", 1)[1],
                role,
                role,
                role,
                direction,
                message_kind,
                content,
                content,
                None,
                None,
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(record, ensure_ascii=False),
                "2026-06-01 10:00:00",
            ),
        )
        conn.commit()


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
