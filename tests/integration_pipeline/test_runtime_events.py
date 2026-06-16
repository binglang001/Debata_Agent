"""Runtime event integration pipeline tests."""

from __future__ import annotations

import pytest

from memory import EventStore
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _ai_send_private,
)


@pytest.mark.asyncio

async def test_run_one_turn_tool_call_writes_runtime_events_and_history(build_pipeline, tmp_path):

    event_store = EventStore(tmp_path / "events.sqlite3")

    pipeline, _, adapter, history, _ = await build_pipeline(

        [_ai_send_private(target_qq="123", content="旁路回复"), _ai_no_action()],

        event_store=event_store,

    )

    await pipeline.run_one_turn(

        "旁路模型轮 runtime event 测试",

        user_event="请执行一次工具调用",

        conversation_id="private:123",

        history_conversation_id="system:proactive",

    )

    assert [content for _, content in adapter.sent] == ["旁路回复"]

    assert await event_store.wait_projected(timeout=1.0)

    events = await event_store.iter_events(limit=20)

    runtime_event_types = [event["event_type"] for event in events]

    assert "tool_call_started" in runtime_event_types

    assert "tool_result_received" in runtime_event_types

    assert {

        event["conversation_id"]

        for event in events

        if event["event_type"] in {"tool_call_started", "tool_result_received"}

    } == {"system:proactive"}

    records = await history.records()

    assert any(

        record.get("role") == "assistant" and record.get("tool_calls")

        for record in records

    )

    assert any(

        record.get("role") == "tool"

        and record.get("tool_call_id") == "tc-1"

        and "旁路回复" in str(record.get("content") or "")

        for record in records

    )

    await event_store.shutdown()
