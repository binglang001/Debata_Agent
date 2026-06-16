"""聊天页加载、事件源和刷新订阅回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

import ui.dashboard.chats_page as chats_page_module
from core.chat_timeline import ChatTimelineStore
from ui.dashboard.chats_page import (
    EVENT_STORE_CHAT_PAGE_EVENT_TYPES,
    ChatsPage,
    _group_records_by_conversation,
    _load_chat_page_records,
    normalize_history_records,
)

from tests.test_dashboard_p2 import (
    _FailingRecordStore,
    _FakeEventStore,
    _FakeTimeline,
    _PagedArchiveStore,
    _StaticRecordStore,
    _dashboard_runtime,
    _pump_dashboard_events,
    _qq_event,
    _refresh_test_chats_page,
    _runtime_event,
    _timeline_record,
    _wait_for_dashboard_condition,
)


@pytest.mark.asyncio
async def test_chats_loads_archive_before_active_history(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _StaticRecordStore([{"role": "user", "content": "归档旧消息"}])
    rt.history = _StaticRecordStore([{"role": "assistant", "content": "活跃新消息"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["归档旧消息", "活跃新消息"]


@pytest.mark.asyncio
async def test_chats_loads_archive_through_filter_records_pages(tmp_paths):
    archived = [
        {
            "archive_id": f"a-{i}",
            "role": "user",
            "content": f"归档 {i}",
            "conversation_id": "group:1",
            "timestamp": f"2026-06-01 00:00:0{i}",
            "sender_id": "100",
            "sender_name": "Alice",
        }
        for i in range(3)
    ]
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _PagedArchiveStore(archived, page_size=2)
    rt.history = _StaticRecordStore([{"role": "assistant", "content": "活跃新消息"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == [
        "归档 0",
        "归档 1",
        "归档 2",
        "活跃新消息",
    ]
    assert rt.archive.records_called is False
    assert [call["offset"] for call in rt.archive.filter_calls] == [0, 2]
    assert rt.archive.get_by_ids_calls == [["a-0", "a-1"], ["a-2"]]


@pytest.mark.asyncio
async def test_chats_load_debug_metrics_do_not_log_message_body(tmp_paths, caplog):
    rt = _dashboard_runtime(tmp_paths)
    rt.history = _StaticRecordStore(
        [
            {
                "role": "system",
                "content": "history-secret-body",
                "conversation_id": "private:debug",
            }
        ]
    )
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                1,
                "qq_message_received",
                conversation_id="private:debug",
                content="event-secret-body",
                msg_id="event-debug",
            )
        ]
    )
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:debug",
            direction="outbound",
            text="timeline-secret-body",
            msg_id="timeline-debug",
            timestamp=1_780_000_002.0,
            time_text="2026-06-08 23:07:20",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _PagedArchiveStore(
        [
            {
                "archive_id": "archive-debug",
                "role": "user",
                "content": "archive-secret-body",
                "conversation_id": "private:debug",
                "timestamp": "2026-06-08 23:07:19",
                "sender_id": "100",
                "sender_name": "Alice",
            }
        ],
        page_size=1,
    )

    with caplog.at_level(logging.DEBUG, logger="ui.dashboard.chats_page"):
        records = await _load_chat_page_records(rt)

    assert len(records) == 4
    log_text = caplog.text
    assert "对话页记录加载指标" in log_text
    assert "history_ms=" in log_text
    assert "event_store_ms=" in log_text
    assert "timeline_ms=" in log_text
    assert "archive_ms=" in log_text
    assert "merge_tag_ms=" in log_text
    assert "history_records=1" in log_text
    assert "event_store_records=1" in log_text
    assert "timeline_records=1" in log_text
    assert "archive_records=1" in log_text
    assert "total_records=4" in log_text
    assert "history-secret-body" not in log_text
    assert "event-secret-body" not in log_text
    assert "timeline-secret-body" not in log_text
    assert "archive-secret-body" not in log_text


@pytest.mark.asyncio
async def test_chats_loads_timeline_without_archive(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="实时发出",
            msg_id="tl-1",
            timestamp=1_780_000_001.0,
            time_text="2026-06-08 23:07:19",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["实时发出"]
    assert records[0]["_source"] == "chat_timeline"


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
    assert items[2].text == "用户中途补充"
    assert "result_hash" in result_item.text


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
    assert "send_id=send-1" in detail
    assert "msg_id=msg-1" in detail
    assert "counts messages=2, conversations=1" in detail
    assert "内容长度 12" in detail
    assert "内容hash=" + "b" * 64 in detail
    assert "预览：系统提示预览" in detail
    assert all(item.kind not in {"inbound_message", "outbound_message"} for item in items)


@pytest.mark.asyncio
async def test_chats_event_store_runtime_events_dedupe_history_runtime_fallback(tmp_paths):
    system_content = "系统补充"
    system_hash = hashlib.sha256(system_content.encode("utf-8")).hexdigest()
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-1"},
                tool_call_id="tc-1",
            ),
            _runtime_event(
                2,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-1",
                    "ok": True,
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

    assert "完整工具返回" not in json.dumps(records, ensure_ascii=False)
    assert all(record.get("_source") == "event_store" for record in records[:4])
    assert records[-1]["content"] == "旧 history fallback"
    assert records[-1].get("_source") is None


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


@pytest.mark.asyncio
async def test_chats_render_uses_timeline_outbound_when_history_has_not_flushed(qapp, tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="history 还没有的实时回复",
            msg_id="tl-out-1",
            timestamp=1_780_000_001.0,
            time_text="2026-06-08 23:07:19",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.history = _StaticRecordStore([{"role": "system", "content": "系统补充"}])
    page = ChatsPage(rt)

    records = await _load_chat_page_records(rt)
    conv = next(item for item in _group_records_by_conversation(records) if item["key"] == "private:10001")
    html = page._render_conversation(conv)

    assert "chat-record chat-message-table chat-side-left chat-bot" in html
    assert "history 还没有的实时回复" in html
    assert "已发送 · msg_id=tl-out-1" in html


@pytest.mark.asyncio
async def test_chats_falls_back_to_history_when_archive_fails(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _FailingRecordStore()
    rt.history = _StaticRecordStore([{"role": "system", "content": "活跃系统事件"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["活跃系统事件"]


@pytest.mark.asyncio
async def test_chats_refresh_debounce_starts_one_load_for_burst(qapp, tmp_paths, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def load_records(_rt):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [{"role": "user", "content": "刷新结果", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        page.refresh()
        page.refresh()
        await _wait_for_dashboard_condition(qapp, started.is_set)

        task = page._refresh_task
        assert task is not None
        assert calls == 1

        release.set()
        await task

        assert calls == 1
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_refresh_pending_collapses_inflight_burst(qapp, tmp_paths, monkeypatch):
    releases: list[asyncio.Event] = []
    calls: list[int] = []

    async def load_records(_rt):
        index = len(calls)
        calls.append(index)
        release = asyncio.Event()
        releases.append(release)
        await release.wait()
        return [{"role": "user", "content": f"刷新 {index}", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 1)
        first_task = page._refresh_task
        assert first_task is not None

        page.refresh()
        page.refresh()
        page.refresh()

        assert page._refresh_pending is True
        assert calls == [0]

        releases[0].set()
        await first_task
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 2)

        second_task = page._refresh_task
        assert second_task is not None
        await _pump_dashboard_events(qapp, rounds=3)
        assert calls == [0, 1]

        releases[1].set()
        await second_task

        assert [item["content"] for item in page._records] == ["刷新 1"]
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_refresh_generation_skips_stale_pending_result(qapp, tmp_paths, monkeypatch):
    first_release = asyncio.Event()
    calls = 0

    async def load_records(_rt):
        nonlocal calls
        calls += 1
        if calls == 1:
            await first_release.wait()
            return [{"role": "user", "content": "旧结果", "conversation_id": "private:1"}]
        return [{"role": "user", "content": "新结果", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        await _wait_for_dashboard_condition(qapp, lambda: calls == 1)
        first_task = page._refresh_task
        assert first_task is not None

        page.refresh()
        first_release.set()
        await first_task

        assert [item["content"] for item in page._records] != ["旧结果"]

        await _wait_for_dashboard_condition(
            qapp,
            lambda: calls == 2 and page._refresh_task is None,
        )

        assert [item["content"] for item in page._records] == ["新结果"]
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_refresh_exception_does_not_block_next_refresh(qapp, tmp_paths, monkeypatch):
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    calls = 0

    async def load_records(_rt):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await first_release.wait()
            raise RuntimeError("load failed")
        second_started.set()
        await second_release.wait()
        return [{"role": "user", "content": "恢复刷新", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        await _wait_for_dashboard_condition(qapp, first_started.is_set)
        first_task = page._refresh_task
        assert first_task is not None
        first_release.set()
        await first_task

        assert page._refresh_task is None
        assert page._refresh_pending is False

        page.refresh()
        await _wait_for_dashboard_condition(qapp, second_started.is_set)
        second_task = page._refresh_task
        assert second_task is not None
        second_release.set()
        await second_task

        assert calls == 2
        assert [item["content"] for item in page._records] == ["恢复刷新"]
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_timeline_notification_schedules_debounced_refresh(qapp, tmp_paths):
    timeline = _FakeTimeline()
    page = _refresh_test_chats_page(tmp_paths, timeline)
    page._refresh_debounce_timer.setInterval(1000)
    try:
        generation = page._refresh_generation

        timeline.emit()

        assert page._refresh_generation == generation
        assert not page._refresh_debounce_timer.isActive()

        await _pump_dashboard_events(qapp)

        assert page._refresh_generation == generation + 1
        assert page._refresh_debounce_timer.isActive()
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page._unsubscribe_chat_timeline()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_timeline_notification_uses_existing_refresh_single_flight(
    qapp,
    tmp_paths,
    monkeypatch,
):
    releases: list[asyncio.Event] = []
    calls: list[int] = []

    async def load_records(_rt):
        index = len(calls)
        calls.append(index)
        release = asyncio.Event()
        releases.append(release)
        await release.wait()
        return [{"role": "user", "content": f"刷新 {index}", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    timeline = _FakeTimeline()
    page = _refresh_test_chats_page(tmp_paths, timeline)
    try:
        timeline.emit()
        timeline.emit()
        timeline.emit()
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 1)
        first_task = page._refresh_task
        assert first_task is not None

        timeline.emit()
        timeline.emit()
        await _pump_dashboard_events(qapp, rounds=3)

        assert calls == [0]
        assert page._refresh_pending is True

        releases[0].set()
        await first_task
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 2)
        second_task = page._refresh_task
        assert second_task is not None

        releases[1].set()
        await second_task

        assert calls == [0, 1]
    finally:
        for release in releases:
            release.set()
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page._unsubscribe_chat_timeline()
        page.deleteLater()


def test_chats_timeline_subscription_switches_when_runtime_changes(qapp, tmp_paths):
    first_timeline = _FakeTimeline()
    second_timeline = _FakeTimeline()
    page = _refresh_test_chats_page(tmp_paths, first_timeline)
    try:
        assert len(first_timeline.listeners) == 1

        page._runtime.pipeline = SimpleNamespace(chat_timeline=second_timeline)
        page.refresh()

        assert first_timeline.listeners == []
        assert first_timeline.unsubscribe_calls == 1
        assert len(second_timeline.listeners) == 1

        page._unsubscribe_chat_timeline()

        assert second_timeline.listeners == []
        assert second_timeline.unsubscribe_calls == 1
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page._unsubscribe_chat_timeline()
        page.deleteLater()
