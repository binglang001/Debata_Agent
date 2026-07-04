from __future__ import annotations

import json
import sqlite3

import pytest

from memory import ArchiveStore
from tools import ToolContext


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


class _SqlSpyCursor:
    def __init__(self, cursor, entry: dict) -> None:
        self._cursor = cursor
        self._entry = entry

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._entry["row_count"] = len(rows)
        return rows

    def fetchone(self):
        return self._cursor.fetchone()

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _SqlSpyConnection:
    def __init__(self, conn, log: list[dict]) -> None:
        self._conn = conn
        self._log = log

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._conn.__exit__(exc_type, exc, tb)

    def execute(self, sql: str, params=()):
        entry = {
            "sql": " ".join(sql.split()),
            "params": tuple(params or ()),
            "row_count": None,
        }
        self._log.append(entry)
        return _SqlSpyCursor(self._conn.execute(sql, params), entry)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _spy_archive_sql(archive: ArchiveStore, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    log: list[dict] = []
    original_connect = archive._store._connect

    def connect():
        return _SqlSpyConnection(original_connect(), log)

    monkeypatch.setattr(archive._store, "_connect", connect)
    return log
