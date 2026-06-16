"""Shared fake pipeline helpers for integration pipeline tests."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from adapters.base import IAdapter
from adapters.types import IncomingMessage, Target, UserInfo
from agents import ChatAgent, Persona
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
from core.message_pipeline import MessagePipeline
from core.state import PendingRequestStore
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

@pytest.fixture(name="build_pipeline")
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


