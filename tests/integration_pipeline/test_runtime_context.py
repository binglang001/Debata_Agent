"""Runtime context and task-context integration pipeline tests."""

from __future__ import annotations

from typing import Any

import pytest

from app_config.schema import TypingConfig
from core.message_pipeline import (
    _recommended_context_budget,
    _text_mentions_self_or_role,
)
from core.state import RateLimiter
from tests.integration_pipeline.helpers import (
    RecordingPersonaAgent,
    _ai_no_action,
    _drain_pipeline,
    _msg,
)


def test_text_mentions_self_or_role_uses_deterministic_tokens():

    assert _text_mentions_self_or_role("@QQ999 在吗", "999", "测试机器人") is True

    assert _text_mentions_self_or_role("[CQ:at,qq=999] 在吗", "999", "测试机器人") is True

    assert _text_mentions_self_or_role("@测试机器人 在吗", "999", "测试机器人") is True

    assert _text_mentions_self_or_role("普通插话", "999", "测试机器人") is False

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

async def test_main_reply_persists_task_context_snapshot_for_kv_prefix(build_pipeline):

    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])

    await pipeline.enqueue(_msg(user_id="123", text="测缓存", message_id="kv-1"))

    await _drain_pipeline(pipeline)

    records = await history.records()

    roles = [r.get("role") for r in records[:4]]

    assert roles == ["user", "user", "assistant", "tool"]

    assert records[1].get("metadata", {}).get("kind") == "task_context_snapshot"

    assert "<task_context" in records[1]["content"]

    assert "不是用户新发言" in records[1]["content"]

    first_call = provider.calls[0]["messages"]

    assert first_call[1]["role"] == "user"

    assert first_call[2]["role"] == "user"

    assert first_call[2]["content"] == records[1]["content"]

@pytest.mark.asyncio

async def test_pipeline_tool_context_injects_persona_runtime_dependencies(

    build_pipeline,

):

    persona_agent = RecordingPersonaAgent()

    subconscious_agent = RecordingSubconsciousAgent()

    persona_db = object()

    pipeline, _, _, _, _ = await build_pipeline(

        [],

        persona_agent=persona_agent,

        subconscious_agent=subconscious_agent,

        persona_db=persona_db,

    )

    ctx = pipeline._build_tool_context(conversation_id="private:123")

    assert ctx.persona_agent is persona_agent

    assert ctx.subconscious_agent is subconscious_agent

    assert ctx.persona_db is persona_db

@pytest.mark.asyncio

async def test_task_context_persists_persona_context_snapshot(build_pipeline):

    persona_agent = RecordingPersonaAgent(

        "<人格状态>\n- 当前对象画像: 亲密朋友\n</人格状态>"

    )

    pipeline, provider, _, history, _ = await build_pipeline(

        [_ai_no_action()],

        persona_agent=persona_agent,

    )

    await pipeline.enqueue(_msg(user_id="123", text="测人格上下文", message_id="persona-ctx"))

    await _drain_pipeline(pipeline)

    records = await history.records()

    task_context_record = next(

        record

        for record in records

        if record.get("metadata", {}).get("kind") == "task_context_snapshot"

    )

    assert "当前对象画像: 亲密朋友" in task_context_record["content"]

    assert persona_agent.context_calls == ["private:123"]

    assert any(

        message.get("content") == task_context_record["content"]

        for message in provider.calls[0]["messages"]

    )

@pytest.mark.asyncio

async def test_pipeline_passes_persona_tool_flags_to_build_messages(

    build_pipeline,

    monkeypatch,

):

    import core.message_pipeline as message_pipeline_module
    import core.pipeline_turns as pipeline_turns_module

    main_calls: list[tuple[bool | None, bool | None]] = []

    turn_calls: list[tuple[bool | None, bool | None]] = []

    real_main_build_messages = message_pipeline_module.build_messages

    real_turn_build_messages = pipeline_turns_module.build_messages

    def capture_main_build_messages(*args: Any, **kwargs: Any):

        main_calls.append((kwargs.get("eat_tool"), kwargs.get("sleep_tool")))

        kwargs.pop("eat_tool", None)

        kwargs.pop("sleep_tool", None)

        return real_main_build_messages(*args, **kwargs)

    def capture_turn_build_messages(*args: Any, **kwargs: Any):

        turn_calls.append((kwargs.get("eat_tool"), kwargs.get("sleep_tool")))

        kwargs.pop("eat_tool", None)

        kwargs.pop("sleep_tool", None)

        return real_turn_build_messages(*args, **kwargs)

    monkeypatch.setattr(

        message_pipeline_module,

        "build_messages",

        capture_main_build_messages,

    )

    monkeypatch.setattr(

        pipeline_turns_module,

        "build_messages",

        capture_turn_build_messages,

    )

    pipeline, _, _, _, _ = await build_pipeline(

        [_ai_no_action(), _ai_no_action()],

        eat_tool=True,

        sleep_tool=True,

    )

    await pipeline.enqueue(_msg(user_id="123", text="测工具开关", message_id="flags-1"))

    await _drain_pipeline(pipeline)

    await pipeline.run_one_turn(

        "单轮工具开关",

        user_event="触发单轮",

        conversation_id="private:123",

    )

    assert main_calls == [(True, True)]

    assert turn_calls == [(True, True)]

@pytest.mark.asyncio

async def test_group_task_context_uses_lookup_hint_instead_of_recent_real_chat_window(

    build_pipeline,

):

    pipeline, provider, _, _, _ = await build_pipeline([_ai_no_action()])

    await pipeline.enqueue(_msg(user_id="a", group_id="5555", text="前一句", message_id="g1"))

    await _drain_pipeline(pipeline)

    await pipeline.enqueue(_msg(user_id="b", group_id="5555", text="接一句", message_id="g2"))

    await _drain_pipeline(pipeline)

    second_call = provider.calls[1]["messages"]

    task_context = "\n".join(

        str(m.get("content") or "")

        for m in second_call

        if m.get("role") == "user" and "<task_context" in str(m.get("content") or "")

    )

    assert "<recent_group_messages" not in task_context

    assert 'limit="10"' not in task_context

    assert "前一句" not in task_context

    assert "接一句" not in task_context

    assert "msg_id=g1" not in task_context

    assert "msg_id=g2" not in task_context

    assert "<conversation_context_hint" in task_context

    assert "get_recent_chat_messages" in task_context

    assert "recall_history" in task_context

    assert "当前会话：group:5555" in task_context

@pytest.mark.asyncio

async def test_private_task_context_does_not_include_group_window(build_pipeline):

    pipeline, provider, _, _, _ = await build_pipeline([_ai_no_action()])

    await pipeline.enqueue(_msg(user_id="123", text="私聊", message_id="p1"))

    await _drain_pipeline(pipeline)

    first_call = provider.calls[0]["messages"]

    task_context = "\n".join(

        str(m.get("content") or "")

        for m in first_call

        if m.get("role") == "user" and "<task_context" in str(m.get("content") or "")

    )

    assert "<recent_group_messages" not in task_context

@pytest.mark.asyncio

async def test_context_budget_uses_provider_context_length(build_pipeline):

    pipeline, _, _, _, _ = await build_pipeline([])

    pipeline.model_context_length = 1_000_000

    assert pipeline._context_budget().max_context_tokens == 300_000

    assert _recommended_context_budget("deepseek-v4-pro", 1_000_000) == 350_000

    pipeline.behavior_cfg.context.max_context_tokens = 12345

    assert pipeline._context_budget().max_context_tokens == 12345

@pytest.mark.asyncio

async def test_pipeline_tool_context_injects_pending_requests_and_rate_limiter(

    build_pipeline,

):

    limiter = RateLimiter(window_seconds=60, max_messages=1)

    pipeline, _, _, _, _ = await build_pipeline([], rate_limiter=limiter)

    pipeline.behavior_cfg.typing = TypingConfig(

        chars_per_second=2.0,

        english_chars_per_second=6.0,

    )

    ctx = pipeline._build_tool_context(conversation_id="private:456")

    assert ctx.extras["pending_requests"] is pipeline.pending_requests

    assert ctx.extras["rate_limiter"] is limiter

    assert ctx.typing_chars_per_second == pytest.approx(1.0)

    assert ctx.typing_english_chars_per_second == pytest.approx(5.0)

    assert not hasattr(ctx, "typing_min_delay_seconds")

    assert not hasattr(ctx, "typing_max_delay_seconds")

    assert not hasattr(ctx, "typing_clamp_model_delay")
