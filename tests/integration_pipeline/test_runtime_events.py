"""Runtime event integration pipeline tests."""

from __future__ import annotations

import json

import pytest

from memory import EventStore
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _ai_send_private,
)


@pytest.mark.asyncio

async def test_run_one_turn_tool_call_writes_runtime_events_and_history(build_pipeline, tmp_path):
    expected_args = {
        "targets": [{"target_qq": "123", "content": "旁路回复", "order": 1, "delay": 0}],
    }
    expected_raw_arguments = json.dumps(expected_args)

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

    send_started = next(
        event
        for event in events
        if event["event_type"] == "tool_call_started"
        and event.get("tool_call_id") == "tc-1"
    )
    send_result = next(
        event
        for event in events
        if event["event_type"] == "tool_result_received"
        and event.get("tool_call_id") == "tc-1"
    )
    assert send_started["source"] == "agent_runner"
    assert send_started["payload"]["tool_call_id"] == "tc-1"
    assert send_started["payload"]["tool_name"] == "send_private_messages"
    assert send_started["payload"]["args"] == expected_args
    assert send_started["payload"]["raw_arguments"] == expected_raw_arguments
    assert send_started["payload"]["args_keys"] == ["targets"]
    assert send_started["payload"]["args_key_count"] == 1
    assert send_started["payload"]["args_length"] > 0
    assert send_result["source"] == "agent_runner"
    assert send_result["payload"]["tool_call_id"] == "tc-1"
    assert send_result["payload"]["tool_name"] == "send_private_messages"
    assert send_result["payload"]["args"] == expected_args
    assert send_result["payload"]["raw_arguments"] == expected_raw_arguments

    records = await history.records()
    tool_record = next(
        record
        for record in records
        if record.get("role") == "tool" and record.get("tool_call_id") == "tc-1"
    )
    tool_result = json.loads(tool_record["content"])
    assert send_result["payload"]["result"] == tool_result
    assert send_result["payload"]["result_keys"] == sorted(tool_result.keys())
    assert send_result["payload"]["result_key_count"] == len(tool_result)
    assert send_result["payload"]["result_length"] > 0
    assert send_result["payload"]["ok"] == tool_result["ok"]
    assert send_result["payload"]["status"] == tool_result["status"]
    assert "旁路回复" in json.dumps(send_result["payload"]["result"], ensure_ascii=False)

    assert any(

        record.get("role") == "assistant" and record.get("tool_calls")

        for record in records

    )

    assert "旁路回复" in str(tool_record.get("content") or "")

    await event_store.shutdown()
