from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import pytest

from memory import EventStore, HistoryManager


class FailingEventStore:
    def __init__(self) -> None:
        self.appended_batches: list[list[dict]] = []
        self.appended_events: list[dict] = []

    async def append_events(self, events: list[dict]) -> list[int]:
        self.appended_batches.append(events)
        raise RuntimeError("event store failed")

    async def append_event(self, **event: dict) -> int:
        self.appended_events.append(event)
        raise RuntimeError("event store failed")


def _read_jsonl(path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_history_event_store_success_writes_full_jsonl_and_full_mirror(
    tmp_path,
):
    history_path = tmp_path / "history.jsonl"
    event_store = EventStore(tmp_path / "events.sqlite3")
    history = HistoryManager(history_path, event_store=event_store)

    records = [
        {
            "role": "user",
            "content": "你好",
            "metadata": {"scope": "group", "target_id": "42", "secret": "raw-meta"},
            "conversation_id": "group:42",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "send", "arguments": "{}"},
                }
            ],
            "reasoning_content": "思考过程",
            "conversation_id": "group:42",
        },
    ]

    await history.add_records(records)

    assert await history.records() == records
    raw_entries = _read_jsonl(history_path)
    assert raw_entries == records
    raw_jsonl = history_path.read_text(encoding="utf-8")
    assert "content" in raw_jsonl
    assert "tool_calls" in raw_jsonl
    assert "reasoning_content" in raw_jsonl
    assert "metadata" in raw_jsonl

    assert await event_store.wait_projected(timeout=1.0)
    events = await event_store.get_events([2, 999, 1])
    assert [event["event_id"] if event else None for event in events] == [2, None, 1]
    first_payload = events[2]["payload"]
    second_payload = events[0]["payload"]
    assert first_payload["record"] == records[0]
    assert second_payload["record"] == records[1]
    assert first_payload["history_index"] == 0
    assert first_payload["content_length"] == len("你好")
    assert first_payload["content_hash"] == _content_hash("你好")
    assert first_payload["record_keys"] == [
        "role",
        "content",
        "metadata",
        "conversation_id",
    ]
    assert second_payload["batch_index"] == 1
    assert second_payload["conversation_id"] == "group:42"
    assert second_payload["tool_call_ids"] == ["call-1"]
    assert second_payload["record_keys"] == [
        "role",
        "content",
        "tool_calls",
        "reasoning_content",
        "conversation_id",
    ]


@pytest.mark.asyncio
async def test_history_load_records_and_get_slice_return_jsonl_append_order(tmp_path):
    history_path = tmp_path / "history.jsonl"
    event_store = EventStore(tmp_path / "events.sqlite3")
    history = HistoryManager(history_path, event_store=event_store)

    await history.add_user_message("u1", conversation_id="private:1")
    await history.add_assistant_message(
        "a1",
        reasoning_content="",
        conversation_id="private:1",
    )
    await history.add_tool_result("call-1", '{"ok": true}', conversation_id="private:1")

    expected = [
        {"role": "user", "content": "u1", "conversation_id": "private:1"},
        {
            "role": "assistant",
            "content": "a1",
            "reasoning_content": "",
            "conversation_id": "private:1",
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"ok": true}',
            "conversation_id": "private:1",
        },
    ]
    reloaded = HistoryManager(history_path, event_store=event_store)

    assert _read_jsonl(history_path) == expected
    assert await reloaded.load(force_reload=True) == expected
    assert await reloaded.records() == expected
    assert await reloaded.get_slice(1, 3) == expected[1:3]


@pytest.mark.asyncio
async def test_history_legacy_raw_jsonl_remains_compatible_with_event_store(tmp_path):
    history_path = tmp_path / "history.jsonl"
    legacy_records = [
        {"role": "user", "content": "legacy-user"},
        {"role": "assistant", "content": "legacy-assistant", "reasoning_content": ""},
    ]
    history_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in legacy_records),
        encoding="utf-8",
    )

    history = HistoryManager(
        history_path,
        event_store=EventStore(tmp_path / "events.sqlite3"),
    )

    assert await history.load(force_reload=True) == legacy_records
    assert await history.records() == legacy_records
    assert await history.get_slice(1) == legacy_records[1:]


@pytest.mark.asyncio
async def test_history_event_store_failure_keeps_full_jsonl_without_losing_context(
    tmp_path,
    caplog,
):
    history_path = tmp_path / "history.jsonl"
    event_store = FailingEventStore()
    history = HistoryManager(history_path, event_store=event_store)  # type: ignore[arg-type]
    called = asyncio.Event()
    received: list[list[dict]] = []

    async def on_append(records: list[dict]) -> None:
        received.append(records)
        called.set()

    records = [
        {"role": "user", "content": "a", "conversation_id": "group:1"},
        {"role": "assistant", "content": "b", "conversation_id": "group:1"},
    ]

    history.on_append(on_append)
    with caplog.at_level(logging.WARNING, logger="memory.history"):
        await history.add_records(records)

    await asyncio.wait_for(called.wait(), timeout=1.0)

    assert await history.records() == records
    assert _read_jsonl(history_path) == records
    assert received == [records]
    assert len(event_store.appended_batches) == 1
    assert "镜像失败" in caplog.text
    assert "已保留完整 JSONL" in caplog.text
    assert "event store failed" in caplog.text


@pytest.mark.asyncio
async def test_history_truncate_rewrites_full_jsonl_and_mirrors_truncation(
    tmp_path,
):
    history_path = tmp_path / "history.jsonl"
    event_store = EventStore(tmp_path / "events.sqlite3")
    history = HistoryManager(history_path, event_store=event_store)

    await history.add_records(
        [
            {"role": "user", "content": "a", "conversation_id": "private:1"},
            {"role": "assistant", "content": "b", "conversation_id": "private:1"},
            {"role": "user", "content": "c", "conversation_id": "private:1"},
        ]
    )

    remaining = await history.truncate_head(2)

    assert remaining == 1
    assert await history.length() == 1
    assert await history.records() == [
        {"role": "user", "content": "c", "conversation_id": "private:1"}
    ]
    assert _read_jsonl(history_path) == [
        {"role": "user", "content": "c", "conversation_id": "private:1"}
    ]

    assert await event_store.wait_projected(timeout=1.0)
    events = await event_store.iter_events(limit=10)
    assert [event["event_type"] for event in events] == [
        "history_record_appended",
        "history_record_appended",
        "history_record_appended",
        "history_truncated",
    ]
    assert [event["payload"]["role"] for event in events[:3]] == [
        "user",
        "assistant",
        "user",
    ]
    assert [event["payload"]["content_hash"] for event in events[:3]] == [
        _content_hash("a"),
        _content_hash("b"),
        _content_hash("c"),
    ]
    assert [event["payload"]["record"] for event in events[:3]] == [
        {"role": "user", "content": "a", "conversation_id": "private:1"},
        {"role": "assistant", "content": "b", "conversation_id": "private:1"},
        {"role": "user", "content": "c", "conversation_id": "private:1"},
    ]
    assert events[-1]["payload"] == {"cut_point": 2, "remaining_count": 1}


@pytest.mark.asyncio
async def test_history_on_append_receives_full_records_when_event_store_enabled(tmp_path):
    event_store = EventStore(tmp_path / "events.sqlite3")
    history = HistoryManager(tmp_path / "history.jsonl", event_store=event_store)
    called = asyncio.Event()
    received: list[list[dict]] = []

    async def on_append(records: list[dict]) -> None:
        received.append(records)
        called.set()

    record = {"role": "tool", "tool_call_id": "call-1", "content": "{}"}
    history.on_append(on_append)
    await history.add_records([record])
    await asyncio.wait_for(called.wait(), timeout=1.0)

    assert received == [[record]]
    assert "event_type" not in received[0][0]


@pytest.mark.asyncio
async def test_history_add_system_note_writes_full_jsonl_and_full_events(tmp_path):
    history_path = tmp_path / "history.jsonl"
    event_store = EventStore(tmp_path / "events.sqlite3")
    history = HistoryManager(history_path, event_store=event_store)
    content = "系统注解正文-" + "密" * 120

    await history.add_system_note(content, conversation_id="private:123")

    expected = [{"role": "system", "content": content, "conversation_id": "private:123"}]
    assert await history.records() == expected
    assert _read_jsonl(history_path) == expected
    assert "content" in history_path.read_text(encoding="utf-8")

    assert await event_store.wait_projected(timeout=1.0)
    events = await event_store.iter_events(limit=10)
    assert [event["event_type"] for event in events] == [
        "history_record_appended",
        "system_note_recorded",
    ]
    assert events[0]["payload"]["record"] == expected[0]
    assert events[0]["payload"]["record"]["content"] == content
    assert events[0]["payload"]["content_length"] == len(content)
    note_payload = events[1]["payload"]
    assert note_payload["role"] == "system"
    assert note_payload["history_index"] == 0
    assert note_payload["conversation_id"] == "private:123"
    assert note_payload["content"] == content
    assert note_payload["record"] == expected[0]
    assert note_payload["content_length"] == len(content)
    assert "preview" not in note_payload
