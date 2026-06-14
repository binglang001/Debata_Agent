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
import copy
import json
import time
from typing import Any

import pytest

from adapters.base import IAdapter
from adapters.types import (
    IncomingMessage,
    IncomingNotice,
    MediaSegment,
    MediaType,
    NoticeType,
    Target,
    UserInfo,
)
from agents import ChatAgent, Persona
from agents.base import AgentRunResult
from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    BehaviorConfig,
    FeaturesConfig,
    LongTermMemoryConfig,
    NapCatAdapterConfig,
    PersonaConfig,
    ProviderConfig,
    RateLimitConfig,
    RootConfig,
    SummarizeConfig,
    TypingConfig,
)
from core.message_pipeline import (
    MessagePipeline,
    _recommended_context_budget,
    _text_mentions_self_or_role,
)
from core.proactive_loop import ProactiveLoop
from core.recall_handler import RecallHandler
from core.state import PendingMessageItem, PendingRequestStore, RateLimiter
from core.wakeup import WakeupScheduler
from memory import (
    ArchiveStore,
    EventStore,
    HistoryManager,
    ImportantMemoryManager,
    RollingSummaryStore,
)
from providers.base import CompletionResult, IProvider, ToolCall, Usage
from tools import ToolContext, build_default_registry

# ============================================================
# 配置/Persona 工厂
# ============================================================


def _make_persona() -> Persona:
    return Persona(
        name="test_persona",
        prompt="<identity>你是测试用 AI</identity>",
        vars={"name": "测试机器人", "admins": []},
    )


def _make_agent_cfg() -> AgentConfig:
    return AgentConfig(
        provider="fake",
        model="fake-1",
        temperature=0.6,
        max_tokens=1024,
        max_loops=3,
        refocus_interval=0,
        first_token_timeout_seconds=5.0,
    )


def _make_root_config() -> RootConfig:
    """最小可用 RootConfig：刚好够 build_default_registry 与 pipeline 跑通。"""
    return RootConfig(
        providers={
            "fake": ProviderConfig(
                preset=None,
                protocol="openai_compat",
                base_url="https://example.com",
                api_key_id=None,
            ),
        },
        adapters={"napcat": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=_make_agent_cfg()),
        features=FeaturesConfig(
            long_term_memory=LongTermMemoryConfig(mode="file"),
        ),
        persona=PersonaConfig(active="test_persona"),
        behavior=BehaviorConfig(
            merge_window_seconds=0.05,
            recall_merge_window_seconds=0.05,
            proactive_think_interval_seconds=600.0,
            default_history_fetch_count=10000,
            typing=TypingConfig(
                chars_per_second=999.0,
            ),
            rate_limit=RateLimitConfig(window_seconds=60, max_messages=100, enabled=False),
            summarize=SummarizeConfig(),
        ),
    )


# ============================================================
# ScriptedProvider —— 每次 chat_completion 弹出一个预设结果
# ============================================================


class ScriptedProvider(IProvider):
    def __init__(self, script: list[CompletionResult]) -> None:
        super().__init__(name="scripted")
        self._script: list[CompletionResult] = list(script)
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        self.calls.append({"messages": copy.deepcopy(messages), "model": model, "tools": tools})
        if not self._script:
            # 默认无脚本时返回 no_action 收尾（防止死循环）
            tc = ToolCall(id="auto", name="no_action", arguments="{}")
            return CompletionResult(tool_calls=[tc], finish_reason="tool_calls", usage=Usage())
        return self._script.pop(0)

    async def aclose(self) -> None:
        pass


# ============================================================
# FakeAdapter —— 实现 IAdapter，所有发送拦截到 self.sent
# ============================================================


class FakeAdapter(IAdapter):
    def __init__(self, name: str = "fake") -> None:
        super().__init__(name)
        self.sent: list[tuple[Target, str]] = []
        self.image_sent: list[tuple[Target, dict[str, Any]]] = []
        self.voice_sent: list[tuple[Target, Any]] = []
        self.uploaded: list[tuple[Target, Any, str | None]] = []
        self.recalled: list[str] = []
        self.voice_text = ""
        self.voice_fetch_calls: list[str] = []
        self._connected = True
        self._next_msg_id = 1000

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send_text(self, target: Target, content: str) -> str:
        mid = str(self._next_msg_id)
        self._next_msg_id += 1
        self.sent.append((target, content))
        return mid

    async def send_voice(self, target: Target, audio_path) -> str:
        mid = str(self._next_msg_id)
        self._next_msg_id += 1
        self.voice_sent.append((target, audio_path))
        return mid

    async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None):
        mid = str(self._next_msg_id)
        self._next_msg_id += 1
        self.image_sent.append(
            (
                target,
                {
                    "image_path": image_path,
                    "image_url": image_url,
                    "image_b64": image_b64,
                },
            )
        )
        return mid

    async def recall(self, message_id: str) -> bool:
        self.recalled.append(message_id)
        return True

    async def list_friends(self):
        return []

    async def list_groups(self):
        return []

    async def list_group_members(self, group_id: str):
        return []

    async def get_user_info(self, user_id: str) -> UserInfo:
        return UserInfo(user_id=user_id, nickname="unknown")

    async def handle_friend_request(self, flag, approve, remark=""): ...
    async def handle_group_request(self, flag, sub_type, approve, reason=""): ...

    async def call_api(self, action: str, **params: Any) -> dict:
        return {}

    async def fetch_voice_text(self, message_id: str) -> str:
        self.voice_fetch_calls.append(message_id)
        return self.voice_text

    async def upload_file(self, target, file_path, *, display_name=None):
        self.uploaded.append((target, file_path, display_name))


class FailingQQSentEventStore:
    def __init__(self) -> None:
        self.appended_events: list[dict[str, Any]] = []

    async def append_event(self, **event: Any) -> int:
        self.appended_events.append(event)
        if event.get("event_type") == "qq_message_sent":
            raise RuntimeError("append log failed")
        return len(self.appended_events)


class ProjectionFailingEventStore(EventStore):
    def _project_events_sync(self, events: list[Any]) -> None:
        raise RuntimeError("projection failed")


# ============================================================
# fixture：构造完整 pipeline
# ============================================================


@pytest.fixture
def build_pipeline(tmp_path):
    """返回 async 工厂。调用 build(script) 得到 (pipeline, provider, adapter, history, important)。"""

    async def _build(
        script: list[CompletionResult],
        *,
        emoji_dir=None,
        workspace_dir=None,
        rate_limiter=None,
        event_store=None,
        persona_agent=None,
        subconscious_agent=None,
        persona_db=None,
        eat_tool=False,
        sleep_tool=False,
    ):
        cfg = _make_root_config()
        provider = ScriptedProvider(script)
        chat_agent = ChatAgent(provider, cfg.agents.chat)
        persona = _make_persona()
        history = HistoryManager(tmp_path / "history.jsonl")
        important = ImportantMemoryManager(tmp_path / "important.json")
        archive = ArchiveStore(tmp_path / "archive.sqlite3")
        rolling_summary = RollingSummaryStore(tmp_path / "rolling_summary.json")
        await history.load()
        await important.load()
        await archive.load()
        await rolling_summary.load()
        registry = build_default_registry(cfg)
        adapter = FakeAdapter("fake")
        scheduler = WakeupScheduler(
            on_fire=lambda r, target=None, mode="wakeup", message_text=None: asyncio.sleep(0)
        )

        pipeline = MessagePipeline(
            adapter=adapter,
            chat_agent=chat_agent,
            persona=persona,
            history=history,
            important=important,
            archive=archive,
            rolling_summary=rolling_summary,
            tool_registry=registry,
            wakeup_scheduler=scheduler,
            pending_requests=PendingRequestStore(),
            behavior_cfg=cfg.behavior,
            features_cfg=cfg.features,
            emoji_dir=emoji_dir,
            workspace_dir=workspace_dir,
            rate_limiter=rate_limiter,
            summary_agent=None,
            event_store=event_store,
            persona_agent=persona_agent,
            subconscious_agent=subconscious_agent,
            persona_db=persona_db,
            eat_tool=eat_tool,
            sleep_tool=sleep_tool,
        )
        scheduler._on_fire = pipeline.run_wakeup_turn  # 双向依赖回填
        return pipeline, provider, adapter, history, important

    return _build


# ============================================================
# Helpers
# ============================================================


def _msg(
    *,
    user_id: str = "123",
    group_id: str | None = None,
    text: str = "你好",
    message_id: str = "m1",
    reply_to: str | None = None,
) -> IncomingMessage:
    scope = "group" if group_id else "private"
    return IncomingMessage(
        adapter="fake",
        timestamp=1.0,
        self_id="999",
        message_id=message_id,
        scope=scope,
        user_id=user_id,
        nickname="用户",
        group_id=group_id,
        text=text,
        raw_message=text,
        reply_to=reply_to,
    )


def _ai_send_private(target_qq: str = "123", content: str = "嗨") -> CompletionResult:
    args = {
        "targets": [{"target_qq": target_qq, "content": content, "order": 1, "delay": 0}],
    }
    tc = ToolCall(id="tc-1", name="send_private_messages", arguments=json.dumps(args))
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls")


def _ai_send_group(group_id: str = "5555", content: str = "群好") -> CompletionResult:
    args = {
        "group_id": int(group_id),
        "targets": [{"content": content, "order": 1, "delay": 0}],
    }
    tc = ToolCall(id="tc-g", name="send_group_message", arguments=json.dumps(args))
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls")


def _ai_no_action(reason: str = "无需回复") -> CompletionResult:
    _ = reason
    tc = ToolCall(id="tc-na", name="no_action", arguments="{}")
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls")


def _ai_tool_search(tool_name: str) -> CompletionResult:
    tc = ToolCall(
        id=f"tc-search-{tool_name}",
        name="tool_search",
        arguments=json.dumps({"tool_name": tool_name}),
    )
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls")


def _approve_stub_tools(ctx: ToolContext, *names: str) -> None:
    approved = ctx.extras.setdefault("tool_search_approved_tools", set())
    assert isinstance(approved, set)
    approved.update(names)


def test_text_mentions_self_or_role_uses_deterministic_tokens():
    assert _text_mentions_self_or_role("@QQ999 在吗", "999", "测试机器人") is True
    assert _text_mentions_self_or_role("[CQ:at,qq=999] 在吗", "999", "测试机器人") is True
    assert _text_mentions_self_or_role("@测试机器人 在吗", "999", "测试机器人") is True
    assert _text_mentions_self_or_role("普通插话", "999", "测试机器人") is False


async def _drain_pipeline(pipeline: MessagePipeline, max_wait: float = 1.0) -> None:
    """等待 batch + agent + send 全部完成。"""
    elapsed = 0.0
    step = 0.05
    while elapsed < max_wait:
        await asyncio.sleep(step)
        elapsed += step
        batch_task = pipeline._batch_task
        requeue = pipeline._requeue_task
        rag_tasks = getattr(pipeline, "_rag_memory_tasks", set())
        send_states = getattr(getattr(pipeline, "_send_manager", None), "_states", {})
        send_workers_done = all(
            state.worker is None or state.worker.done()
            for state in send_states.values()
        )
        receipt_tasks = getattr(pipeline, "_send_receipt_tasks", {})
        receipt_tasks_done = all(task.done() for task in receipt_tasks.values())
        if (
            (batch_task is None or batch_task.done())
            and (requeue is None or requeue.done())
            and all(t.done() for t in rag_tasks)
            and send_workers_done
            and receipt_tasks_done
        ):
            return
    raise AssertionError(f"pipeline 在 {max_wait}s 内未完成")


async def _wait_until(predicate, max_wait: float = 1.0) -> None:
    elapsed = 0.0
    step = 0.02
    while elapsed < max_wait:
        if predicate():
            return
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError("等待条件超时")


class RecordingPersonaAgent:
    def __init__(
        self,
        context: str = "",
        *,
        current_action: str = "awake",
        action_until: float | None = None,
    ) -> None:
        self.context = context
        self.current_action = current_action
        self.action_until = action_until
        self.context_calls: list[str | None] = []
        self.after_turn_calls: list[dict[str, Any]] = []

    def get_context_for_chat(self, conversation_id: str | None) -> str:
        self.context_calls.append(conversation_id)
        return self.context

    def is_resting(self) -> bool:
        if self.current_action not in {"eating", "sleeping", "collapsing"}:
            return False
        return self.action_until is None or self.action_until > time.time()

    async def after_turn(
        self,
        conversation_id: str,
        participants: Any,
        chat_summary: str,
    ) -> None:
        self.after_turn_calls.append(
            {
                "conversation_id": conversation_id,
                "participants": participants,
                "chat_summary": chat_summary,
            }
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


async def _add_history_until_active_tokens(
    pipeline: MessagePipeline,
    history: HistoryManager,
    *,
    min_tokens: int,
    prefix: str,
    conversation_id: str = "private:123",
) -> None:
    estimator = pipeline._token_estimator()
    for _ in range(80):
        active = await pipeline._select_working_history(conversation_id)
        if estimator.estimate_messages(active) >= min_tokens:
            return
        idx = await history.length()
        await history.add_user_message(
            f"{prefix} {idx} " + ("很长的预算测试内容 " * 260),
            conversation_id=conversation_id,
        )
    active = await pipeline._select_working_history(conversation_id)
    raise AssertionError(
        f"未能构造足够长的活跃历史: {estimator.estimate_messages(active)} < {min_tokens}"
    )


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
async def test_working_window_uses_unified_recent_timeline(build_pipeline):
    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    await history.add_user_message("群里的旧内容也属于统一人格时间线", conversation_id="group:1")
    await history.add_user_message("私聊旧内容应该保留", conversation_id="private:123")

    await pipeline.enqueue(_msg(user_id="123", text="继续私聊"))
    await _drain_pipeline(pipeline)

    joined = "\n".join(str(m.get("content", "")) for m in provider.calls[-1]["messages"])
    assert "私聊旧内容应该保留" in joined
    assert "群里的旧内容也属于统一人格时间线" in joined


@pytest.mark.asyncio
async def test_working_window_guarantees_current_conversation_recent_records(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.behavior_cfg.context.max_context_tokens = 14_000
    await history.add_user_message("私聊关键口令：KEEP-ME", conversation_id="private:123")
    for idx in range(30):
        await history.add_user_message(
            f"高频群聊消息 {idx} " + ("占位内容 " * 500),
            conversation_id="group:1",
        )

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "私聊关键口令：KEEP-ME" in joined
    assert "高频群聊消息 29" in joined


@pytest.mark.asyncio
async def test_working_history_without_conversation_uses_normal_budget(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(20):
        await history.add_user_message(
            f"全局消息 {idx} " + ("占位内容 " * 200),
            conversation_id=f"private:{idx}",
        )

    selected = await pipeline._select_working_history(None)
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "全局消息 19" in joined
    assert "全局消息 0" in joined


@pytest.mark.asyncio
async def test_working_history_starts_after_rolling_summary_active_start_index(
    build_pipeline,
):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(5):
        await history.add_user_message(
            f"活跃游标消息 {idx}",
            conversation_id="private:123",
        )
    await pipeline.rolling_summary.update(
        "已摘要前两条",
        archived_until={"legacy": "old"},
        active_start_index=2,
        updated_at="test",
    )

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "活跃游标消息 0" not in joined
    assert "活跃游标消息 1" not in joined
    assert "活跃游标消息 2" in joined
    assert "活跃游标消息 4" in joined


@pytest.mark.asyncio
async def test_working_history_budget_uses_context_budget_and_prompt_overhead(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    context = pipeline.behavior_cfg.context
    context.max_context_tokens = 20_000
    context.reserve_output_tokens = 2_000
    context.memory_token_budget = 3_000
    context.summary_token_budget = 4_000
    context.prompt_overhead_estimate_tokens = 5_000

    assert pipeline._working_history_budget() == 6_000

    context.prompt_overhead_estimate_tokens = 20_000

    assert pipeline._working_history_budget() == 1


@pytest.mark.asyncio
async def test_working_history_keeps_runtime_context_records_in_active_window(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_user_message("群聊真实旧消息仍属于统一时间线", conversation_id="group:1")
    await history.add_user_message("私聊真实旧消息仍应保留", conversation_id="private:123")
    runtime_records = []
    for idx in range(20):
        runtime_records.extend(
            [
                {
                    "role": "user",
                    "content": (
                        "<task_context priority=\"medium\">\n"
                        f"旧运行时上下文 {idx}\n"
                        "</task_context>"
                    ),
                    "metadata": {"kind": "task_context_snapshot"},
                    "conversation_id": "group:runtime",
                },
                {
                    "role": "user",
                    "content": (
                        "<send_status>\n"
                        f"旧清洁发送状态 {idx}\n"
                        "</send_status>"
                    ),
                    "metadata": {"kind": "send_done_snapshot"},
                    "conversation_id": "group:runtime",
                },
            ]
        )
    await history.add_records(runtime_records)
    await history.add_user_message("当前触发消息", conversation_id="private:123")

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(r.get("content", "")) for r in selected)

    assert "群聊真实旧消息仍属于统一时间线" in joined
    assert "私聊真实旧消息仍应保留" in joined
    assert "当前触发消息" in joined
    assert "旧运行时上下文 0" in joined
    assert "旧清洁发送状态 0" in joined
    assert "旧运行时上下文 19" in joined
    assert "旧清洁发送状态 19" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_runtime_noise_before_budget_cutoff(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.behavior_cfg.context.max_context_tokens = 18_000
    await history.add_user_message("预算内应保留的真实旧聊天 KEEP-REAL", conversation_id="group:1")
    for idx in range(80):
        await history.add_records(
            [
                {
                    "role": "user",
                    "content": (
                        "<task_context priority=\"medium\">\n"
                        f"巨大旧运行时噪声 {idx} " + ("填充 " * 120) + "\n"
                        "</task_context>"
                    ),
                    "metadata": {"kind": "task_context_snapshot"},
                    "conversation_id": "group:noise",
                }
            ]
        )
    await history.add_user_message("当前触发消息", conversation_id="private:123")

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(r.get("content", "")) for r in selected)

    assert "预算内应保留的真实旧聊天 KEEP-REAL" in joined
    assert "巨大旧运行时噪声 0" in joined
    assert "当前触发消息" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_all_active_current_conversation_runtime(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(16):
        await history.add_records(
            [
                {
                    "role": "user",
                    "content": (
                        "<task_context priority=\"medium\">\n"
                        f"当前会话近期运行时 {idx}\n"
                        "</task_context>"
                    ),
                    "metadata": {"kind": "task_context_snapshot"},
                    "conversation_id": "private:123",
                }
            ]
        )

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(r.get("content", "")) for r in selected)

    assert "当前会话近期运行时 0" in joined
    assert "当前会话近期运行时 15" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_recent_send_receipt_fields(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(16):
        legacy_receipt = {
            "interrupted": True,
            "send_id": f"legacy-{idx}",
            "sent": [{"content": f"已发-{idx}", "order": 1, "msg_id": f"s-{idx}"}],
            "unsent": [
                {
                    "content": f"旧 JSON 未发-{idx}",
                    "order": 2,
                    "send_id": f"legacy-{idx}",
                    "conversation_id": "group:5555",
                }
            ],
            "new_messages": [
                {
                    "conversation_id": "group:5555",
                    "user_id": "123",
                    "nickname": f"用户{idx}",
                    "text": f"旧 JSON 新消息 {idx}-1",
                    "seq": idx * 10 + 1,
                    "time": "10:00",
                    "msg_id": f"legacy-new-{idx}-1",
                },
                {
                    "conversation_id": "group:5555",
                    "user_id": "123",
                    "nickname": f"用户{idx}",
                    "text": f"旧 JSON 新消息 {idx}-2",
                    "seq": idx * 10 + 2,
                    "time": "10:01",
                    "msg_id": f"legacy-new-{idx}-2",
                },
            ],
            "recalled_messages": [
                {
                    "msg_id": f"legacy-recall-{idx}",
                    "conversation_id": "group:5555",
                    "note": "旧 JSON 撤回",
                }
            ],
            "errors": [{"order": 3, "error": "旧 JSON 错误"}],
            "accepted_messages": [{"content": "不应进入 prompt 的 accepted"}],
            "irrelevant_raw_payload": "不应进入 prompt 的无关字段",
        }
        await history.add_records(
            [
                {
                    "role": "user",
                    "content": (
                        "<send_receipt>\n"
                        "系统说明：运行时发送状态；按 JSON 字段判断。\n"
                        f"{json.dumps(legacy_receipt, ensure_ascii=False)}\n"
                        "</send_receipt>"
                    ),
                    "conversation_id": "group:5555",
                }
            ]
        )
    await history.add_records(
        [
            {
                "role": "user",
                "content": (
                    "<send_receipt>\n"
                    "发送回执：send-latest\n"
                    "会话：group:5555\n"
                    "状态：部分发送；发送期间被新消息打断（interrupted=true）。\n"
                    "未发送 1 条：\n"
                    "1. 未发；order=2；send_id=send-latest；conversation_id=group:5555\n"
                    "新消息 1 条：\n"
                    "- 用户（group:5555；user_id=123）1 条；样例：\"新消息\"；"
                    "最新 seq=8/time=10:00/msg_id=m-new\n"
                    "撤回消息 1 条：\n"
                    "1. msg_id=m1；conversation_id=group:5555；note=用户撤回\n"
                    "</send_receipt>"
                ),
                "conversation_id": "group:5555",
            }
        ]
    )

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(str(r.get("content", "")) for r in selected)
    raw_joined = "\n".join(str(r.get("content", "")) for r in await history.records())

    assert '"new_messages"' in joined
    assert "accepted_messages" in joined
    assert "irrelevant_raw_payload" in joined
    assert "不应进入 prompt 的 accepted" in joined
    assert "不应进入 prompt 的无关字段" in joined
    assert '"new_messages"' in raw_joined
    assert "accepted_messages" in raw_joined
    assert '"send_id": "legacy-15"' in joined
    assert "旧 JSON 未发-15" in joined
    assert "旧 JSON 新消息 15-1" in joined
    assert "legacy-recall-15" in joined
    assert "旧 JSON 错误" in joined
    assert "发送回执：send-latest" in joined
    assert "未发；order=2；send_id=send-latest" in joined
    assert "样例：\"新消息\"" in joined
    assert "msg_id=m1" in joined


@pytest.mark.asyncio
async def test_format_send_receipt_summarizes_spam_without_full_json(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    long_sent_content = "已发-" + "长" * 220
    receipt = {
        "type": "send_receipt",
        "send_id": "send-token",
        "conversation_id": "private:123",
        "interrupted": True,
        "sent": [
            {
                "content": long_sent_content if idx == 0 else f"已发-{idx}",
                "order": idx + 1,
                "msg_id": f"sent-{idx}",
                "conversation_id": "private:123",
            }
            for idx in range(3)
        ]
        + [
            {
                "content": "已发-隐藏",
                "order": 4,
                "msg_id": "sent-hidden",
                "conversation_id": "private:123",
            }
        ],
        "unsent": [
            {
                "content": f"未发-{idx}",
                "order": idx + 10,
                "send_id": "send-token",
                "conversation_id": "private:123",
            }
            for idx in range(5)
        ]
        + [
            {
                "content": "未发-隐藏",
                "order": 99,
                "send_id": "send-token",
                "conversation_id": "private:123",
            }
        ],
        "new_messages": [
            {
                "conversation_id": "private:123",
                "user_id": "123",
                "nickname": "用户",
                "text": f"刷屏 {idx}",
                "seq": idx + 1,
                "time": f"10:{idx:02d}",
                "msg_id": f"m-spam-{idx}",
                "priority_reasons": ["private_message"] if idx == 0 else [],
                "priority_reason": "focus_user" if idx == 1 else "",
            }
            for idx in range(20)
        ],
        "recalled_messages": [
            {
                "conversation_id": "private:123",
                "time": "10:30",
                "msg_id": "m-recall",
                "note": "用户撤回",
                "qq_visible": False,
            }
        ],
        "errors": ["order=9: boom", {"order": 10, "error": "timeout"}],
        "accepted_messages": [{"content": "不应重复出现的 accepted 内容"}],
    }

    summary = pipeline._format_send_receipt(receipt)

    assert "<send_receipt>" in summary
    assert "</send_receipt>" in summary
    assert '"new_messages"' not in summary
    assert '{"conversation_id"' not in summary
    assert summary.count("msg_id=m-spam-") == 1
    assert "新消息 20 条：" in summary
    assert "用户（private:123；user_id=123）20 条" in summary
    assert "样例：\"刷屏 0\"" in summary
    assert "最新 seq=20/time=10:19/msg_id=m-spam-19" in summary
    assert "priority_reasons=private_message,focus_user" in summary
    assert "已发送 4 条：" in summary
    expected_sent_content = f"{long_sent_content[:157]}..."
    assert (
        f"{expected_sent_content}；order=1；msg_id=sent-0；conversation_id=private:123"
        in summary
    )
    assert long_sent_content not in summary
    assert "已发-隐藏" not in summary
    assert "... 另有 1 条未列出。" in summary
    assert "未发送 6 条：" in summary
    assert "未发-4；order=14；send_id=send-token；conversation_id=private:123" in summary
    assert "未发-隐藏" not in summary
    assert "撤回消息 1 条：" in summary
    assert "msg_id=m-recall；conversation_id=private:123；time=10:30；note=用户撤回" in summary
    assert "错误 2 条：" in summary
    assert "order=9: boom" in summary
    assert "error=timeout；order=10" in summary
    assert "不应重复出现的 accepted 内容" not in summary


@pytest.mark.asyncio
async def test_format_send_receipt_limits_new_message_groups(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    receipt = {
        "type": "send_receipt",
        "send_id": "send-groups",
        "conversation_id": "group:0",
        "interrupted": True,
        "sent": [],
        "unsent": [],
        "new_messages": [
            {
                "conversation_id": f"group:{idx}",
                "user_id": str(idx),
                "nickname": f"用户{idx}",
                "text": f"消息 {idx}",
                "seq": idx + 1,
                "time": f"10:{idx:02d}",
                "msg_id": f"m-group-{idx}",
            }
            for idx in range(8)
        ],
    }

    summary = pipeline._format_send_receipt(receipt)

    assert "新消息 8 条：" in summary
    assert "用户0（group:0；user_id=0）1 条" in summary
    assert "用户5（group:5；user_id=5）1 条" in summary
    assert "用户6（group:6；user_id=6）1 条" not in summary
    assert "... 另有 2 组未列出。" in summary
    assert '"new_messages"' not in summary


@pytest.mark.asyncio
async def test_working_history_keeps_complete_no_action_pairs_in_active_window(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(14):
        await history.add_records(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"tc-na-{idx}",
                            "type": "function",
                            "function": {"name": "no_action", "arguments": "{}"},
                        }
                    ],
                    "conversation_id": "group:5555",
                },
                {
                    "role": "tool",
                    "tool_call_id": f"tc-na-{idx}",
                    "content": '{"ok": true, "no_action": true}',
                    "conversation_id": "group:5555",
                },
            ]
        )
    await history.add_user_message("真实聊天不能被 no_action 清理影响", conversation_id="group:5555")

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(json.dumps(r, ensure_ascii=False) for r in selected)

    assert "真实聊天不能被 no_action 清理影响" in joined
    assert "tc-na-0" in joined
    assert "tc-na-13" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_incomplete_or_non_no_action_tool_pairs(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_records(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-send",
                        "type": "function",
                        "function": {"name": "send_group_message", "arguments": "{}"},
                    }
                ],
                "conversation_id": "group:5555",
            },
            {
                "role": "tool",
                "tool_call_id": "tc-send",
                "content": '{"ok": true, "status": "accepted"}',
                "conversation_id": "group:5555",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-na-incomplete",
                        "type": "function",
                        "function": {"name": "no_action", "arguments": "{}"},
                    }
                ],
                "conversation_id": "group:5555",
            },
        ]
    )
    for idx in range(14):
        await history.add_records(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"tc-old-na-{idx}",
                            "type": "function",
                            "function": {"name": "no_action", "arguments": "{}"},
                        }
                    ],
                    "conversation_id": "group:old",
                },
                {
                    "role": "tool",
                    "tool_call_id": f"tc-old-na-{idx}",
                    "content": '{"ok": true, "no_action": true}',
                    "conversation_id": "group:old",
                },
            ]
        )

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(json.dumps(r, ensure_ascii=False) for r in selected)

    assert "tc-send" in joined
    assert "accepted" in joined
    assert "tc-na-incomplete" in joined
    assert "tc-old-na-0" in joined


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
async def test_cross_conversation_clean_send_receipt_visible_in_unified_window(build_pipeline):
    """群里触发的私聊异步发送：accepted 在群轮，完成记录在私聊目标，但统一窗口都能看到。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "私聊第一条", "order": 1, "delay": 0.05},
            {"target_qq": 123, "content": "私聊第二条", "order": 2, "delay": 0.05},
        ],
    }
    pipeline, _, _, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-cross-send",
                        name="send_private_messages",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="群里触发私聊发送"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    records = await history.records()
    assert any(
        r.get("role") == "tool"
        and r.get("conversation_id") == "group:5555"
        and json.loads(r.get("content") or "{}").get("status") == "accepted"
        for r in records
    )
    assert any(
        r.get("role") == "user"
        and r.get("conversation_id") == "private:123"
        and r.get("metadata", {}).get("kind") == "send_done_snapshot"
        and "发送完成（全部消息已发出）" in (r.get("content") or "")
        for r in records
    )

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(str(r.get("content", "")) for r in selected)
    assert '"status": "accepted"' in joined
    assert "发送完成（全部消息已发出）" in joined


@pytest.mark.asyncio
async def test_same_conversation_interrupt_flushes_async_send_queue(build_pipeline):
    """同会话插话会打断后台发送，未发气泡进回执，不再正常批处理重复一轮。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "一", "order": 1, "delay": 0.2},
            {"target_qq": 123, "content": "二", "order": 2, "delay": 0.2},
            {"target_qq": 123, "content": "三", "order": 3, "delay": 0.2},
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
            ),
            _ai_no_action("看到插话后先不补发"),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="开始连发", message_id="m-start"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="123", text="插话", message_id="m-interrupt"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["一"]
    assert len(provider.calls) == 3
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "m-interrupt" in joined
    assert "<send_receipt>" in joined
    assert "状态：部分发送；发送期间被新消息打断（interrupted=true）。" in joined
    assert "未发送 2 条：" in joined
    assert "二；order=2" in joined
    assert "三；order=3" in joined
    assert "新消息 1 条：" in joined
    assert "最新 seq=2/" in joined
    receipt_turn_context = "\n".join(
        str(m.get("content", ""))
        for m in provider.calls[-1]["messages"]
        if m.get("role") == "user" and "<send_receipt_task" in str(m.get("content", ""))
    )
    assert "<send_receipt>" in receipt_turn_context
    assert "interrupted=true" in receipt_turn_context
    assert "最新 seq=2/" in receipt_turn_context
    assert "seq=无" not in receipt_turn_context
    assert '"new_messages"' not in receipt_turn_context
    assert "按回执摘要判断" in receipt_turn_context
    assert "按 JSON 字段判断" not in receipt_turn_context


@pytest.mark.asyncio
async def test_group_priority_interrupt_allows_unrelated_async_chat(build_pipeline):
    """群聊默认 interrupt_priority：其他人普通插话不冲掉已排队的短回应。"""
    args = {
        "group_id": 5555,
        "targets": [
            {"content": "一", "order": 1, "delay": 0.2},
            {"content": "二", "order": 2, "delay": 0.2},
        ],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action("处理普通插话"),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="开始", message_id="m-start"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="路过插话", message_id="m-other"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["一", "二"]
    assert not pipeline._send_manager.should_defer_batch("group:5555")
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "路过插话" in joined
    assert '"interrupted": true' not in joined
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_group_priority_interrupt_stops_same_trigger_user_followup(build_pipeline):
    """同触发用户追问是确定性高优先级事件，仍会阻断剩余发送。"""
    args = {
        "group_id": 5555,
        "targets": [
            {"content": "一", "order": 1, "delay": 0.2},
            {"content": "二", "order": 2, "delay": 0.2},
        ],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action("看到追问后先不补发"),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="开始", message_id="m-start"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="等下", message_id="m-follow"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["一"]
    assert len(provider.calls) == 3
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "m-follow" in joined
    assert "<send_receipt>" in joined
    assert "interrupted=true" in joined
    assert "priority_reasons=same_trigger_user" in joined
    assert "二；order=2" in joined


@pytest.mark.asyncio
async def test_same_conversation_message_while_model_thinking_needs_review(build_pipeline):
    """LLM 思考时当前会话来了新消息，旧发送应 needs_review，并把新消息并入同一轮。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1, "delay": 0}],
    }
    no_action = ToolCall(id="tc-na", name="no_action", arguments="{}")
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return CompletionResult(tool_calls=[no_action], finish_reason="tool_calls")

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="我改口", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert adapter.sent == []
    assert call_count == 2
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert '"status": "needs_review"' in joined
    tool_contents = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    review_result = tool_contents[-1]
    assert review_result["status"] == "needs_review"
    assert review_result["qq_visible"] is False
    assert review_result["send_attempt_id"].startswith("attempt-")
    assert review_result["latest_seq"] == 2
    attempted = review_result["attempted_messages"][0]
    assert attempted["send_id"] == review_result["send_attempt_id"]
    assert attempted["conversation_id"] == "private:123"
    assert attempted["target_type"] == "private"
    assert attempted["target_id"] == "123"
    assert attempted["order"] == 1
    assert attempted["content"] == "旧回复"
    assert attempted["delay"] >= 0
    assert attempted["qq_visible"] is False
    assert review_result["unseen_messages"][0]["conversation_id"] == "private:123"
    assert review_result["unseen_messages"][0]["text"] == "我改口"
    assert review_result["unseen_messages"][0]["qq_visible"] is True
    assert review_result["priority_interrupts"][0]["priority_reasons"] == [
        "private_message",
        "focus_user",
    ]
    assert "note" not in review_result
    assert "commit_send_attempt" in review_result["next"]
    assert "m-new" in joined
    assert "<send_receipt>" in joined
    timeline_markdown = pipeline.chat_timeline.to_markdown(
        pipeline.chat_timeline.recent("private:123", 10)
    )
    assert "m-old" in timeline_markdown
    assert "m-new" in timeline_markdown
    assert "旧回复" not in timeline_markdown


@pytest.mark.asyncio
async def test_other_private_message_while_model_thinking_does_not_review_current_send(
    build_pipeline,
):
    """A 私聊思考时 B 私聊来消息，不应让 A 私聊发送 needs_review。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "A回复", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="A先问", message_id="a-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="456", text="B插话", message_id="b-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["A回复"]
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "B插话" in joined
    assert '"status": "needs_review"' not in joined
    send_results = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    assert send_results[-1]["status"] == "sent"
    assert call_count == 3


@pytest.mark.asyncio
async def test_unrelated_group_message_while_model_thinking_does_not_stale_send(build_pipeline):
    """模型思考时普通群聊插话不应让默认群短回应饿死。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "group_id": 5555,
        "targets": [{"content": "短回", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="路过", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["短回"]
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "路过" in joined
    assert '"status": "needs_review"' not in joined
    assert "<send_receipt>" not in joined
    assert call_count == 3


@pytest.mark.asyncio
async def test_group_review_all_requires_review_for_ordinary_unseen_message(build_pipeline):
    """review_all 下，模型思考期间普通群插话也会让发送先复核。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "group_id": 5555,
        "review_policy": "review_all",
        "targets": [{"content": "短回", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="路过", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert adapter.sent == []
    records = await history.records()
    results = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    assert results[-1]["status"] == "needs_review"
    assert results[-1]["unseen_messages"][0]["text"] == "路过"
    assert results[-1]["priority_interrupts"] == []


@pytest.mark.asyncio
async def test_group_focus_user_followup_needs_review_before_send(build_pipeline):
    """focus 用户思考期间追问是确定性高优先级，发送前需复核。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "group_id": 5555,
        "targets": [{"content": "短回", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="等下", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert adapter.sent == []
    records = await history.records()
    results = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    assert results[-1]["status"] == "needs_review"
    assert results[-1]["priority_interrupts"][0]["priority_reasons"] == ["focus_user"]


@pytest.mark.asyncio
async def test_slash_hash_group_text_does_not_trigger_priority_review(build_pipeline):
    """群聊 /xxx、#xxx 普通文本不再天然视为高优先级打断。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "group_id": 5555,
        "targets": [{"content": "短回", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="/other", message_id="m-slash"))
    await pipeline.enqueue(_msg(user_id="789", group_id="5555", text="#topic", message_id="m-hash"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["短回"]
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert '"status": "needs_review"' not in joined
    assert "command_message" not in joined


@pytest.mark.asyncio
async def test_atomic_delivery_policy_does_not_bypass_preflight_review(build_pipeline):
    """atomic 只影响接收后的投递中断，不能绕过发送前 focus 用户复核。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "group_id": 5555,
        "delivery_interrupt_policy": "atomic",
        "targets": [{"content": "短回", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="等下", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert adapter.sent == []
    records = await history.records()
    results = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    assert results[-1]["status"] == "needs_review"
    assert results[-1]["priority_interrupts"][0]["priority_reasons"] == ["focus_user"]


@pytest.mark.asyncio
async def test_needs_review_again_reuses_attempt_and_increments_revision(build_pipeline):
    """commit 前再次出现高优先级未见消息时，复用原 attempt 并递增 revision。"""
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    commit_started = asyncio.Event()
    commit_release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": copy.deepcopy(messages), "model": model, "tools": tools})
        if call_count == 1:
            first_started.set()
            await first_release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        if call_count == 2:
            commit_started.set()
            await commit_release.wait()
            tool_records = [m for m in messages if m.get("role") == "tool"]
            attempt = json.loads(tool_records[-1]["content"])["send_attempt_id"]
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-commit",
                        name="commit_send_attempt",
                        arguments=json.dumps(
                            {
                                "send_attempt_id": attempt,
                                "reviewed_until_seq": 2,
                                "delivery_interrupt_policy": "interrupt_priority",
                            }
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="先问", message_id="m-old"))
    await first_started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="我改口", message_id="m-new"))
    first_release.set()
    await commit_started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="再补一句", message_id="m-third"))
    commit_release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert adapter.sent == []
    records = await history.records()
    results = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") in {"tc-send", "tc-commit"}
    ]
    initial = next(item for item in results if item["status"] == "needs_review")
    again = next(item for item in results if item["status"] == "needs_review_again")
    assert again["send_attempt_id"] == initial["send_attempt_id"]
    assert initial["attempt_revision"] == 1
    assert again["attempt_revision"] == 2
    assert again["revision"] == 2
    assert again["latest_seq"] == 3
    assert again["unseen_messages"][0]["text"] == "再补一句"


@pytest.mark.asyncio
async def test_ignore_review_interrupts_forces_soft_preflight_review(build_pipeline):
    """ignore_review_interrupts=true 可提交软复核打断，并返回 forced_unseen_messages。"""
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    commit_started = asyncio.Event()
    commit_release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": copy.deepcopy(messages), "model": model, "tools": tools})
        if call_count == 1:
            first_started.set()
            await first_release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        if call_count == 2:
            commit_started.set()
            await commit_release.wait()
            tool_records = [m for m in messages if m.get("role") == "tool"]
            attempt = json.loads(tool_records[-1]["content"])["send_attempt_id"]
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-commit",
                        name="commit_send_attempt",
                        arguments=json.dumps(
                            {
                                "send_attempt_id": attempt,
                                "reviewed_until_seq": 2,
                                "delivery_interrupt_policy": "interrupt_priority",
                                "ignore_review_interrupts": True,
                            }
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="先问", message_id="m-old"))
    await first_started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="我改口", message_id="m-new"))
    first_release.set()
    await commit_started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="再补一句", message_id="m-third"))
    commit_release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["旧回复"]
    records = await history.records()
    result = next(
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-commit"
    )
    assert result["status"] == "accepted"
    assert result["delivery"] == "pending"
    assert result["ignored_review_interrupts"] is True
    assert result["forced_unseen_messages"][0]["text"] == "再补一句"
    assert "不要重复提交同一批" in result["next"]


@pytest.mark.asyncio
async def test_send_ignore_review_interrupts_does_not_bypass_preflight_review(
    build_pipeline,
):
    """send_* 的 ignore_review_interrupts=true 不能绕过首次发送前 preflight。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1, "delay": 0}],
        "ignore_review_interrupts": True,
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": copy.deepcopy(messages), "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="我改口", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert adapter.sent == []
    records = await history.records()
    results = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    assert results[-1]["status"] == "needs_review"
    assert results[-1]["unseen_messages"][0]["text"] == "我改口"


@pytest.mark.asyncio
async def test_send_ignore_review_interrupts_prevents_post_send_soft_interrupt(
    build_pipeline,
):
    """发送被 accepted 后，ignore_review_interrupts=true 会忽略后续普通入站软打断。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "第一条", "order": 1, "delay": 0.2},
            {"target_qq": 123, "content": "第二条", "order": 2, "delay": 0.2},
        ],
        "ignore_review_interrupts": True,
    }
    pipeline, _, adapter, history, _ = await build_pipeline(
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
            _ai_no_action(),
            _ai_no_action(),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="开始", message_id="m-old"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="123", text="发送期间的新消息", message_id="m-new"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["第一条", "第二条"]
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "发送期间的新消息" in joined
    assert '"interrupted": true' not in joined
    assert '"status": "needs_review"' not in joined
    assert '"status": "needs_review_again"' not in joined
    assert "<send_receipt>" not in joined


@pytest.mark.asyncio
async def test_send_ignore_review_interrupts_does_not_hide_interrupt_from_queued_send(
    build_pipeline,
):
    """第一批发送后 ignore 只保护当前 job，不能吞掉后续默认发送的中断。"""
    first_args = {
        "targets": [
            {"target_qq": 123, "content": "第一批1", "order": 1, "delay": 0.2},
            {"target_qq": 123, "content": "第一批2", "order": 2, "delay": 0.2},
        ],
        "ignore_review_interrupts": True,
    }
    second_args = {
        "targets": [{"target_qq": 123, "content": "第二批", "order": 1, "delay": 0}],
    }
    pipeline, _, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-first",
                        name="send_private_messages",
                        arguments=json.dumps(first_args),
                    ),
                    ToolCall(
                        id="tc-second",
                        name="send_private_messages",
                        arguments=json.dumps(second_args),
                    ),
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action(),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="开始", message_id="m-old"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="123", text="发送期间的新消息", message_id="m-new"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["第一批1", "第一批2"]
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "发送期间的新消息" in joined
    assert "<send_receipt>" in joined
    assert "interrupted=true" in joined
    assert "第二批；order=1" in joined
    assert "send_id=send-" in joined


@pytest.mark.asyncio
async def test_send_ignore_review_interrupts_does_not_ignore_recall_during_send(
    build_pipeline,
):
    """撤回是硬边界，send_* 的 ignore_review_interrupts=true 不能忽略。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "第一条", "order": 1, "delay": 0.2},
            {"target_qq": 123, "content": "第二条", "order": 2, "delay": 0.2},
        ],
        "ignore_review_interrupts": True,
    }
    pipeline, _, adapter, history, _ = await build_pipeline(
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
            _ai_no_action(),
        ]
    )
    recall = RecallHandler(
        pipeline=pipeline,
        behavior_cfg=pipeline.behavior_cfg,
    )

    await pipeline.enqueue(_msg(user_id="123", text="马上撤", message_id="m-old"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await recall.on_notice(
        IncomingNotice(
            adapter="fake",
            timestamp=1.1,
            self_id="999",
            notice_type=NoticeType.FRIEND_RECALL,
            user_id="123",
            message_id="m-old",
        )
    )
    await _drain_pipeline(pipeline, max_wait=3.0)
    await recall.shutdown()

    assert [content for _, content in adapter.sent] == ["第一条"]
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "<send_receipt>" in joined
    assert "interrupted=true" in joined
    assert "撤回消息 1 条" in joined
    assert "m-old" in joined
    assert "第二条；order=2" in joined


@pytest.mark.asyncio
async def test_commit_send_attempt_sends_once_and_second_commit_is_blocked(build_pipeline):
    """send_attempt 可确认一次，二次 commit 只返回 already_committed 不重复发送。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1, "delay": 0}],
    }
    no_action = ToolCall(id="tc-na", name="no_action", arguments="{}")
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": copy.deepcopy(messages), "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        if call_count == 2:
            tool_records = [m for m in messages if m.get("role") == "tool"]
            attempt = json.loads(tool_records[-1]["content"])["send_attempt_id"]
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-commit",
                        name="commit_send_attempt",
                        arguments=json.dumps(
                            {
                                "send_attempt_id": attempt,
                                "reviewed_until_seq": 2,
                                "delivery_interrupt_policy": "interrupt_all",
                            }
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        if call_count == 3:
            tool_records = [m for m in messages if m.get("role") == "tool"]
            attempt = next(
                json.loads(m["content"])["send_attempt_id"]
                for m in tool_records
                if json.loads(m["content"]).get("status") == "needs_review"
            )
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-commit-again",
                        name="commit_send_attempt",
                        arguments=json.dumps(
                            {
                                "send_attempt_id": attempt,
                                "reviewed_until_seq": 2,
                                "delivery_interrupt_policy": "interrupt_all",
                            }
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        return CompletionResult(tool_calls=[no_action], finish_reason="tool_calls")

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="先问", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="我改口", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["旧回复"]
    records = await history.records()
    results = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool"
        and r.get("tool_call_id") in {"tc-commit", "tc-commit-again"}
    ]
    assert results[0]["status"] == "sent"
    assert results[0]["send_attempt_id"].startswith("attempt-")
    assert results[1]["status"] == "already_committed"


@pytest.mark.asyncio
async def test_commit_send_attempt_rejects_recalled_trigger_message(build_pipeline):
    """触发消息被撤回后，即使旧 attempt 仍存在也不能 commit。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": copy.deepcopy(messages), "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="马上撤", message_id="m-old"))
    await started.wait()
    await pipeline.enqueue(_msg(user_id="123", text="补一句", message_id="m-new"))
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    records = await history.records()
    attempt = next(
        json.loads(r["content"])["send_attempt_id"]
        for r in records
        if r.get("role") == "tool"
        and r.get("tool_call_id") == "tc-send"
        and json.loads(r["content"]).get("status") == "needs_review"
    )
    recall = RecallHandler(
        pipeline=pipeline,
        behavior_cfg=pipeline.behavior_cfg,
    )
    await recall.on_notice(
        IncomingNotice(
            adapter="fake",
            timestamp=1.1,
            self_id="999",
            notice_type=NoticeType.FRIEND_RECALL,
            user_id="123",
            message_id="m-old",
        )
    )
    await recall.shutdown()

    ctx = pipeline._build_tool_context(conversation_id="private:123")
    executor = pipeline.tool_registry.get_executor(ctx)
    result = await executor(
        "commit_send_attempt",
        {
            "send_attempt_id": attempt,
            "reviewed_until_seq": 2,
            "delivery_interrupt_policy": "interrupt_priority",
            "ignore_review_interrupts": True,
        },
        tool_call_id="tc-direct-commit",
    )

    assert result["status"] == "cannot_commit_recalled_trigger"
    assert result["recalled_messages"][0]["msg_id"] == "m-old"
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_same_content_from_different_tool_calls_is_allowed(build_pipeline):
    """重复内容不由程序拦截，不同 tool call 明确再次发送时允许。"""
    args = {
        "targets": [{"target_qq": 123, "content": "嗯", "order": 1, "delay": 0}],
    }
    pipeline, _, adapter, _, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(id="tc-send-1", name="send_private_messages", arguments=json.dumps(args)),
                    ToolCall(id="tc-send-2", name="send_private_messages", arguments=json.dumps(args)),
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action(),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="发两次"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    assert [content for _, content in adapter.sent] == ["嗯", "嗯"]


@pytest.mark.asyncio
async def test_same_tool_call_id_replay_does_not_send_twice(build_pipeline):
    """同一个 tool_call_id 被重放时返回缓存结果，不重复真实发送。"""
    pipeline, _, adapter, _, _ = await build_pipeline([])
    ctx = pipeline._build_tool_context(conversation_id="private:123")
    executor = pipeline.tool_registry.get_executor(ctx)
    args = {
        "targets": [{"target_qq": 123, "content": "嗯", "order": 1, "delay": 0}],
    }

    first = await executor("send_private_messages", args, tool_call_id="tc-replay")
    second = await executor("send_private_messages", args, tool_call_id="tc-replay")

    assert first == second
    assert [content for _, content in adapter.sent] == ["嗯"]


@pytest.mark.asyncio
async def test_late_inbound_after_final_async_send_restarts_deferred_batch(build_pipeline):
    """最后一条异步发送期间来的新消息不能留下 sticky stale。"""
    second_send_entered = asyncio.Event()
    release_second_send = asyncio.Event()
    first_args = {
        "targets": [
            {"target_qq": 123, "content": "第一条", "order": 1, "delay": 0.01},
            {"target_qq": 123, "content": "第二条", "order": 2, "delay": 0.01},
        ],
    }
    second_args = {
        "targets": [{"target_qq": 123, "content": "新回复", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-first",
                        name="send_private_messages",
                        arguments=json.dumps(first_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action(),
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-second",
                        name="send_private_messages",
                        arguments=json.dumps(second_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    original_send_text = adapter.send_text

    async def blocking_second_send(target: Target, content: str) -> str:
        msg_id = await original_send_text(target, content)
        if content == "第二条":
            second_send_entered.set()
            await release_second_send.wait()
        return msg_id

    adapter.send_text = blocking_second_send  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="开始", message_id="m-start"))
    await asyncio.wait_for(second_send_entered.wait(), timeout=1.0)
    await pipeline.enqueue(_msg(user_id="123", text="补一句", message_id="m-late"))
    release_second_send.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["第一条", "第二条", "新回复"]
    assert len(provider.calls) == 4
    assert not pipeline._send_manager.should_defer_batch("private:123")
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "补一句" in joined


@pytest.mark.asyncio
async def test_recalled_pending_message_is_not_processed_as_new_task(build_pipeline):
    """合并窗口内被撤回的消息只记录状态，不再触发主模型接旧话。"""
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [_ai_send_private(target_qq="123", content="不该发")]
    )
    recall = RecallHandler(
        pipeline=pipeline,
        behavior_cfg=pipeline.behavior_cfg,
    )

    await pipeline.enqueue(_msg(user_id="123", text="发错了的内容", message_id="m-recall"))
    await recall.on_notice(
        IncomingNotice(
            adapter="fake",
            timestamp=1.1,
            self_id="999",
            notice_type=NoticeType.FRIEND_RECALL,
            user_id="123",
            message_id="m-recall",
        )
    )
    await _drain_pipeline(pipeline, max_wait=1.0)
    await recall.shutdown()

    assert provider.calls == []
    assert adapter.sent == []
    records = await history.records()
    assert any(
        record.get("role") == "system"
        and record.get("conversation_id") == "private:123"
        and "m-recall" in str(record.get("content") or "")
        for record in records
    )


@pytest.mark.asyncio
async def test_recall_while_model_thinking_marks_send_stale(build_pipeline):
    """模型思考中的触发消息被撤回时，旧回复需要复核，不能继续发出。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1, "delay": 0}],
        "ignore_review_interrupts": True,
    }
    pipeline, provider, adapter, history, _ = await build_pipeline([])
    call_count = 0

    async def blocking_chat_completion(messages, *, model, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        provider.calls.append({"messages": messages, "model": model, "tools": tools})
        if call_count == 1:
            started.set()
            await release.wait()
            return CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_private_messages",
                        arguments=json.dumps(send_args),
                    )
                ],
                finish_reason="tool_calls",
            )
        return CompletionResult(
            tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],
            finish_reason="tool_calls",
        )

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]
    recall = RecallHandler(
        pipeline=pipeline,
        behavior_cfg=pipeline.behavior_cfg,
    )

    await pipeline.enqueue(_msg(user_id="123", text="马上撤回", message_id="m-old"))
    await started.wait()
    await recall.on_notice(
        IncomingNotice(
            adapter="fake",
            timestamp=1.1,
            self_id="999",
            notice_type=NoticeType.FRIEND_RECALL,
            user_id="123",
            message_id="m-old",
        )
    )
    release.set()
    await _drain_pipeline(pipeline, max_wait=3.0)
    await recall.shutdown()

    assert adapter.sent == []
    records = await history.records()
    review_results = [
        json.loads(record["content"])
        for record in records
        if record.get("role") == "tool" and record.get("tool_call_id") == "tc-send"
    ]
    assert review_results
    assert review_results[-1]["status"] == "needs_review"
    assert review_results[-1]["recalled_messages"][0]["msg_id"] == "m-old"


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


@pytest.mark.asyncio
async def test_history_records_alias_returns_records(tmp_path):
    """B13 单点回归：HistoryManager.records() 别名必须返回 list[dict]。"""
    hm = HistoryManager(tmp_path / "h.jsonl")
    await hm.load()
    await hm.add_user_message("hi")
    await hm.add_assistant_message("hello")

    records = await hm.records()
    assert isinstance(records, list)
    roles = [r.get("role") for r in records]
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.asyncio
async def test_inbound_keyword_text_does_not_auto_save_important_memory(build_pipeline):
    """普通入站关键词不再自动写入 important.json。"""
    pipeline, _, _, _, important = await build_pipeline([_ai_no_action()])

    await pipeline.enqueue(_msg(text="记住我喜欢吃寿司"))
    await _drain_pipeline(pipeline)

    items = important.items()
    assert not any("寿司" in (i.get("content") or "") for i in items)


@pytest.mark.asyncio
async def test_token_compaction_archives_without_truncating_full_history(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.existing_important_text = ""

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.existing_important_text = existing_important_text
            return {
                "summary_text": f"{existing_summary_text}\n已归档 {len(history_slice)} 条".strip(),
                "new_important": [
                    {
                        "content": "归档中提到用户喜欢测试",
                        "scope": "user:123",
                        "pinned": True,
                    },
                    {"content": "归档中提到用户喜欢测试"},
                    {"content": "归档中提到旧格式也要保存"},
                    {
                        "content": "归档中提到非 bool pinned 不应置顶",
                        "scope": "group:5555",
                        "pinned": "true",
                    },
                    {"content": ""},
                    "invalid",
                ],
            }

    pipeline, _, _, history, important = await build_pipeline([])
    pipeline.summary_agent = FakeSummaryAgent()
    pipeline.behavior_cfg.summarize.trigger_at_tokens = 50
    pipeline.behavior_cfg.summarize.target_after_tokens = 20

    for idx in range(6):
        await history.add_user_message(
            f"旧消息 {idx} " + ("很长的测试内容 " * 20),
            conversation_id="private:123",
        )

    before = await history.length()
    result = await pipeline._maybe_summarize()

    archived = await pipeline.archive.records()
    after = await history.length()
    assert archived, "压缩前应先把原文写入 archive"
    assert result.success
    assert after == before + 1
    assert pipeline.rolling_summary.active_start_index() == len(archived)
    full_joined = "\n".join(str(r.get("content", "")) for r in await history.records())
    active_joined = "\n".join(
        str(r.get("content", ""))
        for r in await pipeline._select_working_history("private:123")
    )
    assert "旧消息 0" in full_joined
    assert "旧消息 0" not in active_joined
    assert "已归档" in pipeline.rolling_summary.text()
    items = important.items()
    scoped_item = next(
        item for item in items if item.get("content") == "归档中提到用户喜欢测试"
    )
    assert scoped_item.get("scope") == "user:123"
    assert scoped_item.get("pinned") is True
    assert sum(
        1 for item in items if item.get("content") == "归档中提到用户喜欢测试"
    ) == 1
    assert any(item.get("content") == "归档中提到旧格式也要保存" for item in items)
    non_bool_pinned_item = next(
        item for item in items if item.get("content") == "归档中提到非 bool pinned 不应置顶"
    )
    assert non_bool_pinned_item.get("scope") == "group:5555"
    assert non_bool_pinned_item.get("pinned") is False


@pytest.mark.asyncio
async def test_compaction_does_not_trigger_at_active_message_count(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append(list(history_slice))
            return {"summary_text": "不应因记录数量摘要", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 1

    for idx in range(3):
        await history.add_user_message(
            f"短消息不触发摘要 {idx}",
            conversation_id="private:123",
        )

    result = await pipeline._maybe_summarize()

    assert result.status == "not_needed"
    assert result.reason == "below_trigger"
    assert agent.calls == []
    assert pipeline.rolling_summary.active_start_index() == 0
    assert await history.length() == 3


@pytest.mark.asyncio
async def test_compaction_slice_keeps_assistant_tool_result_group_together(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    estimator = pipeline._token_estimator()
    records = [
        {"role": "user", "content": "很早的普通消息 " + ("内容 " * 40)},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc-boundary",
                    "type": "function",
                    "function": {"name": "no_action", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc-boundary", "content": '{"no_action": true}'},
        {"role": "user", "content": "必须留在活跃窗口里的新消息"},
    ]
    active_tokens = estimator.estimate_messages(records)
    target_after = active_tokens - estimator.estimate_messages(records[:2])

    selected = pipeline._select_compaction_slice(
        records,
        active_tokens=active_tokens,
        target_after_tokens=target_after,
        estimator=estimator,
    )

    assert selected == records[:3]
    assert selected[-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_active_start_index_moves_back_from_orphan_tool_result(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_user_message("旧消息", conversation_id="private:123")
    await history.add_records(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-orphan",
                        "type": "function",
                        "function": {"name": "no_action", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc-orphan", "content": '{"no_action": true}'},
            {"role": "user", "content": "后续消息"},
        ],
        conversation_id="private:123",
    )
    await pipeline.rolling_summary.update("旧摘要", active_start_index=2)

    active = await pipeline._select_working_history("private:123")

    assert active[0]["role"] == "assistant"
    assert active[0]["tool_calls"][0]["id"] == "tc-orphan"
    assert active[1]["role"] == "tool"
    assert active[1]["tool_call_id"] == "tc-orphan"


@pytest.mark.asyncio
async def test_concurrent_compaction_is_serialized_without_duplicate_archive(
    build_pipeline,
):
    class BlockingSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.first_started = asyncio.Event()
            self.first_release = asyncio.Event()
            self.second_started = asyncio.Event()

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append([str(record.get("content") or "") for record in history_slice])
            if len(self.calls) == 1:
                self.first_started.set()
                await self.first_release.wait()
            else:
                self.second_started.set()
            return {
                "summary_text": f"{existing_summary_text}\n并发摘要 {len(self.calls)}".strip(),
                "new_important": [],
            }

    pipeline, _, _, history, _ = await build_pipeline([])
    agent = BlockingSummaryAgent()
    pipeline.summary_agent = agent
    for idx in range(8):
        await history.add_user_message(
            f"并发压缩消息 {idx} " + ("内容 " * 100),
            conversation_id="private:123",
        )
    active = await pipeline._select_working_history("private:123")
    estimator = pipeline._token_estimator()
    target_after = max(
        1,
        estimator.estimate_messages(active)
        - estimator.estimate_messages(active[:2]),
    )

    first_task = asyncio.create_task(
        pipeline._maybe_summarize(
            force=True,
            target_after_tokens=target_after,
            reason="concurrent_first",
        )
    )
    await asyncio.wait_for(agent.first_started.wait(), timeout=1.0)
    second_task = asyncio.create_task(
        pipeline._maybe_summarize(
            force=True,
            target_after_tokens=target_after,
            reason="concurrent_second",
        )
    )
    await asyncio.sleep(0.05)

    assert len(agent.calls) == 1
    assert not agent.second_started.is_set()

    agent.first_release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result.success
    assert second_result.success
    assert second_result.active_start_before >= first_result.active_start_after
    assert len(agent.calls) == 2
    assert set(agent.calls[0]).isdisjoint(agent.calls[1])
    archived = await pipeline.archive.records()
    archived_contents = [
        str(record.get("content") or "")
        for record in archived
        if "并发压缩消息" in str(record.get("content") or "")
    ]
    assert len(archived_contents) == len(set(archived_contents))
    assert pipeline.rolling_summary.active_start_index() == second_result.active_start_after


@pytest.mark.asyncio
async def test_compaction_partial_archive_retry_does_not_append_duplicate(
    build_pipeline,
    monkeypatch,
):
    class FakeSummaryAgent:
        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            return {"summary_text": f"partial retry {len(history_slice)}", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.summary_agent = FakeSummaryAgent()
    for idx in range(5):
        await history.add_user_message(
            f"partial archive 旧消息 {idx} " + ("内容 " * 120),
            conversation_id="private:123",
        )

    original_update = pipeline.rolling_summary.update

    async def fail_update(*args, **kwargs):
        raise RuntimeError("rolling summary write failed")

    monkeypatch.setattr(pipeline.rolling_summary, "update", fail_update)
    first = await pipeline._maybe_summarize(
        force=True,
        target_after_tokens=5,
        reason="partial_archive_first",
    )

    assert not first.success
    assert first.reason == "commit_error"
    assert first.partial_archive_committed is True
    assert pipeline.rolling_summary.active_start_index() == 0
    archived_after_failure = await pipeline.archive.records()
    assert archived_after_failure

    pipeline._summary_partial_archives = {}
    pipeline.archive = ArchiveStore(pipeline.archive.path)
    await pipeline.archive.load()
    monkeypatch.setattr(pipeline.rolling_summary, "update", original_update)
    second = await pipeline._maybe_summarize(
        force=True,
        target_after_tokens=5,
        reason="partial_archive_retry",
    )
    archived_after_retry = await pipeline.archive.records()

    assert second.success
    assert second.archive_reused is False
    assert len(archived_after_retry) == len(archived_after_failure)
    assert pipeline.rolling_summary.active_start_index() == second.active_start_after


@pytest.mark.asyncio
async def test_main_reply_budget_overflow_compacts_before_calling_model(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append(list(history_slice))
            return {"summary_text": "预算预检摘要完成", "new_important": []}

    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 31_000
    pipeline.behavior_cfg.context.reserve_output_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 3_000
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=24_000,
        prefix="预算压缩旧消息",
    )

    await pipeline.enqueue(_msg(text="触发预算压缩", message_id="budget-compact"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert agent.calls
    assert provider.calls
    assert pipeline.rolling_summary.active_start_index() > 0
    joined = "\n".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])
    assert "预算预检摘要完成" in joined
    assert "触发预算压缩" in joined
    assert "预算压缩旧消息 0" not in joined


@pytest.mark.asyncio
async def test_main_reply_budget_retry_expands_compaction_range(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls.append(list(history_slice))
            return {
                "summary_text": f"预算重试摘要完成 {len(self.calls)}",
                "new_important": [],
            }

    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 31_000
    pipeline.behavior_cfg.context.reserve_output_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 15_000
    summarize.retry_target_after_context_percent = 20
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=26_000,
        prefix="预算重试旧消息",
    )

    await pipeline.enqueue(_msg(text="触发预算重试", message_id="budget-retry"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert len(agent.calls) == 2
    assert provider.calls
    assert pipeline.rolling_summary.active_start_index() > len(agent.calls[0])
    joined = "\n".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])
    assert "预算重试摘要完成 2" in joined
    assert "触发预算重试" in joined


@pytest.mark.asyncio
async def test_budget_retry_target_never_exceeds_first_target(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.behavior_cfg.context.max_context_tokens = 100_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.target_after_tokens = 2_000
    summarize.retry_target_after_context_percent = 50
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=8_000,
        prefix="重试目标保护旧消息",
    )

    estimator = pipeline._token_estimator()
    first_target = await pipeline._first_budget_retry_target(estimator)
    retry_target = await pipeline._retry_budget_target(
        estimator,
        first_target=first_target,
    )

    assert first_target == 2_000
    assert retry_target <= first_target


@pytest.mark.asyncio
async def test_run_one_turn_budget_failure_skips_model_and_writes_system_note(
    build_pipeline,
):
    class FailingSummaryAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.calls += 1
            return None

    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = FailingSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 31_000
    pipeline.behavior_cfg.context.reserve_output_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = 999_999
    summarize.target_after_tokens = 3_000
    await _add_history_until_active_tokens(
        pipeline,
        history,
        min_tokens=24_000,
        prefix="预算失败旧消息",
    )

    await pipeline.run_one_turn(
        "预算失败测试",
        user_event="这轮不应调用主模型",
        conversation_id="private:123",
    )

    assert agent.calls == 2
    assert provider.calls == []
    records = await history.records()
    assert any(
        record.get("role") == "system"
        and "主模型输入预检失败" in str(record.get("content") or "")
        for record in records
    )


@pytest.mark.asyncio
async def test_rag_mode_compaction_still_reads_and_writes_important_memory(build_pipeline):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.existing_important_text = ""

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.existing_important_text = existing_important_text
            return {
                "summary_text": f"RAG 模式归档 {len(history_slice)} 条",
                "new_important": [{"content": "RAG 模式归档中提到用户喜欢测试"}],
            }

    pipeline, _, _, history, important = await build_pipeline([])
    pipeline.features_cfg.long_term_memory.mode = "rag"
    await important.save("已有重要记忆仍应参与摘要")
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.summarize.trigger_at_tokens = 50
    pipeline.behavior_cfg.summarize.target_after_tokens = 20

    for idx in range(6):
        await history.add_user_message(
            f"RAG 模式旧消息 {idx} " + ("很长的测试内容 " * 20),
            conversation_id="private:123",
        )

    await pipeline._maybe_summarize()

    assert "已有重要记忆仍应参与摘要" in agent.existing_important_text
    assert any(
        "RAG 模式归档中提到用户喜欢测试" in item.get("content", "")
        for item in important.items()
    )


@pytest.mark.asyncio
async def test_compaction_uses_percent_thresholds_when_token_fields_are_unset(
    build_pipeline,
):
    class FakeSummaryAgent:
        def __init__(self) -> None:
            self.history_slice: list[dict[str, Any]] = []

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.history_slice = list(history_slice)
            return {"summary_text": "百分比摘要完成", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([])
    agent = FakeSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.context.max_context_tokens = 1_000
    summarize = pipeline.behavior_cfg.summarize
    summarize.trigger_at_tokens = None
    summarize.target_after_tokens = None
    summarize.trigger_at_context_percent = 50
    summarize.target_after_context_percent = 80

    estimator = pipeline._token_estimator()
    while estimator.estimate_messages(await history.records()) < 950:
        idx = await history.length()
        await history.add_user_message(
            f"百分比旧消息 {idx} " + ("很长的测试内容 " * 20),
            conversation_id="private:123",
        )

    records = await history.records()
    active_tokens = estimator.estimate_messages(records)
    expected_target = int(
        pipeline._context_budget().max_context_tokens
        * summarize.target_after_context_percent
        / 100
    )
    expected_slice = pipeline._select_compaction_slice(
        records,
        active_tokens=active_tokens,
        target_after_tokens=max(1, min(expected_target, active_tokens - 1)),
        estimator=estimator,
    )

    await pipeline._maybe_summarize()

    assert agent.history_slice
    assert len(agent.history_slice) == len(expected_slice)
    assert "百分比摘要完成" in pipeline.rolling_summary.text()


@pytest.mark.asyncio
async def test_triggered_compaction_runs_before_model_call(build_pipeline):
    class BlockingSummaryAgent:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.provider = None
            self.cfg = type("Cfg", (), {"model": "fake-summary"})()

        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            self.started.set()
            await self.release.wait()
            return {"summary_text": "后台摘要完成", "new_important": []}

    pipeline, _, _, history, _ = await build_pipeline([_ai_no_action()])
    agent = BlockingSummaryAgent()
    pipeline.summary_agent = agent
    pipeline.behavior_cfg.summarize.trigger_at_tokens = 10
    pipeline.behavior_cfg.summarize.target_after_tokens = 5

    for idx in range(3):
        await history.add_user_message(
            f"旧消息 {idx} " + ("很长的测试内容 " * 10),
            conversation_id="private:123",
        )

    await pipeline.enqueue(_msg(text="触发后台压缩"))
    await asyncio.wait_for(agent.started.wait(), timeout=1.0)

    assert pipeline._batch_task is not None and not pipeline._batch_task.done()

    agent.release.set()
    await _drain_pipeline(pipeline, max_wait=2.0)
    assert "后台摘要完成" in pipeline.rolling_summary.text()


@pytest.mark.asyncio
async def test_recall_history_reads_archive(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await pipeline.archive.append_many(
        [
            {
                "role": "user",
                "content": "很久以前约定：周日做游戏 Demo",
                "conversation_id": "group:42",
                "metadata": {"timestamp": "2026-05-30 01:00"},
            }
        ]
    )
    await history.add_user_message(
        "当前活跃区约定：周一补玩法文档",
        metadata={"timestamp": "2026-05-30 02:00"},
        conversation_id="group:42",
    )
    registry = build_default_registry(_make_root_config())
    ctx = ToolContext(archive=pipeline.archive, history=history)
    _approve_stub_tools(ctx, "recall_history")
    executor = registry.get_executor(ctx)

    result = await executor(
        "recall_history",
        {"conversation_id": "group:42", "time_range": "2026-05-30", "limit": 5},
    )

    assert result["ok"] is True
    assert result["status"] == "inline"
    assert result["count"] == 2
    assert "周日做游戏 Demo" in result["content"]
    assert "周一补玩法文档" in result["content"]
    assert "metadata" not in result["results"][0]


@pytest.mark.asyncio
async def test_pipeline_injects_scope_filtered_important_memory(build_pipeline):
    pipeline, provider, _, _, important = await build_pipeline([_ai_no_action()])
    await important.save("全局偏好", scope="global")
    await important.save("群 42 约定", scope="group:42")
    await important.save("群 99 约定", scope="group:99")
    await important.save("置顶跨会话事实", scope="user:123", pinned=True)

    await pipeline.enqueue(_msg(text="触发上下文", group_id="42"))
    await _drain_pipeline(pipeline)

    joined = "\n".join(
        str(message.get("content", "")) for message in provider.calls[0]["messages"]
    )
    assert "全局偏好" in joined
    assert "群 42 约定" in joined
    assert "置顶跨会话事实" in joined
    assert "群 99 约定" not in joined


@pytest.mark.asyncio
async def test_rag_mode_still_injects_scope_filtered_important_memory(build_pipeline):
    pipeline, provider, _, _, important = await build_pipeline([_ai_no_action()])
    pipeline.features_cfg.long_term_memory.mode = "rag"
    await important.save("RAG 模式全局偏好", scope="global")
    await important.save("RAG 模式群 42 约定", scope="group:42")
    await important.save("RAG 模式群 99 约定", scope="group:99")

    await pipeline.enqueue(_msg(text="触发 RAG 模式上下文", group_id="42"))
    await _drain_pipeline(pipeline)

    joined = "\n".join(
        str(message.get("content", "")) for message in provider.calls[0]["messages"]
    )
    assert "RAG 模式全局偏好" in joined
    assert "RAG 模式群 42 约定" in joined
    assert "RAG 模式群 99 约定" not in joined
    assert "<long_term_memory" in joined


@pytest.mark.asyncio
async def test_rag_mode_inbound_keyword_text_does_not_auto_save_important_memory(build_pipeline):
    pipeline, _, _, _, important = await build_pipeline([_ai_no_action()])
    pipeline.features_cfg.long_term_memory.mode = "rag"

    await pipeline.enqueue(_msg(text="记一下：我报名了某项长期活动，7月7日有选拔环节"))
    await _drain_pipeline(pipeline)

    assert not any("长期活动" in item.get("content", "") for item in important.items())


@pytest.mark.asyncio
async def test_wakeup_mode_no_action_does_not_send_fallback(build_pipeline):
    pipeline, _, adapter, _, _ = await build_pipeline([_ai_no_action()])

    await pipeline.run_wakeup_turn(
        "内部继续任务：检查后台状态",
        {"target_type": "private", "target_id": 123},
    )

    assert adapter.sent == []


@pytest.mark.asyncio
async def test_send_message_mode_sends_without_model_or_interrupt(
    build_pipeline,
):
    pipeline, provider, adapter, _, _ = await build_pipeline([])
    await pipeline.batch.append(
        PendingMessageItem(
            message_id="pending-1",
            user_id="u1",
            nickname="用户",
            location="私聊",
            text="打断用的新消息",
            raw_event=_msg(text="打断用的新消息"),
        )
    )

    await pipeline.run_wakeup_turn(
        "[定时发送消息]\n消息内容：到点了\n发送目标：private:123",
        {"target_type": "private", "target_id": 123},
        mode="send_message",
        message_text="到点了",
    )

    assert provider.calls == []
    assert adapter.sent[-1][1] == "到点了"


@pytest.mark.asyncio
async def test_send_message_mode_raises_when_qq_sent_append_log_fails(
    build_pipeline,
):
    event_store = FailingQQSentEventStore()
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [],
        event_store=event_store,
    )

    with pytest.raises(
        RuntimeError,
        match="QQ 消息已发送，但 qq_message_sent 事件持久化失败.*append log failed",
    ):
        await pipeline.run_wakeup_turn(
            "[定时发送消息]\n消息内容：审计失败\n发送目标：private:123",
            {"target_type": "private", "target_id": 123},
            mode="send_message",
            message_text="审计失败",
        )

    assert provider.calls == []
    assert adapter.sent[-1][0].scope == "private"
    assert adapter.sent[-1][0].target_id == "123"
    assert adapter.sent[-1][1] == "审计失败"
    assert [event["event_type"] for event in event_store.appended_events] == [
        "qq_message_sent"
    ]
    records = await history.records()
    assert not any(
        "msg_id=1000" in str(record.get("content") or "") for record in records
    )


@pytest.mark.asyncio
async def test_send_message_mode_ignores_sqlite_projection_failure(
    build_pipeline,
    tmp_path,
):
    event_store = ProjectionFailingEventStore(
        tmp_path / "projection-fails.sqlite3",
        projection_retry_delay=0.001,
    )
    pipeline, provider, adapter, _, _ = await build_pipeline(
        [],
        event_store=event_store,
    )

    try:
        await pipeline.run_wakeup_turn(
            "[定时发送消息]\n消息内容：投影失败仍发送\n发送目标：private:123",
            {"target_type": "private", "target_id": 123},
            mode="send_message",
            message_text="投影失败仍发送",
        )

        stats = await event_store.stats()
        assert provider.calls == []
        assert adapter.sent[-1][1] == "投影失败仍发送"
        assert stats["last_appended_event_id"] == 1
        assert stats["last_projected_event_id"] == 0
    finally:
        await event_store.shutdown(timeout=0.01)


@pytest.mark.asyncio
async def test_wakeup_action_uses_normal_window_with_reminder_as_current_task(build_pipeline):
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [_ai_send_private(target_qq="123", content="到点了")]
    )
    await history.add_user_message("旧消息：请执行已完成的无关任务")

    await pipeline.run_wakeup_turn(
        "内部继续任务：检查后台状态",
        {"target_type": "private", "target_id": 123},
    )

    messages = provider.calls[0]["messages"]
    joined = "\n".join(str(m.get("content", "")) for m in messages)
    assert "旧消息：请执行已完成的无关任务" in joined
    assert "检查后台状态" in joined
    assert "只处理这一条提醒" in joined
    assert messages[-1]["role"] == "user"
    assert adapter.sent[-1][1] == "到点了"


@pytest.mark.asyncio
async def test_execute_collected_sends_voice_action(build_pipeline, tmp_path):
    pipeline, _, adapter, _, _ = await build_pipeline([])
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF")

    await pipeline._execute_collected(
        [
            {
                "kind": "voice",
                "action": "private",
                "target": "123",
                "label": "[语音] 测试",
                "delay": 0.0,
                "audio_path": str(audio),
            }
        ],
    )

    assert adapter.voice_sent
    target, sent_path = adapter.voice_sent[-1]
    assert target.scope == "private"
    assert target.target_id == "123"
    assert sent_path == audio


@pytest.mark.asyncio
async def test_plain_text_after_send_is_rejected_until_no_action(build_pipeline):
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            _ai_send_private(target_qq="123", content="先说一句"),
            CompletionResult(content="这句纯文本不应该触发纠正", finish_reason="stop"),
            _ai_no_action(),
        ]
    )

    await pipeline.enqueue(_msg(text="测试发送后纯文本"))
    await _drain_pipeline(pipeline)

    assert [content for _, content in adapter.sent] == ["先说一句"]
    records = await history.records()
    assert any(
        "错误：未调用工具" in (r.get("content") or "") for r in records
    )
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_voice_message_prefers_injected_asr(build_pipeline, tmp_path, monkeypatch):
    """语音消息应优先调用 Runtime 注入的 ASR，而不是适配器自带转写。"""

    class FakeASR:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def transcribe(self, audio_path):
            self.calls.append(audio_path)
            return "ASR 识别文本"

    pipeline, _, adapter, _, _ = await build_pipeline([])
    asr = FakeASR()
    pipeline.asr = asr
    pipeline.workspace_dir = tmp_path

    async def fake_save_media(url: str, suggested_name: str) -> str:
        incoming = tmp_path / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        (incoming / suggested_name).write_bytes(b"voice")
        return f"incoming/{suggested_name}"

    monkeypatch.setattr(pipeline, "_save_media_to_workspace", fake_save_media)
    event = _msg(text="[语音]", message_id="voice-1")
    event.media.append(
        MediaSegment(type=MediaType.VOICE, url="https://example.com/voice.amr")
    )

    text = await pipeline._build_readable_text(event)

    assert "ASR 识别文本" in text
    assert "workspace=incoming/voice_voice-1.amr" in text
    assert asr.calls == [tmp_path / "incoming" / "voice_voice-1.amr"]
    assert adapter.voice_fetch_calls == []


@pytest.mark.asyncio
async def test_voice_message_falls_back_to_adapter_when_asr_fails(
    build_pipeline, tmp_path, monkeypatch
):
    """ASR 失败时应记录失败并回退到 adapter.fetch_voice_text。"""

    class BrokenASR:
        async def transcribe(self, audio_path):
            raise RuntimeError("boom")

    pipeline, _, adapter, _, _ = await build_pipeline([])
    pipeline.asr = BrokenASR()
    pipeline.workspace_dir = tmp_path
    adapter.voice_text = "适配器转写文本"

    async def fake_save_media(url: str, suggested_name: str) -> str:
        incoming = tmp_path / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        (incoming / suggested_name).write_bytes(b"voice")
        return f"incoming/{suggested_name}"

    monkeypatch.setattr(pipeline, "_save_media_to_workspace", fake_save_media)
    event = _msg(text="[语音]", message_id="voice-2")
    event.media.append(
        MediaSegment(type=MediaType.VOICE, url="https://example.com/voice.amr")
    )

    text = await pipeline._build_readable_text(event)

    assert "适配器转写文本" in text
    assert adapter.voice_fetch_calls == ["voice-2"]


@pytest.mark.asyncio
async def test_multi_turn_tool_loop(build_pipeline):
    """非 no_action 工具默认把结果回填给模型，不再因 no_feedback 隐式结束。"""
    # 工具参数字段名是 memory_text 不是 content（被集成测试抓出来的）
    save_args = {"memory_text": "用户喜欢咖啡", "scope": "user:12345"}
    save_tc = ToolCall(id="tc-s", name="save_important_memory", arguments=json.dumps(save_args))
    pipeline, provider, adapter, _, important = await build_pipeline(
        [CompletionResult(tool_calls=[save_tc], finish_reason="tool_calls")]
    )

    await pipeline.enqueue(_msg(text="我喜欢咖啡"))
    await _drain_pipeline(pipeline)

    items = important.items()
    assert any("咖啡" in (i.get("content") or "") for i in items)


@pytest.mark.asyncio
async def test_max_loops_reached_no_crash(build_pipeline):
    """AgentRunner 对无工具纯文本只做纠正重试，不把文本兜底发送。"""
    # 脚本：连续返回纯文本（无工具调用）—— runner 应在一次纠正重试后 give up
    plain = CompletionResult(content="（纯文本）", finish_reason="stop")
    pipeline, provider, adapter, history, _ = await build_pipeline([plain, plain, plain])

    await pipeline.enqueue(_msg(text="测试无工具重试"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    # 不应发出消息（runner 拒绝接受纯文本输出）
    assert adapter.sent == []
    # provider 被调用 2 次：首次纯文本 + 一次纠正重试。
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_tool_loop_finalization_does_not_send_plain_text_to_qq(build_pipeline):
    """工具循环无工具收尾只能写内部记录，不能绕过发送工具发 QQ。"""
    save_args = {"memory_text": "循环中的中间结果"}
    save_tc_1 = ToolCall(id="tc-save-1", name="save_important_memory", arguments=json.dumps(save_args))
    save_tc_2 = ToolCall(id="tc-save-2", name="save_important_memory", arguments=json.dumps(save_args))
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(tool_calls=[save_tc_1], finish_reason="tool_calls"),
            CompletionResult(tool_calls=[save_tc_2], finish_reason="tool_calls"),
            CompletionResult(content="内部最终说明，不应发送到 QQ。", finish_reason="stop"),
        ]
    )
    pipeline.chat_agent.cfg.tool_loop_reminder_interval = 1
    pipeline.chat_agent.cfg.tool_loop_final_warning_count = 1
    pipeline.chat_agent.cfg.tool_loop_final_grace_loops = 1

    await pipeline.enqueue(_msg(text="测试工具循环最终收尾"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    assert adapter.sent == []
    assert provider.calls[-1]["tools"] is None
    records = await history.records()
    assert any(
        record.get("role") == "assistant"
        and "内部最终说明" in str(record.get("content") or "")
        for record in records
    )


@pytest.mark.asyncio
async def test_unknown_scope_fails_gracefully(build_pipeline):
    """B17 防御：如果 collected 里的 scope 既不是 'private' 也不是 'group'，
    _do_send 应该写 system_note 而不是崩。

    这里手动塞一条非法 action 进 ctx.collected 进行验证。"""
    pipeline, _, adapter, history, _ = await build_pipeline([])

    # 直接调 _do_send 触发分支
    result = await pipeline._do_send(
        {"action": "weird_scope", "target": "1", "content": "hi", "label": "x", "delay": 0}
    )
    assert result is None
    assert adapter.sent == []

    records = await history.records()
    assert any(
        "未知 scope" in (r.get("content") or "") for r in records if r.get("role") == "system"
    ), f"应有 system_note 警告未知 scope；实际 records={records}"
