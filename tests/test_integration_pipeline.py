"""跨模块集成测试 —— 防一致性 bug 的安全网。

用 mock IProvider（脚本化 CompletionResult）+ mock IAdapter（拦截 send_text）+
真实 HistoryManager / ImportantMemoryManager / ChatAgent / ToolRegistry / MessagePipeline，
让一条 IncomingMessage 走完完整链路：

    IncomingMessage
      → MessagePipeline.enqueue
        → batch + _batch_loop
          → _process_batch
            → history.add_user_message
            → build_messages(persona, history.records(), ...)
            → chat_agent.run(messages, tools, executor)
              → tool executor → tools/messaging.py 即时发送
            → history.add_records

链路里任何方法名 / 字段 / 键名不一致，这里会立刻挂。这是 P1.8 完成的硬门槛——
比 273 个模块单测更能抓住跨模块 critical（例如 history.records() 不存在这种）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from adapters.types import (
    Target,
)
from agents.base import AgentRunResult
from app_config.schema import (
    TypingConfig,
)
from core.message_pipeline import (
    _recommended_context_budget,
    _text_mentions_self_or_role,
)
from core.state import PendingMessageItem, RateLimiter
from memory import (
    EventStore,
)
from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    RecordingPersonaAgent,
    _ai_no_action,
    _ai_send_group,
    _ai_send_private,
    _ai_tool_search,
    _drain_pipeline,
    _make_root_config,
    _msg,
    _wait_until,
)
from tests.integration_pipeline.helpers import (
    build_pipeline as _build_pipeline_fixture,
)
from tools import build_default_registry

build_pipeline = _build_pipeline_fixture

# ============================================================
# 配置/Persona 工厂
# ============================================================


# ============================================================
# ScriptedProvider —— 每次 chat_completion 弹出一个预设结果
# ============================================================


# ============================================================
# FakeAdapter —— 实现 IAdapter，所有发送拦截到 self.sent
# ============================================================


# ============================================================
# fixture：构造完整 pipeline
# ============================================================


# ============================================================
# Helpers
# ============================================================


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


# ============================================================
# 测试用例
# ============================================================


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


def test_tool_registry_stub_schema_reduces_exposed_tool_schema_size():
    registry = build_default_registry(_make_root_config())
    for name in ("upload_file", "start_agent_task"):
        spec = registry.get_spec(name)
        assert spec is not None
        stub_text = json.dumps(spec.to_openai_schema(), ensure_ascii=False)
        full_text = json.dumps(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.full_parameters_schema(),
                },
            },
            ensure_ascii=False,
        )

        assert "_tool_search_required" in stub_text
        assert len(stub_text) < len(full_text)


@pytest.mark.asyncio
async def test_slash_hash_group_messages_are_not_explicit_batch_triggers(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    events = [
        _msg(user_id="111", group_id="5555", text="/other", message_id="m-slash"),
        _msg(user_id="222", group_id="5555", text="#topic", message_id="m-hash"),
        _msg(user_id="333", group_id="5555", text="普通接话", message_id="m-normal"),
    ]
    items = [
        PendingMessageItem(
            message_id=event.message_id,
            user_id=event.user_id,
            nickname=event.nickname,
            location=f"群聊 {event.group_id}",
            text=event.text,
            conversation_id=f"group:{event.group_id}",
            inbound_seq=index,
            received_at=1.0,
            raw_event=event,
        )
        for index, event in enumerate(events, start=1)
    ]

    trigger_ids = pipeline._batch_trigger_message_ids(items)
    focus_ids = pipeline._batch_focus_user_ids(items, trigger_ids)

    assert trigger_ids == ["m-normal"]
    assert focus_ids == ["333"]


@pytest.mark.asyncio
async def test_basic_private_message_flow(build_pipeline):
    """私聊：enqueue → batch → chat_agent → tool → adapter.send_text。

    这条链路全程不抛错，意味着所有跨模块接口名/字段名一致。"""
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [_ai_send_private(target_qq="123", content="收到，你好~")]
    )

    await pipeline.enqueue(_msg(user_id="123", text="你好"))
    await _drain_pipeline(pipeline)

    # adapter 收到了对应发送
    assert len(adapter.sent) == 1, f"expected 1 send, got {adapter.sent}"
    target, content = adapter.sent[0]
    assert target.scope == "private"
    assert target.target_id == "123"
    assert content == "收到，你好~"

    # history 不仅写了 user/assistant，且 records() 别名能用
    records = await history.records()
    roles = [r.get("role") for r in records]
    assert "user" in roles
    assert "assistant" in roles

    # 发送后还会把工具结果交还给模型，由 no_action 明确收尾。
    assert len(provider.calls) == 2
    call_msgs = provider.calls[0]["messages"]
    assert call_msgs[0]["role"] == "system"
    assert any(m["role"] == "user" for m in call_msgs)


@pytest.mark.asyncio
async def test_group_message_flow(build_pipeline):
    """群聊：send_group_message 路径走通，scope=group。"""
    pipeline, _, adapter, _, _ = await build_pipeline([_ai_send_group("5555", "群好")])

    await pipeline.enqueue(_msg(user_id="9", group_id="5555", text="在吗"))
    await _drain_pipeline(pipeline)

    assert any(
        t.scope == "group" and t.target_id == "5555" and c == "群好"
        for t, c in adapter.sent
    ), f"群消息未发出: {adapter.sent}"


@pytest.mark.asyncio
async def test_group_messages_bypass_private_rate_limiter(build_pipeline):
    """群聊由群白名单/审核控制，不应套用陌生私聊频控。"""
    limiter = RateLimiter(window_seconds=60, max_messages=0)
    pipeline, provider, adapter, _, _ = await build_pipeline(
        [_ai_send_group("5555", "群内正常回复")],
        rate_limiter=limiter,
    )

    await pipeline.enqueue(_msg(user_id="stranger", group_id="5555", text="群里说话"))
    await _drain_pipeline(pipeline)

    assert len(provider.calls) == 2
    assert [content for _, content in adapter.sent] == ["群内正常回复"]


@pytest.mark.asyncio
async def test_merge_window_groups_by_conversation(build_pipeline):
    """同一合并窗口内的私聊和群聊应拆成两轮，目标不串。"""
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            _ai_send_private(target_qq="123", content="私聊回复"),
            _ai_no_action(),
            _ai_send_group("5555", "群聊回复"),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="私聊消息", message_id="p1"))
    await pipeline.enqueue(
        _msg(user_id="9", group_id="5555", text="群消息", message_id="g1")
    )
    await _drain_pipeline(pipeline)

    assert [(t.scope, t.target_id, c) for t, c in adapter.sent] == [
        ("private", "123", "私聊回复"),
        ("group", "5555", "群聊回复"),
    ]
    assert len(provider.calls) == 4
    call1 = "\n".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])
    call2 = "\n".join(str(m.get("content", "")) for m in provider.calls[2]["messages"])
    assert "当前会话：private:123" in call1
    assert "当前会话：group:5555" in call2

    records = await history.records()
    convs = [r.get("conversation_id") for r in records if r.get("role") == "user"]
    assert "private:123" in convs
    assert "group:5555" in convs


@pytest.mark.asyncio
async def test_agent_task_materializes_sources_without_url(build_pipeline, tmp_path):
    pipeline, _, adapter, history, _ = await build_pipeline([])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pipeline.workspace_dir = workspace
    (workspace / "input.md").write_text("已有文件", encoding="utf-8")
    await history.add_user_message("这里有 msg_id=abc123 的记录", conversation_id="private:123")
    task_dir = workspace / "agent_tasks" / "manual"
    task_dir.mkdir(parents=True)

    async def fake_get_forward_msg(forward_id: str):
        if forward_id == "outer":
            return [
                {
                    "sender": {"nickname": "Lilith"},
                    "raw_message": "[CQ:forward,id=inner]",
                }
            ]
        return [
            {
                "sender": {"nickname": "Diana"},
                "raw_message": "内层消息",
            }
        ]

    adapter.get_forward_msg = fake_get_forward_msg  # type: ignore[method-assign]

    manifest = await pipeline._materialize_agent_task_sources(
        [
            {"type": "workspace_path", "value": "input.md"},
            {"type": "inline_text", "value": "内联材料"},
            {"type": "message_id", "value": "abc123"},
            {"type": "image_ref", "value": "https://example.com/a.png"},
            {"type": "forward_id", "value": "outer"},
        ],
        task_dir,
    )

    assert manifest["count"] == 5
    assert manifest["sources"][0]["path"] == "input.md"
    inline_path = workspace / manifest["sources"][1]["path"]
    assert inline_path.read_text(encoding="utf-8") == "内联材料"
    assert manifest["sources"][2]["record_count"] == 1
    assert "暂不支持直接传 URL" in manifest["sources"][3]["error"]
    assert manifest["sources"][4]["message_count"] == 2
    assert manifest["sources"][4]["nested_forward_count"] == 1
    forward_tree = json.loads(
        (workspace / manifest["sources"][4]["path"]).read_text(encoding="utf-8")
    )
    assert forward_tree["type"] == "forward"
    assert forward_tree["messages"][0]["segments"][0]["node"]["forward_id"] == "inner"


@pytest.mark.asyncio
async def test_agent_task_max_loops_returns_partial_result(build_pipeline, tmp_path):
    pipeline, _, _, _, _ = await build_pipeline([])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pipeline.workspace_dir = workspace

    async def fake_run(_messages, *, max_loops=None, **_kwargs):
        assert max_loops == 17
        return AgentRunResult(
            final_content="还没完全整理完",
            records=[{"role": "assistant", "content": "正在整理资料"}],
            loop_count=17,
            finish_reason="max_loops",
        )

    pipeline.chat_agent.run = fake_run  # type: ignore[method-assign]

    result = await pipeline._run_agent_task(
        "task-partial",
        {
            "prompt": "整理资料并输出 Markdown",
            "max_loops": 17,
            "output_format": "markdown",
            "output_name": "result.md",
        },
        conversation_id="private:123",
        default_target=None,
    )

    result_path = workspace / "agent_tasks" / "task-partial" / "result.md"
    assert result_path.exists()
    text = result_path.read_text(encoding="utf-8")
    assert "部分结果" in text
    assert "整理资料并输出 Markdown" in text
    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["result_file"] == "agent_tasks/task-partial/result.md"
    assert "工具循环最终收尾条件" in result["error"]
    assert "content" in result and "部分结果" in result["content"]


@pytest.mark.asyncio
async def test_agent_task_returns_after_target_output_written(build_pipeline, tmp_path):
    output_name = "target_result.md"
    write_args = {
        "path": f"agent_tasks/task-write/{output_name}",
        "content": "# 结果\n\n完成。",
    }
    pipeline, provider, _, _, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-write-target",
                        name="write_file",
                        arguments=json.dumps(write_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action(),
        ]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pipeline.workspace_dir = workspace

    result = await pipeline._run_agent_task(
        "task-write",
        {
            "prompt": "写出结果文件",
            "output_format": "markdown",
            "output_name": output_name,
        },
        conversation_id="private:123",
        default_target=None,
    )

    assert len(provider.calls) == 1
    result_path = workspace / "agent_tasks" / "task-write" / output_name
    assert result_path.exists()
    assert result["status"] == "completed"
    assert result["result_file"] == f"agent_tasks/task-write/{output_name}"
    assert result["content"] == "# 结果\n\n完成。"
    assert result["data"]["finish_reason"] == "tool_stop"


@pytest.mark.asyncio
async def test_agent_task_timeout_returns_existing_target_output(
    build_pipeline,
    tmp_path,
    monkeypatch,
):
    pipeline, _, _, _, _ = await build_pipeline([])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pipeline.workspace_dir = workspace

    import core.message_pipeline as message_pipeline

    monkeypatch.setattr(
        message_pipeline,
        "_agent_task_timeout_seconds",
        lambda *_args, **_kwargs: 0.01,
    )

    async def fake_run(_messages, **_kwargs):
        result_path = workspace / "agent_tasks" / "task-timeout" / "result.md"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("# 结果\n\n已经写出。", encoding="utf-8")
        await asyncio.sleep(3600)

    pipeline.chat_agent.run = fake_run  # type: ignore[method-assign]

    result = await pipeline._run_agent_task(
        "task-timeout",
        {
            "prompt": "写出结果后模拟挂起",
            "output_format": "markdown",
            "output_name": "result.md",
        },
        conversation_id="private:123",
        default_target=None,
    )

    result_path = workspace / "agent_tasks" / "task-timeout" / "result.md"
    assert result_path.exists()
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["result_file"] == "agent_tasks/task-timeout/result.md"
    assert "超时" in result["error"]
    assert pipeline._agent_task_meta["task-timeout"]["timeout_with_existing_output"] is True


@pytest.mark.asyncio
async def test_start_agent_task_result_is_in_band_same_turn(build_pipeline, tmp_path):
    start_args = {
        "prompt": "整理资料",
        "sources": [{"type": "inline_text", "value": "资料"}],
        "output_format": "markdown",
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            _ai_tool_search("start_agent_task"),
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-start",
                        name="start_agent_task",
                        arguments=json.dumps(start_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_send_private(target_qq="123", content="后台结果已完成"),
        ]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pipeline.workspace_dir = workspace

    async def fake_run_agent_task(task_id, _payload, *, conversation_id, default_target):
        result_path = workspace / "agent_tasks" / task_id / "result.md"
        result_path.parent.mkdir(parents=True)
        result_path.write_text("# 结果\n\n完成。", encoding="utf-8")
        return {
            "ok": True,
            "status": "completed",
            "brief": "子 Agent 已完成：结果",
            "task_id": task_id,
            "result_file": f"agent_tasks/{task_id}/result.md",
            "path": f"agent_tasks/{task_id}/result.md",
            "content": "# 结果\n\n完成。",
            "summary": "结果",
            "data": {"task_id": task_id, "result_file": f"agent_tasks/{task_id}/result.md"},
        }

    pipeline._run_agent_task = fake_run_agent_task  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="先把能做的完成", message_id="m-start"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert len(provider.calls) == 4
    second_messages = provider.calls[2]["messages"]
    tool_records = [m for m in second_messages if m.get("role") == "tool"]
    assert tool_records
    assert "agent_tasks/" in tool_records[-1]["content"]
    assert "agent_task_result" not in "\n".join(str(m.get("content") or "") for m in second_messages)
    assert any(
        sent_target.target_id == "123" and text == "后台结果已完成"
        for sent_target, text in adapter.sent
    )
    records = await history.records()
    joined_records = "\n".join(str(record.get("content", "")) for record in records)
    assert "agent_task_result" not in joined_records
    assert "后台结果已完成" in joined_records


@pytest.mark.asyncio
async def test_wakeup_turn_uses_user_event_and_denies_long_running_tools(build_pipeline):
    pipeline, provider, adapter, _, _ = await build_pipeline([_ai_no_action()])

    await pipeline.run_wakeup_turn(
        "30 秒到了，请发送消息。",
        target={"target_type": "private", "target_id": 123},
        mode="wakeup",
    )

    assert len(provider.calls) == 1
    names = {
        schema["function"]["name"]
        for schema in provider.calls[0]["tools"]
    }
    assert "schedule_wakeup" in names
    assert "start_agent_task" in names
    assert "summarize_chat_history" in names
    assert "summarize_conversation" in names
    messages = provider.calls[0]["messages"]
    assert messages[-1]["role"] == "user"
    assert "[系统事件 · 非用户消息]" in messages[-1]["content"]
    assert "定时唤醒已到" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_wakeup_turn_denies_long_running_tool_execution(build_pipeline):
    start_args = {
        "prompt": "整理资料",
        "sources": [{"type": "inline_text", "value": "资料"}],
        "output_format": "markdown",
    }
    pipeline, provider, _, _, _ = await build_pipeline(
        [
            _ai_tool_search("start_agent_task"),
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-start-denied",
                        name="start_agent_task",
                        arguments=json.dumps(start_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action(),
        ]
    )

    await pipeline.run_wakeup_turn(
        "30 秒到了，请处理提醒。",
        target={"target_type": "private", "target_id": 123},
        mode="wakeup",
    )

    assert len(provider.calls) == 3
    third_messages = provider.calls[2]["messages"]
    denied_records = [
        json.loads(str(message.get("content") or "{}"))
        for message in third_messages
        if message.get("role") == "tool"
        and "tc-start-denied" == message.get("tool_call_id")
    ]
    assert denied_records
    assert denied_records[-1]["status"] == "denied"


@pytest.mark.asyncio
async def test_context_budget_uses_provider_context_length(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    pipeline.model_context_length = 1_000_000

    assert pipeline._context_budget().max_context_tokens == 300_000
    assert _recommended_context_budget("deepseek-v4-pro", 1_000_000) == 350_000

    pipeline.behavior_cfg.context.max_context_tokens = 12345
    assert pipeline._context_budget().max_context_tokens == 12345


@pytest.mark.asyncio
async def test_send_result_msg_id_can_be_recalled_same_turn(build_pipeline):
    """发送工具即时返回 msg_id，后续工具轮可立刻撤回刚发出的消息。"""
    send_args = {
        "targets": [{"target_qq": 123, "content": "这条会撤回", "order": 1, "delay": 0}],
    }
    recall_args = {"message_id": 1000}
    pipeline, _, adapter, _, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-recall",
                        name="recall_message",
                        arguments=json.dumps(recall_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action(),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="发完撤回"))
    await _drain_pipeline(pipeline)

    assert [content for _, content in adapter.sent] == ["这条会撤回"]
    assert adapter.recalled == ["1000"]


@pytest.mark.asyncio
async def test_no_action_finishes_silently(build_pipeline):
    """no_action 不应触发任何发送。"""
    pipeline, _, adapter, history, _ = await build_pipeline([_ai_no_action()])

    await pipeline.enqueue(_msg(text="测试 noop"))
    await _drain_pipeline(pipeline)

    assert adapter.sent == []
    records = await history.records()
    assert any(r.get("role") == "user" for r in records)
    assert any(r.get("role") == "assistant" for r in records)


@pytest.mark.asyncio
async def test_send_private_immediate_path_reaches_adapter(build_pipeline):
    """send_private_messages 即时发送路径必须把内容送到 adapter。"""
    pipeline, _, adapter, _, _ = await build_pipeline(
        [_ai_send_private(target_qq="456", content="键名一致性测试")]
    )

    await pipeline.enqueue(_msg(user_id="456", text="hi"))
    await _drain_pipeline(pipeline)

    assert any(c == "键名一致性测试" for _, c in adapter.sent)


@pytest.mark.asyncio
async def test_send_private_emoji_reaches_image_adapter_and_timeline(build_pipeline, tmp_path):
    emoji_dir = tmp_path / "emoji"
    emoji_dir.mkdir()
    emoji_path = emoji_dir / "无语.png"
    emoji_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    args = {
        "targets": [{"target_qq": 456, "emoji": "无语", "order": 1, "delay": 0}],
    }
    pipeline, _, adapter, _, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-emoji",
                        name="send_private_messages",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            )
        ],
        emoji_dir=emoji_dir,
    )

    await pipeline.enqueue(_msg(user_id="456", text="发个表情"))
    await _drain_pipeline(pipeline)

    assert adapter.sent == []
    assert adapter.image_sent[0][1]["image_path"] == emoji_path
    messages = pipeline.chat_timeline.recent("private:456", 10)
    markdown = pipeline.chat_timeline.to_markdown(messages)
    assert "我(999)：[表情包: 无语] [msg_id=1000]" in markdown


@pytest.mark.asyncio
async def test_chat_timeline_records_real_inbound_and_successful_outbound(build_pipeline):
    """真实 QQ 时间线只记录已进入处理的入站和 adapter 成功返回后的出站。"""
    pipeline, _, adapter, _, _ = await build_pipeline(
        [_ai_send_private(target_qq="456", content="真实回复")]
    )

    await pipeline.enqueue(_msg(user_id="456", text="真实入站", message_id="in-1"))
    await _drain_pipeline(pipeline)

    assert [content for _, content in adapter.sent] == ["真实回复"]
    messages = pipeline.chat_timeline.recent("private:456", 10)
    markdown = pipeline.chat_timeline.to_markdown(messages)
    assert "用户(456)：真实入站 [msg_id=in-1]" in markdown
    assert "我(999)：真实回复 [msg_id=1000]" in markdown

    ctx = pipeline._build_tool_context(conversation_id="private:456")
    executor = pipeline.tool_registry.get_executor(ctx)
    result = await executor("get_recent_chat_messages", {"limit": 10})
    assert result["ok"] is True
    assert result["status"] == "inline"
    assert "真实入站" in result["content"]
    assert "真实回复" in result["content"]


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


@pytest.mark.asyncio
async def test_send_private_with_delay_returns_accepted_pending(build_pipeline):
    """多条且存在正 delay 时，工具先返回 accepted，后台仍按原拆条发完。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "第一条", "order": 1, "delay": 0.05},
            {"target_qq": 123, "content": "第二条", "order": 2, "delay": 0.05},
        ],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="连发测试"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    assert [content for _, content in adapter.sent] == ["第一条", "第二条"]
    records = await history.records()
    tool_contents = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    assert tool_contents[-1]["status"] == "accepted"
    assert tool_contents[-1]["accepted"] is True
    assert tool_contents[-1]["delivery"] == "pending"
    assert tool_contents[-1]["qq_visible"] == "pending"
    assert tool_contents[-1]["accepted_messages"][0]["content"] == "第一条"
    assert tool_contents[-1]["data"]["conversation_ids"] == ["private:123"]
    assert tool_contents[-1]["data"]["message_count"] == 2
    assert tool_contents[-1]["result_format"] == "structured_json"
    assert isinstance(tool_contents[-1]["brief"], str)
    assert tool_contents[-1]["brief"].strip()
    assert any(
        r.get("role") == "user"
        and r.get("metadata", {}).get("kind") == "send_done_snapshot"
        and "发送完成（全部消息已发出）" in (r.get("content") or "")
        for r in records
    )
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_other_conversation_does_not_interrupt_async_send(build_pipeline):
    """A 会话后台发送时，B 会话入站只排自己的轮，不冲掉 A 的队列。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "A1", "order": 1, "delay": 0.1},
            {"target_qq": 123, "content": "A2", "order": 2, "delay": 0.1},
        ],
    }
    pipeline, provider, adapter, _, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            CompletionResult(
                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
            ),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="A开始", message_id="a1"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="9", group_id="5555", text="B插话", message_id="b1"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["A1", "A2"]
    assert len(provider.calls) == 3

