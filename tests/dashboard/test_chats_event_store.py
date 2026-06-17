"""聊天页事件存储合并与投影回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from core.chat_timeline import ChatTimelineStore
from ui.dashboard.chats_page import (
    EVENT_STORE_CHAT_PAGE_EVENT_TYPES,
    _load_chat_page_records,
    normalize_history_records,
)

from tests.test_dashboard_p2 import (
    _FakeEventStore,
    _StaticRecordStore,
    _dashboard_runtime,
    _qq_event,
    _runtime_event,
    _timeline_record,
)

@pytest.mark.asyncio
async def test_chats_loads_event_store_qq_records_before_timeline_and_archive_duplicates(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                1,
                "qq_message_received",
                content="EventStore 入站",
                msg_id="same-in",
            ),
            _qq_event(
                2,
                "qq_message_sent",
                content="EventStore 出站",
                msg_id="same-out",
            ),
        ]
    )
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="timeline 重复入站",
            msg_id="same-in",
            timestamp=1_780_000_011.0,
            time_text="2026-06-08 23:07:21",
        )
    )
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="timeline 新消息",
            msg_id="timeline-only",
            timestamp=1_780_000_012.0,
            time_text="2026-06-08 23:07:22",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _StaticRecordStore(
        [
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "archive 重复入站",
                "msg_id": "same-in",
            },
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "archive 旧消息",
                "msg_id": "archive-only",
            },
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {"role": "user", "content": "history 普通聊天不应作为气泡主来源"},
            {"role": "system", "content": "系统补充"},
        ]
    )

    records = await _load_chat_page_records(rt)

    assert [call["event_type"] for call in rt.event_store.calls] == list(
        EVENT_STORE_CHAT_PAGE_EVENT_TYPES
    )
    assert all(call["limit"] > 0 and call["order"] == "desc" for call in rt.event_store.calls)
    assert rt.event_store.wait_projected_calls == []
    by_msg_id = {
        item.get("msg_id"): item
        for item in records
        if item.get("msg_id")
    }
    assert by_msg_id["same-in"]["_source"] == "event_store"
    assert by_msg_id["same-in"]["content"] == "EventStore 入站"
    assert by_msg_id["same-out"]["_source"] == "event_store"
    assert by_msg_id["timeline-only"]["_source"] == "chat_timeline"
    assert by_msg_id["archive-only"]["content"] == "archive 旧消息"
    assert "archive 重复入站" not in [item["content"] for item in records]
    assert "history 普通聊天不应作为气泡主来源" not in [item["content"] for item in records]
    assert "系统补充" in [item["content"] for item in records]

@pytest.mark.asyncio
async def test_chats_mixed_sources_sort_by_layer_not_event_id_as_timestamp(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                1,
                "qq_message_received",
                content="EventStore 已投影",
                msg_id="event-only",
                timestamp_unix=1_780_000_021.0,
            ),
        ]
    )
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="timeline pending",
            msg_id="timeline-only",
            timestamp=1_780_000_022.0,
            time_text="2026-06-08 23:07:22",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _StaticRecordStore(
        [
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "archive cold",
                "msg_id": "archive-only",
                "timestamp": "2026-06-08 23:07:20",
            },
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "role": "system",
                "conversation_id": "private:10001",
                "content": "history fallback without timestamp",
            },
        ]
    )

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [item.text for item in items] == [
        "archive cold",
        "EventStore 已投影",
        "timeline pending",
        "history fallback without timestamp",
    ]
    event_record = next(item for item in records if item.get("_source") == "event_store")
    assert event_record["_sort_layer"] == "event_store"
    assert event_record["_sort_kind"] == "event_id"
    assert event_record["_sort_value"] == 1.0
    assert "_sort_ts" not in event_record
    assert rt.event_store.wait_projected_calls == []

@pytest.mark.asyncio
async def test_chats_event_store_failure_falls_back_to_existing_sources(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(fail=True)
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="实时 fallback",
            msg_id="tl-fallback",
            timestamp=1_780_000_001.0,
            time_text="2026-06-08 23:07:19",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _StaticRecordStore(
        [
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "归档 fallback",
                "msg_id": "arch-fallback",
            }
        ]
    )
    rt.history = _StaticRecordStore([{"role": "system", "content": "系统 fallback"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == [
        "归档 fallback",
        "实时 fallback",
        "系统 fallback",
    ]

@pytest.mark.asyncio
async def test_chats_event_store_ignores_non_qq_events(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            {
                "event_id": 1,
                "event_type": "history_record_appended",
                "conversation_id": "private:10001",
                "payload": {"content": "不应投影"},
            },
            _qq_event(
                2,
                "qq_message_received",
                content="应投影",
                msg_id="visible-in",
            ),
        ],
        include_mismatched=True,
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["应投影"]
    assert records[0]["event_id"] == 2

@pytest.mark.asyncio
async def test_chats_event_store_projects_received_and_sent_fields_by_event_id_order(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                2,
                "qq_message_sent",
                conversation_id="private:10001",
                content="后发送",
                msg_id="out-1",
                timestamp_unix=100.0,
                payload={"target_scope": "private", "target_id": "10001", "self_id": "999"},
            ),
            _qq_event(
                1,
                "qq_message_received",
                conversation_id="group:20002",
                content="先收到",
                msg_id="in-1",
                timestamp_unix=200.0,
                payload={
                    "group_id": "20002",
                    "user_id": "10001",
                    "sender_name": "Alice",
                    "target_id": "20002",
                    "self_id": "999",
                },
            ),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["先收到", "后发送"]
    received, sent = records
    assert received["id"] == "event:1"
    assert received["event_id"] == 1
    assert received["role"] == "user"
    assert received["direction"] == "inbound"
    assert received["qq_visible"] is True
    assert received["conversation_id"] == "group:20002"
    assert received["msg_id"] == "in-1"
    assert received["_source"] == "event_store"
    assert received["sender_name"] == "Alice"
    assert received["user_id"] == "10001"
    assert received["target_id"] == "20002"
    assert received["group_id"] == "20002"
    assert received["self_id"] == "999"
    assert received["_sort_layer"] == "event_store"
    assert received["_sort_kind"] == "event_id"
    assert received["_sort_value"] == 1.0
    assert "_sort_ts" not in received
    assert sent["id"] == "event:2"
    assert sent["role"] == "assistant"
    assert sent["direction"] == "outbound"
    assert sent["conversation_id"] == "private:10001"
    assert sent["msg_id"] == "out-1"
    assert sent["target_id"] == "10001"
    assert sent["self_id"] == "999"

@pytest.mark.asyncio
async def test_chats_event_store_runtime_events_interleave_with_qq_by_event_id(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(1, "qq_message_received", content="用户先说", msg_id="in-1"),
            _runtime_event(
                2,
                "tool_call_started",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-write",
                    "raw_arguments": {"path": "result.md", "content": "完整参数内容"},
                    "args_keys": ["content", "path"],
                    "args_length": 42,
                    "args_preview": '{"path":"result.md"}',
                    "loop": 1,
                    "step": 1,
                },
                tool_call_id="tc-write",
            ),
            _qq_event(3, "qq_message_received", content="用户中途补充", msg_id="in-2"),
            _runtime_event(
                4,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-write",
                    "ok": True,
                    "result": {"ok": True, "path": "result.md", "content": "完整工具返回内容"},
                    "result_keys": ["ok", "path"],
                    "result_length": 64,
                    "result_hash": "a" * 64,
                    "result_preview": '{"ok":true}',
                },
                tool_call_id="tc-write",
            ),
            _qq_event(5, "qq_message_sent", content="随后回复", msg_id="out-1"),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [(item.get("event_id"), item.get("event_type")) for item in records] == [
        (1, "qq_message_received"),
        (2, "tool_call_started"),
        (3, "qq_message_received"),
        (4, "tool_result_received"),
        (5, "qq_message_sent"),
    ]
    items = normalize_history_records(records, persona_name="Debata")
    assert [item.kind for item in items] == [
        "inbound_message",
        "tool_call",
        "inbound_message",
        "tool_result",
        "outbound_message",
    ]
    tool_item = items[1]
    result_item = items[3]
    assert tool_item.related_tool_call_id == "tc-write"
    assert tool_item.tool_results == []
    assert result_item.related_tool_call_id == "tc-write"
    assert "write_file" in tool_item.summary
    assert "工具调用：写入文件" in tool_item.text
    assert "路径：result.md" in tool_item.text
    assert "内容：完整参数内容" in tool_item.text
    assert "完整参数内容" in tool_item.text
    assert '"content": "完整参数内容"' in tool_item.raw["tool_call"]["function"]["arguments"]
    assert items[2].text == "用户中途补充"
    assert "工具返回：写入文件" in result_item.text
    assert "成功" in result_item.text
    assert "路径：result.md" in result_item.text
    assert "内容：完整工具返回内容" in result_item.text
    assert "完整工具返回内容" in result_item.text
    assert '"content":' not in tool_item.text
    assert '"tool_name"' not in tool_item.text
    assert '"content":' not in result_item.text
    assert "result_hash" not in result_item.text
    assert result_item.raw["metadata"]["event_payload"]["result_hash"] == "a" * 64

@pytest.mark.asyncio
async def test_chats_event_store_runtime_send_and_system_events_share_event_id_axis(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(1, "qq_message_received", content="用户先说", msg_id="in-1"),
            _runtime_event(
                2,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-axis"},
                tool_call_id="tc-axis",
            ),
            _qq_event(3, "qq_message_received", content="用户补充", msg_id="in-2"),
            _runtime_event(
                4,
                "send_message_started",
                payload={
                    "send_id": "send-axis",
                    "status": "started",
                    "order": 0,
                    "target_conversation_id": "private:10001",
                    "content_length": 6,
                },
                external_id="send-axis",
            ),
            _runtime_event(
                5,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-axis",
                    "ok": True,
                    "result_hash": "e" * 64,
                },
                tool_call_id="tc-axis",
            ),
            {
                "event_id": 6,
                "event_type": "history_truncated",
                "source": "fake",
                "timestamp_unix": 1_780_000_006.0,
                "payload": {"cut_point": 120, "remaining_count": 80},
            },
            _qq_event(7, "qq_message_sent", content="最终回复", msg_id="out-1"),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [(item.get("event_id"), item.get("event_type")) for item in records] == [
        (1, "qq_message_received"),
        (2, "tool_call_started"),
        (3, "qq_message_received"),
        (4, "send_message_started"),
        (5, "tool_result_received"),
        (6, "history_truncated"),
        (7, "qq_message_sent"),
    ]
    assert [item.kind for item in items] == [
        "inbound_message",
        "tool_call",
        "inbound_message",
        "system_event",
        "tool_result",
        "system_event",
        "outbound_message",
    ]
    assert all(record["_sort_layer"] == "event_store" for record in records)
    assert all(record["_sort_kind"] == "event_id" for record in records)
    assert [record["_sort_value"] for record in records] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    assert "发送消息开始" in items[3].summary
    assert "历史截断" in items[5].summary
    assert "截断点 120" in items[5].text
    assert rt.event_store.wait_projected_calls == []

@pytest.mark.asyncio
async def test_chats_event_store_runtime_events_dedupe_semantic_duplicates(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-dup"},
                tool_call_id="tc-dup",
            ),
            _runtime_event(
                2,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-dup"},
                tool_call_id="tc-dup",
            ),
            _runtime_event(
                3,
                "send_batch_accepted",
                payload={"send_id": "send-dup", "status": "accepted"},
                external_id="send-dup",
            ),
            _runtime_event(
                4,
                "send_batch_accepted",
                payload={"send_id": "send-dup", "status": "accepted"},
                external_id="send-dup",
            ),
            _runtime_event(
                5,
                "send_message_started",
                payload={
                    "send_id": "send-dup",
                    "status": "started",
                    "order": 0,
                    "target_conversation_id": "private:10001",
                },
                external_id="send-dup",
            ),
            _runtime_event(
                6,
                "send_message_started",
                payload={
                    "send_id": "send-dup",
                    "status": "started",
                    "order": 0,
                    "target_conversation_id": "private:10001",
                },
                external_id="send-dup",
            ),
            _runtime_event(
                7,
                "send_message_started",
                payload={
                    "send_id": "send-dup",
                    "status": "started",
                    "order": 1,
                    "target_conversation_id": "private:10001",
                },
                external_id="send-dup",
            ),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [(record["event_id"], record["event_type"]) for record in records] == [
        (1, "tool_call_started"),
        (3, "send_batch_accepted"),
        (5, "send_message_started"),
        (7, "send_message_started"),
    ]

@pytest.mark.asyncio
async def test_chats_event_store_send_and_system_runtime_events_are_displayable(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "send_batch_accepted",
                payload={
                    "send_id": "send-1",
                    "status": "accepted",
                    "source_tool": "send_private_messages",
                    "counts": {"messages": 2, "conversations": 1},
                    "conversation_ids": ["private:10001"],
                },
                external_id="send-1",
            ),
            _runtime_event(
                2,
                "send_message_succeeded",
                payload={
                    "send_id": "send-1",
                    "status": "succeeded",
                    "msg_id": "msg-1",
                    "target_conversation_id": "private:10001",
                    "content_length": 12,
                    "content_hash": "b" * 64,
                },
                external_id="send-1",
            ),
            _runtime_event(
                3,
                "system_note_recorded",
                payload={
                    "role": "system",
                    "conversation_id": "private:10001",
                    "record": {"content": "完整系统提示内容"},
                    "content_length": 18,
                    "content_hash": "c" * 64,
                    "preview": "系统提示预览",
                },
            ),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [item.kind for item in items] == ["system_event", "system_event", "system_event"]
    detail = "\n".join(item.text for item in items)
    assert "发送 ID send-1" in detail
    assert "消息 ID msg-1" in detail
    assert "counts messages=2, conversations=1" in detail
    assert "内容长度 12" in detail
    assert "内容hash=" + "b" * 64 in detail
    assert "完整系统提示内容" in detail
    assert "预览：系统提示预览" in detail
    assert all(item.kind not in {"inbound_message", "outbound_message"} for item in items)

@pytest.mark.asyncio
async def test_chats_event_store_runtime_events_do_not_backfill_from_history(tmp_paths):
    system_content = "系统补充"
    system_hash = hashlib.sha256(system_content.encode("utf-8")).hexdigest()
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-1",
                    "args": {"path": "event.md"},
                },
                tool_call_id="tc-1",
            ),
            _runtime_event(
                2,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-1",
                    "ok": True,
                    "result": {"ok": True, "content": "EventStore 完整工具返回"},
                    "result_hash": "d" * 64,
                },
                tool_call_id="tc-1",
            ),
            _runtime_event(
                3,
                "send_receipt_recorded",
                payload={"send_id": "send-1", "status": "succeeded"},
                external_id="send-1",
            ),
            _runtime_event(
                4,
                "system_note_recorded",
                payload={
                    "conversation_id": "private:10001",
                    "content_length": len(system_content),
                    "content_hash": system_hash,
                    "content": system_content,
                    "preview": system_content,
                },
            ),
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "role": "assistant",
                "content": "",
                "conversation_id": "private:10001",
                "tool_calls": [
                    {
                        "id": "tc-1",
                        "function": {"name": "write_file", "arguments": '{"path":"full.md"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-1",
                "conversation_id": "private:10001",
                "content": '{"ok":true,"content":"完整工具返回"}',
            },
            {
                "role": "system",
                "conversation_id": "private:10001",
                "content": '<send_receipt>\n{"send_id":"send-1","status":"succeeded"}\n</send_receipt>',
            },
            {"role": "system", "conversation_id": "private:10001", "content": system_content},
            {"role": "system", "conversation_id": "private:10001", "content": "旧 history fallback"},
        ]
    )

    records = await _load_chat_page_records(rt)

    blob = json.dumps(records, ensure_ascii=False)
    assert "EventStore 完整工具返回" in blob
    assert not any(
        record.get("content") == '{"ok":true,"content":"完整工具返回"}'
        for record in records
    )
    assert '{"path":"full.md"}' not in blob
    assert all(record.get("_source") == "event_store" for record in records[:4])
    assert records[-1]["content"] == "旧 history fallback"
    assert records[-1].get("_source") is None

@pytest.mark.asyncio
async def test_chats_event_store_keeps_history_reasoning_when_tool_events_dedupe(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-reasoning",
                    "args": {"path": "event.md"},
                },
                tool_call_id="tc-reasoning",
            ),
            _runtime_event(
                2,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-reasoning",
                    "result": {"ok": True, "content": "EventStore 工具返回"},
                },
                tool_call_id="tc-reasoning",
            ),
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "id": "turn-reasoning",
                "role": "assistant",
                "content": "助手可见正文",
                "reasoning_content": "history 思考过程",
                "conversation_id": "private:10001",
                "direction": "outbound",
                "qq_visible": True,
                "tool_calls": [
                    {
                        "id": "tc-reasoning",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"history.md"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-reasoning",
                "conversation_id": "private:10001",
                "content": '{"ok":true,"content":"history 工具返回"}',
            },
        ]
    )

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    history_records = [record for record in records if record.get("_sort_layer") == "history"]
    assert len(history_records) == 1
    assert history_records[0]["content"] == "助手可见正文"
    assert history_records[0]["reasoning_content"] == "history 思考过程"
    assert "tool_calls" not in history_records[0]
    blob = json.dumps(records, ensure_ascii=False)
    assert "history.md" not in blob
    assert "history 工具返回" not in blob

    assert [item.kind for item in items] == [
        "tool_call",
        "tool_result",
        "reasoning",
        "outbound_message",
    ]
    assert [item.kind for item in items].count("tool_call") == 1
    assert [item.kind for item in items].count("tool_result") == 1
    assert items[0].related_tool_call_id == "tc-reasoning"
    assert items[1].related_tool_call_id == "tc-reasoning"
    assert items[2].text == "history 思考过程"
    assert items[3].text == "助手可见正文"
    assert "event.md" in items[0].text
    assert "EventStore 工具返回" in items[1].text

@pytest.mark.asyncio
async def test_chats_event_store_keeps_reasoning_blocks_when_visible_message_dedupes(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-blocks"},
                tool_call_id="tc-blocks",
            ),
            _qq_event(
                2,
                "qq_message_sent",
                content="EventStore 已发正文",
                msg_id="out-visible",
            ),
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "id": "turn-blocks",
                "role": "assistant",
                "content": "history 重复已发正文",
                "reasoning_blocks": [
                    {"text": "第一段 blocks 思考"},
                    {"content": "第二段 blocks 思考"},
                ],
                "conversation_id": "private:10001",
                "direction": "outbound",
                "qq_visible": True,
                "msg_id": "out-visible",
                "tool_calls": [
                    {
                        "id": "tc-blocks",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"history-blocks.md"}',
                        },
                    }
                ],
            },
        ]
    )

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    history_records = [record for record in records if record.get("_sort_layer") == "history"]
    assert len(history_records) == 1
    assert history_records[0]["content"] == ""
    assert history_records[0]["reasoning_content"] == "第一段 blocks 思考\n第二段 blocks 思考"
    assert "tool_calls" not in history_records[0]
    assert [item.kind for item in items] == ["tool_call", "outbound_message", "reasoning"]
    assert "工具调用：write_file" in items[0].text
    assert "tc-blocks" in items[0].text
    assert "工具调用：写入文件" in items[0].text
    assert '"tool_name": "write_file"' not in items[0].text
    assert items[1].text == "EventStore 已发正文"
    assert items[2].text == "第一段 blocks 思考\n第二段 blocks 思考"

@pytest.mark.asyncio
async def test_chats_event_store_system_note_dedupes_history_record_appended_without_history_backfill(tmp_paths):
    system_content = "完整系统正文只显示一次"
    system_hash = hashlib.sha256(system_content.encode("utf-8")).hexdigest()
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            {
                "event_id": 1,
                "event_type": "history_record_appended",
                "conversation_id": "private:10001",
                "payload": {
                    "role": "system",
                    "conversation_id": "private:10001",
                    "content": system_content,
                    "content_hash": system_hash,
                    "content_length": len(system_content),
                },
            },
            _runtime_event(
                2,
                "system_note_recorded",
                payload={
                    "role": "system",
                    "conversation_id": "private:10001",
                    "record": {"content": system_content},
                    "content_hash": system_hash,
                    "content_length": len(system_content),
                    "preview": "摘要预览",
                },
            ),
            _qq_event(3, "qq_message_received", content="event_id 后续聊天", msg_id="in-after"),
        ],
        include_mismatched=True,
    )
    rt.history = _StaticRecordStore(
        [
            {"role": "system", "conversation_id": "private:10001", "content": system_content},
        ]
    )

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [(record.get("event_id"), record.get("event_type")) for record in records] == [
        (2, "system_note_recorded"),
        (3, "qq_message_received"),
    ]
    assert [item.text for item in items] == [
        "系统消息记录；event_id=2；会话 private:10001；内容长度 11；内容hash="
        + system_hash
        + "；预览：摘要预览；内容：\n完整系统正文只显示一次",
        "event_id 后续聊天",
    ]
    assert "\n".join(item.text for item in items).count(system_content) == 1

@pytest.mark.asyncio
async def test_chats_history_only_tool_records_remain_low_priority_fallback(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [_qq_event(1, "qq_message_received", content="EventStore 新消息", msg_id="event-in")]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "role": "assistant",
                "content": "",
                "conversation_id": "private:10001",
                "tool_calls": [
                    {
                        "id": "tc-old",
                        "function": {"name": "read_file", "arguments": '{"path":"old.md"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-old",
                "conversation_id": "private:10001",
                "content": '{"ok":true,"content":"旧工具返回"}',
            },
        ]
    )

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [record.get("_sort_layer") for record in records] == [
        "event_store",
        "history",
        "history",
    ]
    assert [item.kind for item in items] == ["inbound_message", "tool_call"]
    assert items[0].text == "EventStore 新消息"
    assert items[1].related_tool_call_id == "tc-old"
    assert items[1].tool_results[0].text == '{"ok":true,"content":"旧工具返回"}'
