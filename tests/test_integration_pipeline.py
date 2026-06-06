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
from core.message_pipeline import MessagePipeline, _recommended_context_budget
from core.proactive_loop import ProactiveLoop
from core.recall_handler import RecallHandler
from core.state import PendingMessageItem, PendingRequestStore, RateLimiter
from core.wakeup import WakeupScheduler
from memory import (
    ArchiveStore,
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
            long_term_memory=LongTermMemoryConfig(mode="file", keyword_trigger_save=True),
        ),
        persona=PersonaConfig(active="test_persona"),
        behavior=BehaviorConfig(
            merge_window_seconds=0.05,
            recall_merge_window_seconds=0.05,
            proactive_think_interval_seconds=600.0,
            default_history_fetch_count=10000,
            typing=TypingConfig(chars_per_second=999.0, max_delay_seconds=0.01),
            rate_limit=RateLimitConfig(window_seconds=60, max_messages=100, enabled=False),
            summarize=SummarizeConfig(
                trigger_at_messages=99999, range_start_messages=9000, range_end_messages=11000,
            ),
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
    ):
        cfg = _make_root_config()
        provider = ScriptedProvider(script)
        chat_agent = ChatAgent(provider, cfg.agents.chat)
        persona = _make_persona()
        history = HistoryManager(tmp_path / "history.jsonl")
        important = ImportantMemoryManager(tmp_path / "important.json")
        archive = ArchiveStore(tmp_path / "archive.jsonl")
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
    )


def _ai_send_private(target_qq: str = "123", content: str = "嗨", send_only: bool = True) -> CompletionResult:
    args = {
        "targets": [{"target_qq": target_qq, "content": content, "order": 1}],
        "send_only": send_only,
    }
    tc = ToolCall(id="tc-1", name="send_private_messages", arguments=json.dumps(args))
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls")


def _ai_send_group(group_id: str = "5555", content: str = "群好", send_only: bool = True) -> CompletionResult:
    args = {
        "group_id": int(group_id),
        "targets": [{"content": content, "order": 1}],
        "send_only": send_only,
    }
    tc = ToolCall(id="tc-g", name="send_group_message", arguments=json.dumps(args))
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls")


def _ai_no_action(reason: str = "无需回复") -> CompletionResult:
    _ = reason
    tc = ToolCall(id="tc-na", name="no_action", arguments="{}")
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls")


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
async def test_group_task_context_includes_recent_real_chat_window(build_pipeline):
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
    assert "<recent_group_messages" in task_context
    assert 'limit="10"' in task_context
    assert "前一句" in task_context
    assert "接一句" in task_context
    assert "msg_id=g1" in task_context
    assert "msg_id=g2" in task_context


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
async def test_basic_private_message_flow(build_pipeline):
    """私聊：enqueue → batch → chat_agent → tool → adapter.send_text。

    这条链路全程不抛错，意味着所有跨模块接口名/字段名一致。"""
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [_ai_send_private(target_qq="123", content="收到，你好~", send_only=True)]
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

    # provider 被调用了一次，且 messages 含 system+user
    assert len(provider.calls) == 1
    call_msgs = provider.calls[0]["messages"]
    assert call_msgs[0]["role"] == "system"
    assert any(m["role"] == "user" for m in call_msgs)


@pytest.mark.asyncio
async def test_group_message_flow(build_pipeline):
    """群聊：send_group_message 路径走通，scope=group。"""
    pipeline, _, adapter, _, _ = await build_pipeline([_ai_send_group("5555", "群好", True)])

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
        [_ai_send_group("5555", "群内正常回复", True)],
        rate_limiter=limiter,
    )

    await pipeline.enqueue(_msg(user_id="stranger", group_id="5555", text="群里说话"))
    await _drain_pipeline(pipeline)

    assert len(provider.calls) == 1
    assert [content for _, content in adapter.sent] == ["群内正常回复"]


@pytest.mark.asyncio
async def test_merge_window_groups_by_conversation(build_pipeline):
    """同一合并窗口内的私聊和群聊应拆成两轮，目标不串。"""
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            _ai_send_private(target_qq="123", content="私聊回复", send_only=True),
            _ai_send_group("5555", "群聊回复", True),
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
    assert len(provider.calls) == 2
    call1 = "\n".join(str(m.get("content", "")) for m in provider.calls[0]["messages"])
    call2 = "\n".join(str(m.get("content", "")) for m in provider.calls[1]["messages"])
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
        [_ai_send_private(target_qq="123", content="主动提醒", send_only=True)]
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
    assert "start_agent_task" not in names
    assert "summarize_conversation" not in names
    assert "summarize_chat_history" not in names


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
    assert "工具循环上限" in result["error"]
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
            _ai_send_private(target_qq="123", content="后台结果已完成", send_only=True),
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

    assert len(provider.calls) == 2
    second_messages = provider.calls[1]["messages"]
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
    assert "start_agent_task" not in names
    assert "summarize_chat_history" not in names
    assert "summarize_conversation" not in names
    messages = provider.calls[0]["messages"]
    assert messages[-1]["role"] == "user"
    assert "[系统事件 · 非用户消息]" in messages[-1]["content"]
    assert "定时唤醒已到" in messages[-1]["content"]


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
        "targets": [{"target_qq": 123, "content": "这条会撤回", "order": 1}],
        "send_only": False,
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
        [_ai_send_private(target_qq="456", content="键名一致性测试", send_only=True)]
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
        "targets": [{"target_qq": 456, "emoji": "无语", "order": 1}],
        "send_only": True,
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
        [_ai_send_private(target_qq="456", content="真实回复", send_only=True)]
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
async def test_send_private_with_delay_uses_async_queue(build_pipeline):
    """多条且存在正 delay 时，工具先返回 queued，后台仍按原拆条发完。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "第一条", "order": 1, "delay": 0.05},
            {"target_qq": 123, "content": "第二条", "order": 2, "delay": 0.05},
        ],
        "send_only": True,
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
    assert tool_contents[-1]["status"] == "queued"
    assert tool_contents[-1]["qq_visible"] == "pending"
    assert tool_contents[-1]["data"]["conversation_ids"] == ["private:123"]
    assert tool_contents[-1]["data"]["message_count"] == 2
    assert "brief" not in tool_contents[-1]
    assert "note" not in tool_contents[-1]
    assert any(
        r.get("role") == "user"
        and r.get("metadata", {}).get("kind") == "send_done_snapshot"
        and "发送完成（全部消息已发出）" in (r.get("content") or "")
        for r in records
    )
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_cross_conversation_clean_send_receipt_visible_in_unified_window(build_pipeline):
    """群里触发的私聊异步发送：queued 在群轮，完成记录在私聊目标，但统一窗口都能看到。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "私聊第一条", "order": 1, "delay": 0.05},
            {"target_qq": 123, "content": "私聊第二条", "order": 2, "delay": 0.05},
        ],
        "send_only": True,
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
        and json.loads(r.get("content") or "{}").get("status") == "queued"
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
    assert '"status": "queued"' in joined
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
        "send_only": True,
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
    assert len(provider.calls) == 2
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "m-interrupt" in joined
    assert "<send_receipt>" in joined
    assert '"interrupted": true' in joined
    assert '"content": "二"' in joined
    assert '"content": "三"' in joined
    assert '"qq_visible": false' in joined
    assert '"qq_visible": true' in joined
    receipt_turn_context = "\n".join(
        str(m.get("content", ""))
        for m in provider.calls[-1]["messages"]
        if m.get("role") == "user" and "<send_receipt_task" in str(m.get("content", ""))
    )
    assert "<send_receipt>" in receipt_turn_context
    assert '"interrupted": true' in receipt_turn_context
    assert "按 JSON 字段判断" in receipt_turn_context


@pytest.mark.asyncio
async def test_same_conversation_message_while_model_thinking_returns_stale(build_pipeline):
    """LLM 思考时当前会话来了新消息，旧发送应 stale，并把新消息并入同一轮。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1}],
        "send_only": False,
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
    assert '"status": "stale"' in joined
    tool_contents = [
        json.loads(r["content"])
        for r in records
        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"
    ]
    stale_result = tool_contents[-1]
    assert stale_result["qq_visible"] is False
    attempted = stale_result["attempted_messages"][0]
    assert attempted["send_id"] == stale_result["send_id"]
    assert attempted["conversation_id"] == "private:123"
    assert attempted["target_type"] == "private"
    assert attempted["target_id"] == "123"
    assert attempted["order"] == 1
    assert attempted["content"] == "旧回复"
    assert attempted["delay"] >= 0
    assert attempted["qq_visible"] is False
    assert stale_result["new_visible_messages"][0]["conversation_id"] == "private:123"
    assert stale_result["new_visible_messages"][0]["text"] == "我改口"
    assert stale_result["new_visible_messages"][0]["qq_visible"] is True
    assert "note" not in stale_result
    assert "get_recent_chat_messages" in stale_result["next"]
    assert "m-new" in joined
    assert "<send_receipt>" in joined
    timeline_markdown = pipeline.chat_timeline.to_markdown(
        pipeline.chat_timeline.recent("private:123", 10)
    )
    assert "m-old" in timeline_markdown
    assert "m-new" in timeline_markdown
    assert "旧回复" not in timeline_markdown


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
        "send_only": True,
    }
    second_args = {
        "targets": [{"target_qq": 123, "content": "新回复", "order": 1}],
        "send_only": True,
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
    assert len(provider.calls) == 2
    assert not pipeline._send_manager.should_defer_batch("private:123")
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "补一句" in joined


@pytest.mark.asyncio
async def test_recalled_pending_message_is_not_processed_as_new_task(build_pipeline):
    """合并窗口内被撤回的消息只记录状态，不再触发主模型接旧话。"""
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [_ai_send_private(target_qq="123", content="不该发", send_only=True)]
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
    """模型思考中的触发消息被撤回时，旧回复不能继续发出。"""
    started = asyncio.Event()
    release = asyncio.Event()
    send_args = {
        "targets": [{"target_qq": 123, "content": "旧回复", "order": 1}],
        "send_only": False,
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
    stale_results = [
        json.loads(record["content"])
        for record in records
        if record.get("role") == "tool" and record.get("tool_call_id") == "tc-send"
    ]
    assert stale_results
    assert stale_results[-1]["status"] == "stale"
    assert stale_results[-1]["recalled_messages"][0]["msg_id"] == "m-old"


@pytest.mark.asyncio
async def test_other_conversation_does_not_interrupt_async_send(build_pipeline):
    """A 会话后台发送时，B 会话入站只排自己的轮，不冲掉 A 的队列。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "A1", "order": 1, "delay": 0.1},
            {"target_qq": 123, "content": "A2", "order": 2, "delay": 0.1},
        ],
        "send_only": True,
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
    assert len(provider.calls) == 2


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
async def test_keyword_force_save_triggered(build_pipeline):
    """关键词强制保存：包含「记住」时自动写入 important.json，不依赖 AI 主动调工具。"""
    pipeline, _, _, _, important = await build_pipeline([_ai_no_action()])

    await pipeline.enqueue(_msg(text="记住我喜欢吃寿司"))
    await _drain_pipeline(pipeline)

    items = important.items()
    assert any("寿司" in (i.get("content") or "") for i in items), (
        f"关键词「记住」未触发强制保存。items={items}"
    )


@pytest.mark.asyncio
async def test_token_compaction_archives_before_truncate(build_pipeline):
    class FakeSummaryAgent:
        async def summarize_rolling(
            self,
            history_slice,
            existing_summary_text,
            existing_important_text,
        ):
            return {
                "summary_text": f"{existing_summary_text}\n已归档 {len(history_slice)} 条".strip(),
                "new_important": [{"content": "归档中提到用户喜欢测试"}],
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
    await pipeline._maybe_summarize()

    archived = await pipeline.archive.records()
    after = await history.length()
    assert archived, "压缩前应先把原文写入 archive"
    assert after < before
    assert "已归档" in pipeline.rolling_summary.text()
    assert any("喜欢测试" in item.get("content", "") for item in important.items())


@pytest.mark.asyncio
async def test_compaction_is_scheduled_in_background(build_pipeline):
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
    await _drain_pipeline(pipeline)
    await asyncio.wait_for(agent.started.wait(), timeout=1.0)

    assert pipeline._batch_task is None or pipeline._batch_task.done()
    assert pipeline._summary_task is not None and not pipeline._summary_task.done()

    agent.release.set()
    await asyncio.wait_for(pipeline._summary_task, timeout=1.0)
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
    executor = registry.get_executor(
        ToolContext(archive=pipeline.archive, history=history)
    )

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
async def test_rag_mode_does_not_write_important_memory(build_pipeline):
    pipeline, _, _, _, important = await build_pipeline([_ai_no_action()])
    pipeline.features_cfg.long_term_memory.mode = "rag"

    await pipeline.enqueue(_msg(text="记一下：我报名了某项长期活动，7月7日有选拔环节"))
    await _drain_pipeline(pipeline)

    assert important.items() == []


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
async def test_wakeup_action_uses_normal_window_with_reminder_as_current_task(build_pipeline):
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [_ai_send_private(target_qq="123", content="到点了", send_only=True)]
    )
    await history.add_user_message("旧消息：请执行已完成的无关任务")

    await pipeline.run_wakeup_turn(
        "内部继续任务：检查后台状态",
        {"target_type": "private", "target_id": 123},
    )

    messages = provider.calls[-1]["messages"]
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
async def test_no_tool_after_pending_send_finishes_without_retry(build_pipeline):
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            _ai_send_private(target_qq="123", content="先说一句", send_only=False),
            CompletionResult(content="这句纯文本不应该触发纠正", finish_reason="stop"),
        ]
    )

    await pipeline.enqueue(_msg(text="测试发送后纯文本"))
    await _drain_pipeline(pipeline)

    assert [content for _, content in adapter.sent] == ["先说一句"]
    records = await history.records()
    assert not any(
        "错误：未调用工具" in (r.get("content") or "") for r in records
    )


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
    """多轮：AI 第一次 save_important_memory（no_feedback），第二次 send_private（结束）。

    验证 runner 不会在 save_important 后提前 finish——它属于 no_feedback，
    单步 save 后应该继续等 send_private 或 no_action。

    注意：当前 runner 的 _all_no_feedback 设计是「所有 tool_calls 都属于 no_feedback
    才提前结束」，所以单个 save_important_memory 会让循环结束。这里实际验证的是
    runner 的真实行为，不强求多轮。"""
    # 工具参数字段名是 memory_text 不是 content（被集成测试抓出来的）
    save_args = {"memory_text": "用户喜欢咖啡"}
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
    """AgentRunner 在达到 max_loops 时优雅退出（不抛错），且不污染 adapter。"""
    # 脚本：连续 3 次返回纯文本（无工具调用）—— runner 应在第 3 轮 give up
    plain = CompletionResult(content="（纯文本）", finish_reason="stop")
    pipeline, provider, adapter, history, _ = await build_pipeline([plain, plain, plain])

    await pipeline.enqueue(_msg(text="测试 max_loops"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    # 不应发出消息（runner 拒绝接受纯文本输出）
    assert adapter.sent == []
    # provider 被调用了 max_loops 次（含前两次的"纠正消息"重试）
    assert len(provider.calls) == 3


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
