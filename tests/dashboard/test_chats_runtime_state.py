"""聊天页订阅与运行时状态回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.chat_timeline import ChatTimelineStore
from ui.dashboard.chats_page import (
    ChatsPage,
    _group_records_by_conversation,
    _load_chat_page_records,
)

from tests.test_dashboard_p2 import (
    _FakeTimeline,
    _StaticRecordStore,
    _dashboard_runtime,
    _refresh_test_chats_page,
    _timeline_record,
)

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
