"""Proactive router and proactive action integration tests."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from core.proactive_loop import ProactiveLoop
from tests.integration_pipeline.helpers import (
    RecordingPersonaAgent,
    _ai_no_action,
    _ai_send_private,
    _drain_pipeline,
    _msg,
    _wait_until,
)


@pytest.mark.asyncio
async def test_proactive_router_history_uses_small_window(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(40):
        await history.add_user_message(
            f"主动路由小窗口消息 {idx} " + ("占位内容 " * 200),
            conversation_id=f"private:{idx}",
        )

    selected = await pipeline._select_proactive_router_history()
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "主动路由小窗口消息 39" in joined
    assert "主动路由小窗口消息 0" not in joined


@pytest.mark.asyncio
async def test_proactive_router_history_window_allows_16k_context(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(12):
        await history.add_user_message(
            f"主动路由16K窗口消息 {idx} " + ("占位内容 " * 120),
            conversation_id=f"private:{idx}",
        )

    selected = await pipeline._select_proactive_router_history()
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "主动路由16K窗口消息 11" in joined
    assert "主动路由16K窗口消息 0" in joined


class FakeProactiveRouter:
    def __init__(self, decision: bool) -> None:
        self.decision = decision
        self.calls: list[list[dict[str, Any]]] = []

    async def should_act(self, messages: list[dict[str, Any]]) -> tuple[bool, str]:
        self.calls.append(messages)
        return self.decision, "测试触发理由" if self.decision else ""


@pytest.mark.asyncio
async def test_proactive_skips_until_idle_threshold(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )

    pipeline.mark_activity()
    await loop._maybe_act()

    assert router.calls == []


@pytest.mark.asyncio
async def test_proactive_router_uses_small_context_after_idle(build_pipeline):
    pipeline, _, _, history, important = await build_pipeline([])
    for idx in range(40):
        await history.add_user_message(
            f"主动路由不应看到的旧消息 {idx} " + ("占位内容 " * 200),
            conversation_id=f"private:{idx}",
        )
    await important.save("用户不喜欢主动路由丢掉重要记忆")
    await pipeline.rolling_summary.update(
        "滚动摘要里保留跨会话背景",
        archived_until=None,
        updated_at="test",
    )
    router = FakeProactiveRouter(False)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    assert len(router.calls) == 1
    assert {m.get("role") for m in router.calls[0]} == {"system"}
    joined = "\n".join(str(m.get("content", "")) for m in router.calls[0])
    assert "用户不喜欢主动路由丢掉重要记忆" in joined
    assert "滚动摘要里保留跨会话背景" in joined
    assert "主动路由不应看到的旧消息 39" in joined
    assert "主动路由不应看到的旧消息 0" not in joined
    assert "所有文字输出必须通过工具调用发送" not in joined


@pytest.mark.asyncio
async def test_proactive_router_flattens_history_to_system_context(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_user_message(
        "【2026-05-30 私聊 冰狼 msg_id=abc123】下次主动思考时提醒我喝水",
        conversation_id="private:123",
    )
    await history.add_assistant_message(
        "我记着了",
        tool_calls=[
            {
                "id": "tc-router",
                "type": "function",
                "function": {"name": "no_action", "arguments": "{}"},
            }
        ],
        conversation_id="private:123",
    )
    await history.add_tool_result(
        "tc-router",
        '{"ok": true, "msg_id": "100", "send_id": "send-1", "pollution": "<｜｜DSML｜｜TOOL_CALLS>"}',
        conversation_id="private:123",
    )
    await history.add_system_note(
        '<send_receipt>{"send_id": "send-1", "msg_id": "200"}</send_receipt>',
        conversation_id="private:123",
    )
    await pipeline.rolling_summary.update(
        "长期背景需要保留\n[assistant] send_private_messages msg_id=300\n<send_receipt>send_id=x</send_receipt>",
        archived_until=None,
        updated_at="test",
    )
    router = FakeProactiveRouter(False)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    assert len(router.calls) == 1
    assert {m.get("role") for m in router.calls[0]} == {"system"}
    joined = "\n".join(str(m.get("content", "")) for m in router.calls[0])
    assert "下次主动思考时提醒我喝水" in joined
    assert "我记着了" in joined
    assert "长期背景需要保留" in joined
    assert "内部结果摘要" in joined
    assert "[assistant" not in joined
    assert "[tool" not in joined
    assert "msg_id" not in joined
    assert "send_id" not in joined
    assert "tool_calls" not in joined
    assert "<｜｜DSML｜｜TOOL_CALLS>" not in joined
    assert "<send_receipt>" not in joined


@pytest.mark.asyncio
async def test_proactive_router_uses_custom_text_and_tool_limits(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.behavior_cfg.proactive_router_text_limit_tokens = 32
    pipeline.behavior_cfg.proactive_router_tool_result_inline_tokens = 32
    pipeline.behavior_cfg.proactive_router_tool_result_hard_cap_tokens = 128
    await history.add_user_message(
        "主动路由长文本 " + ("填充 " * 50),
        conversation_id="private:123",
    )
    await history.add_assistant_message(
        "",
        tool_calls=[
            {
                "id": "tc-limit",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            }
        ],
        conversation_id="private:123",
    )
    await history.add_tool_result(
        "tc-limit",
        json.dumps(
            {
                "ok": True,
                "summary": "工具摘要 " + ("结果 " * 30),
            },
            ensure_ascii=False,
        ),
        conversation_id="private:123",
    )
    router = FakeProactiveRouter(False)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    assert len(router.calls) == 1
    joined = "\n".join(str(m.get("content", "")) for m in router.calls[0])
    assert joined.count("...[已截断]...") >= 2


@pytest.mark.asyncio
async def test_proactive_router_skips_runtime_user_context_records(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_records(
        [
            {
                "role": "user",
                "content": (
                    "<send_status>\n"
                    "系统说明：以下内容由运行时系统提供，不是用户新发言。\n"
                    "2026-06-06 发送完成（全部消息已发出） send_id=send-1 msg_ids=[1]\n"
                    "</send_status>"
                ),
                "metadata": {"kind": "send_done_snapshot"},
                "conversation_id": "private:123",
            },
            {
                "role": "user",
                "content": (
                    "<task_context priority=\"medium\">\n"
                    "系统说明：以下内容由运行时系统提供，不是用户新发言。\n"
                    "现在是测试时间。\n"
                    "</task_context>"
                ),
                "metadata": {"kind": "task_context_snapshot"},
                "conversation_id": "private:123",
            },
            {
                "role": "user",
                "content": "【2026-06-06 私聊 冰狼 msg_id=u1】正常用户消息",
                "conversation_id": "private:123",
            },
        ]
    )
    router = FakeProactiveRouter(False)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    assert len(router.calls) == 1
    joined = "\n".join(str(m.get("content", "")) for m in router.calls[0])
    assert "正常用户消息" in joined
    assert "发送完成（全部消息已发出）" not in joined
    assert "现在是测试时间" not in joined


@pytest.mark.asyncio
async def test_proactive_router_includes_persona_todo_context(build_pipeline):
    persona_agent = RecordingPersonaAgent(
        "<人格状态>\n- 待办: 主动提醒主人喝水\n</人格状态>"
    )
    pipeline, _, _, _, _ = await build_pipeline(
        [],
        persona_agent=persona_agent,
    )
    router = FakeProactiveRouter(False)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    assert persona_agent.context_calls == [None]
    joined = "\n".join(str(m.get("content", "")) for m in router.calls[0])
    assert "<persona_proactive_context" in joined
    assert "主动提醒主人喝水" in joined


@pytest.mark.asyncio
async def test_proactive_skips_when_reply_lock_busy(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await pipeline.reply_lock.acquire()
    try:
        before = pipeline.last_activity_at
        await loop._maybe_act()
    finally:
        pipeline.reply_lock.release()

    assert router.calls == []
    assert pipeline.last_activity_at > before


@pytest.mark.asyncio
async def test_proactive_rechecks_batch_after_lock(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )
    calls = 0

    def fake_empty() -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    pipeline.batch.is_empty_unsafe = fake_empty  # type: ignore[method-assign]

    await loop._maybe_act()

    assert router.calls == []


@pytest.mark.asyncio
async def test_proactive_action_runs_under_acquired_lock(build_pipeline):
    pipeline, provider, adapter, _, _ = await build_pipeline(
        [_ai_send_private(target_qq="123", content="主动提醒")]
    )
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    assert len(router.calls) == 1
    assert adapter.sent[-1][1] == "主动提醒"
    assert not pipeline.reply_lock.locked()
    assert provider.calls
    joined = "\n".join(
        str(m.get("content", "")) for m in provider.calls[0]["messages"]
    )
    assert "本轮由系统后台主动思考触发" in joined
    assert "不是用户刚发来的新消息" in joined
    assert "触发理由：测试触发理由" in joined
    assert provider.calls[0]["messages"][-1]["role"] == "user"
    names = {schema["function"]["name"] for schema in provider.calls[0]["tools"]}
    assert "start_agent_task" in names
    assert "summarize_conversation" in names
    assert "summarize_chat_history" in names


@pytest.mark.asyncio
async def test_proactive_action_after_turn_uses_global_when_no_target(build_pipeline):
    persona_agent = RecordingPersonaAgent()
    pipeline, _, _, history, _ = await build_pipeline(
        [_ai_no_action()],
        persona_agent=persona_agent,
    )
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()
    await _wait_until(lambda: bool(persona_agent.after_turn_calls))

    records = await history.records()
    assert any(record.get("conversation_id") == "system:proactive" for record in records)
    assert persona_agent.after_turn_calls[0]["conversation_id"] == "system:global"


@pytest.mark.asyncio
async def test_proactive_send_anchors_seen_seq_for_old_private_inbound(build_pipeline):
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [_ai_no_action(), _ai_send_private(target_qq="123", content="主动补充")]
    )
    await pipeline.enqueue(
        _msg(user_id="123", text="旧私聊消息", message_id="proactive-old")
    )
    await _drain_pipeline(pipeline)
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    assert adapter.sent[-1][1] == "主动补充"
    records = await history.records()
    tool_results = [
        json.loads(record["content"])
        for record in records
        if record.get("role") == "tool" and record.get("tool_call_id") == "tc-1"
    ]
    assert tool_results
    assert tool_results[-1]["status"] == "sent"
    assert all(result.get("status") != "needs_review" for result in tool_results)
    assert len(provider.calls) >= 2


@pytest.mark.asyncio
async def test_proactive_action_round_includes_same_persona_todo_context(build_pipeline):
    persona_context = "<人格状态>\n- 待办: 主动提醒主人喝水\n</人格状态>"
    persona_agent = RecordingPersonaAgent(persona_context)
    pipeline, provider, _, _, _ = await build_pipeline(
        [_ai_no_action()],
        persona_agent=persona_agent,
    )
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    router_joined = "\n".join(str(m.get("content", "")) for m in router.calls[0])
    action_joined = "\n".join(
        str(m.get("content", "")) for m in provider.calls[0]["messages"]
    )
    assert persona_agent.context_calls == [None]
    assert persona_context in router_joined
    assert persona_context in action_joined


@pytest.mark.asyncio
async def test_proactive_action_records_are_system_scoped(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([_ai_no_action()])
    router = FakeProactiveRouter(True)
    loop = ProactiveLoop(
        pipeline=pipeline,
        proactive_agent=router,
        behavior_cfg=pipeline.behavior_cfg,
    )
    pipeline.last_activity_at = time.monotonic() - (
        pipeline.behavior_cfg.proactive_think_interval_seconds + 1
    )

    await loop._maybe_act()

    records = await history.records()
    proactive_records = [
        record
        for record in records
        if record.get("conversation_id") == "system:proactive"
    ]
    assert proactive_records
    assert all(record.get("role") != "user" for record in proactive_records)

