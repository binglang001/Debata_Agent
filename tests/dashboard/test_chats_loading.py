"""聊天页加载基础来源回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from core.chat_timeline import ChatTimelineStore
from ui.dashboard.chats_page import _load_chat_page_records

from tests.test_dashboard_p2 import (
    _FailingRecordStore,
    _FakeEventStore,
    _PagedArchiveStore,
    _StaticRecordStore,
    _dashboard_runtime,
    _qq_event,
    _timeline_record,
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
async def test_chats_falls_back_to_history_when_archive_fails(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _FailingRecordStore()
    rt.history = _StaticRecordStore([{"role": "system", "content": "活跃系统事件"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["活跃系统事件"]
