from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memory.diana_db import DianaDB
from memory.diana_stores import DianaArchiveStore


@pytest.mark.asyncio
async def test_diana_archive_store_starts_empty_rejects_empty_persona_and_exports(tmp_path):
    store = DianaArchiveStore(tmp_path / "diana.db", " yuexi ")

    assert await store.load() == []
    assert await store.records() == []
    assert await store.media_records() == []

    with pytest.raises(ValueError, match="persona_id must not be empty"):
        DianaArchiveStore(tmp_path / "diana.db", "  ")

    from memory import DianaArchiveStore as PackageDianaArchiveStore

    assert PackageDianaArchiveStore is DianaArchiveStore


@pytest.mark.asyncio
async def test_diana_archive_append_many_keeps_real_chat_and_sent_outbound_only(tmp_path):
    store = DianaArchiveStore(tmp_path / "diana.db", "yuexi")

    await store.append_many(
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
                timestamp="2026-06-01 10:01:00",
            ),
            *_runtime_records("group:2"),
        ]
    )

    records = await store.records()
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
async def test_diana_archive_persona_scopes_short_ids_and_queries(tmp_path):
    db_path = tmp_path / "diana.db"
    yuexi = DianaArchiveStore(db_path, "yuexi")
    jiu = DianaArchiveStore(db_path, "jiu")

    await yuexi.append_many(
        [
            _chat_record(
                "第一条",
                conversation_id="private:1",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "第二条",
                conversation_id="private:1",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:01:00",
            ),
            _chat_record(
                "第三条别的会话 [文件 workspace=docs/a.txt name=a.txt]",
                conversation_id="group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-01 10:02:00",
            ),
        ]
    )
    await jiu.append_many(
        [
            _chat_record(
                "九的第一条 [图片 workspace=incoming/jiu.jpg]",
                conversation_id="private:1",
                sender_id="9",
                sender_name="Jiu",
                timestamp="2026-06-01 10:00:00",
            )
        ]
    )

    assert [record["archive_id"] for record in await yuexi.records()] == ["a1", "a2", "a3"]
    assert [record["archive_id"] for record in await jiu.records()] == ["a1"]
    assert [row["archive_id"] for row in _archive_rows(db_path, "yuexi")] == [
        "a1",
        "a2",
        "a3",
    ]
    assert [row["archive_id"] for row in _archive_rows(db_path, "jiu")] == ["a1"]

    yuexi_search = await yuexi.search(conversation_id="private:1", keyword="第二", limit=10)
    assert [record["content"] for record in yuexi_search] == ["第二条"]
    assert await jiu.search(keyword="第二", limit=10) == []

    filtered = await yuexi.filter_records(
        {
            "conversation_ids": ["private:1"],
            "keywords": ["条"],
            "limit": 10,
            "order": "asc",
        }
    )
    assert filtered["total"] == 2
    assert [item["id"] for item in filtered["results"]] == ["a1", "a2"]

    assert [record["content"] for record in await yuexi.get_by_ids(["a2", "a1"])] == [
        "第二条",
        "第一条",
    ]
    assert await jiu.get_by_ids(["a2"]) == []

    context = await yuexi.context_around("a2", before=1, after=1)
    assert [record["content"] for record in context] == ["第一条", "第二条"]
    assert await jiu.context_around("a2", before=1, after=1) == []

    rag = await yuexi.rag_records()
    assert [record["archive_id"] for record in rag] == ["a1", "a2", "a3"]
    assert "workspace=docs/a.txt" in rag[2]["content"]
    assert "jiu" not in str(rag).lower()

    assert [item["id"] for item in await yuexi.media_records()] == [1]
    assert [item["archive_id"] for item in await yuexi.media_records()] == ["a3"]
    assert [item["id"] for item in await jiu.media_records()] == [1]
    assert [item["archive_id"] for item in await jiu.media_records()] == ["a1"]
    assert await yuexi.media_records("a1") == []
    assert (await yuexi.media_records("a3"))[0]["workspace_path"] == "docs/a.txt"


@pytest.mark.asyncio
async def test_diana_archive_duplicate_record_json_is_scoped_by_persona(tmp_path):
    db_path = tmp_path / "diana.db"
    yuexi = DianaArchiveStore(db_path, "yuexi")
    jiu = DianaArchiveStore(db_path, "jiu")
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

    await yuexi.append_many([record, record])
    await yuexi.append_many([record, next_record])
    await jiu.append_many([record])

    assert [item["archive_id"] for item in await yuexi.records()] == ["a1", "a2"]
    assert [item["content"] for item in await yuexi.records()] == [
        "重复图片 [图片 workspace=incoming/dup.jpg]",
        "重复后新增",
    ]
    assert [item["archive_id"] for item in await jiu.records()] == ["a1"]
    assert [row["record_json"] for row in _archive_rows(db_path, "yuexi")].count(
        _archive_rows(db_path, "jiu")[0]["record_json"]
    ) == 1


@pytest.mark.asyncio
async def test_diana_archive_filter_fallback_and_media_are_persona_isolated(tmp_path):
    db_path = tmp_path / "diana.db"
    yuexi = DianaArchiveStore(db_path, "yuexi")
    jiu = DianaArchiveStore(db_path, "jiu")

    await yuexi.append_many(
        [
            _chat_record(
                "KEEP 一",
                conversation_id="group:2",
                sender_id="1",
                sender_name="Alice",
                timestamp="2026-06-01 10:00:00",
            ),
            _chat_record(
                "KEEP 二 [图片 workspace=incoming/yuexi.jpg]",
                conversation_id="Group:2",
                sender_id="2",
                sender_name="Bob",
                timestamp="2026-06-01 11:00:00",
            ),
            _chat_record(
                "不应命中会话",
                conversation_id="group:3",
                sender_id="3",
                sender_name="Carol",
                timestamp="2026-06-01 12:00:00",
            ),
        ]
    )
    await jiu.append_many(
        [
            _chat_record(
                "KEEP 其他人格 [图片 workspace=incoming/jiu.jpg]",
                conversation_id="group:2",
                sender_id="9",
                sender_name="Jiu",
                timestamp="2026-06-01 10:00:00",
            )
        ]
    )

    result = await yuexi.filter_records(
        {
            "conversation_ids": ["group:2"],
            "keywords": ["KEEP"],
            "limit": 10,
            "order": "asc",
        }
    )

    assert result["total"] == 2
    assert [item["content"] for item in result["results"]] == [
        "KEEP 一",
        "KEEP 二 [图片 workspace=incoming/yuexi.jpg]",
    ]
    assert "其他人格" not in str(result)
    assert [item["workspace_path"] for item in await yuexi.media_records()] == [
        "incoming/yuexi.jpg"
    ]
    assert [item["workspace_path"] for item in await jiu.media_records()] == [
        "incoming/jiu.jpg"
    ]


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
                    "group_id": target_id if scope.lower() == "group" else None,
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
                    },
                    {
                        "conversation_id": conversation_id,
                        "msg_id": "hidden",
                        "content": "未证明 outbound",
                        "time": timestamp,
                        "qq_visible": False,
                    },
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


def _archive_rows(db_path: Path, persona_id: str) -> list[sqlite3.Row]:
    db = DianaDB(db_path)
    try:
        db.load()
        rows = db.connect().execute(
            """
            SELECT persona_id, rowid, archive_id, record_json
            FROM archive_messages
            WHERE persona_id = ?
            ORDER BY rowid ASC
            """,
            (persona_id,),
        ).fetchall()
        return list(rows)
    finally:
        db.close()
