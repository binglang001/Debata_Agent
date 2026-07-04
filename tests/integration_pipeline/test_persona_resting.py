"""Persona after-turn and resting-state integration pipeline tests."""

from __future__ import annotations

import pytest

from adapters.types import Target
from memory import EventStore
from tests.integration_pipeline.helpers import (
    RecordingPersonaAgent,
    _ai_no_action,
    _ai_send_private,
    _drain_pipeline,
    _msg,
    _wait_until,
)


class RecordingSubconsciousAgent:

    def __init__(self, *, active: bool = True) -> None:

        self.is_active = active

        self.message_calls: list[tuple[str, str, float]] = []

        self.stop_calls = 0

    async def on_message(

        self,

        text: str,

        sender_id: str,

        profile_affinity: float,

    ) -> None:

        self.message_calls.append((text, sender_id, profile_affinity))

    async def stop(self) -> None:

        self.stop_calls += 1

@pytest.mark.asyncio

async def test_after_turn_runs_after_normal_batch(build_pipeline):

    persona_agent = RecordingPersonaAgent()

    pipeline, _, _, _, _ = await build_pipeline(

        [_ai_send_private(target_qq="123", content="人格回复")],

        persona_agent=persona_agent,

    )

    await pipeline.enqueue(_msg(user_id="123", text="你好人格", message_id="after-1"))

    await _drain_pipeline(pipeline)

    await _wait_until(lambda: bool(persona_agent.after_turn_calls))

    call = persona_agent.after_turn_calls[0]

    assert call["conversation_id"] == "private:123"

    assert call["participants"] == [{"user_id": "123", "nickname": "用户"}]

    assert "你好人格" in call["chat_summary"]

    assert "人格回复" in call["chat_summary"]

@pytest.mark.asyncio

async def test_after_turn_runs_after_run_one_turn(build_pipeline):

    persona_agent = RecordingPersonaAgent()

    pipeline, _, _, _, _ = await build_pipeline(

        [_ai_no_action()],

        persona_agent=persona_agent,

    )

    await pipeline.run_one_turn(

        "单轮人格上下文",

        user_event="系统事件触发",

        conversation_id="private:123",

        history_conversation_id="system:proactive",

    )

    await _wait_until(lambda: bool(persona_agent.after_turn_calls))

    call = persona_agent.after_turn_calls[0]

    assert call["conversation_id"] == "private:123"

    assert call["participants"] == [{"user_id": "123"}]

    assert "系统事件触发" in call["chat_summary"]

    assert "单轮人格上下文" in call["chat_summary"]

@pytest.mark.asyncio

async def test_after_turn_run_one_turn_system_global_uses_default_private_target(

    build_pipeline,

):

    persona_agent = RecordingPersonaAgent()

    pipeline, _, _, _, _ = await build_pipeline(

        [_ai_no_action()],

        persona_agent=persona_agent,

    )

    await pipeline.run_one_turn(

        "全局系统事件",

        user_event="后台任务实际面向私聊用户",

        default_target=Target(

            adapter="unit",

            scope="private",

            target_id="456",

        ),

        conversation_id=None,

        history_conversation_id="system:proactive",

    )

    await _wait_until(lambda: bool(persona_agent.after_turn_calls))

    call = persona_agent.after_turn_calls[0]

    assert call["conversation_id"] == "system:global"

    assert call["participants"] == [{"user_id": "456"}]

    assert "后台任务实际面向私聊用户" in call["chat_summary"]

@pytest.mark.asyncio

async def test_resting_run_one_turn_skips_model_send_and_after_turn(build_pipeline):

    persona_agent = RecordingPersonaAgent(current_action="sleeping")

    pipeline, provider, adapter, history, _ = await build_pipeline(

        [_ai_send_private(target_qq="123", content="不应发送")],

        persona_agent=persona_agent,

    )

    await pipeline.run_one_turn(

        "睡眠中的系统轮",

        user_event="这轮不应调用主模型",

        as_system_note="睡眠中的系统事件仍应记录",

        conversation_id="private:123",

        history_conversation_id="system:proactive",

    )

    records = await history.records()

    assert provider.calls == []

    assert adapter.sent == []

    assert persona_agent.after_turn_calls == []

    assert any(

        record.get("role") == "system"

        and "睡眠中的系统事件仍应记录" in str(record.get("content") or "")

        for record in records

    )

    assert any(

        record.get("role") == "system"

        and "persona_resting" in str(record.get("content") or "")

        for record in records

    )

@pytest.mark.asyncio

async def test_resting_inbound_is_recorded_and_buffered_without_main_provider(

    build_pipeline,

    tmp_path,

):

    event_store = EventStore(tmp_path / "resting-events.sqlite3")

    persona_agent = RecordingPersonaAgent(current_action="eating")

    subconscious_agent = RecordingSubconsciousAgent(active=True)

    pipeline, provider, _, _, _ = await build_pipeline(

        [_ai_no_action()],

        event_store=event_store,

        persona_agent=persona_agent,

        subconscious_agent=subconscious_agent,

    )

    await pipeline.enqueue(_msg(user_id="123", text="吃饭中仍入站", message_id="sub-1"))

    await _drain_pipeline(pipeline)

    assert await event_store.wait_projected(timeout=1.0)

    events = await event_store.events_by_type("qq_message_received", limit=10)

    assert subconscious_agent.message_calls == [("吃饭中仍入站", "123", 0.0)]

    assert len(events) == 1

    assert events[0]["payload"]["content"] == "吃饭中仍入站"

    assert provider.calls == []

    assert pipeline.batch.is_empty_unsafe()

@pytest.mark.asyncio

@pytest.mark.parametrize("action", ["sleeping", "collapsing"])

async def test_resting_inbound_without_subconscious_does_not_call_provider(

    build_pipeline,

    action,

):

    _ = action

    persona_agent = RecordingPersonaAgent(current_action=action)

    pipeline, provider, _, history, _ = await build_pipeline(

        [_ai_no_action()],

        persona_agent=persona_agent,

        subconscious_agent=None,

    )

    await pipeline.enqueue(_msg(user_id="123", text="休息中普通消息", message_id="rest-1"))

    await _drain_pipeline(pipeline)

    records = await history.records()

    assert provider.calls == []

    assert any(

        record.get("role") == "user"

        and "休息中普通消息" in str(record.get("content") or "")

        and record.get("metadata", {}).get("suppressed_reason") == "persona_resting"

        for record in records

    )

@pytest.mark.asyncio

async def test_inbound_after_resting_ends_calls_main_provider(build_pipeline):

    persona_agent = RecordingPersonaAgent(current_action="eating")

    pipeline, provider, _, _, _ = await build_pipeline(

        [_ai_no_action()],

        persona_agent=persona_agent,

    )

    await pipeline.enqueue(_msg(user_id="123", text="吃饭中普通消息", message_id="rest-1"))

    await _drain_pipeline(pipeline)

    assert provider.calls == []

    persona_agent.current_action = "awake"

    await pipeline.enqueue(_msg(user_id="123", text="吃完了再聊", message_id="awake-1"))

    await _drain_pipeline(pipeline)

    assert len(provider.calls) == 1

@pytest.mark.asyncio

async def test_inbound_expired_resting_action_does_not_suppress_provider(build_pipeline):

    persona_agent = RecordingPersonaAgent(current_action="eating", action_until=0.0)

    pipeline, provider, _, history, _ = await build_pipeline(

        [_ai_no_action()],

        persona_agent=persona_agent,

    )

    await pipeline.enqueue(_msg(user_id="123", text="吃饭时间已过", message_id="rest-expired-1"))

    await _drain_pipeline(pipeline)

    records = await history.records()

    assert len(provider.calls) == 1

    assert not any(

        record.get("metadata", {}).get("suppressed_reason") == "persona_resting"

        for record in records

    )
