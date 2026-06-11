"""端到端集成测试：NapCat JSON 事件 → EventBus → Pipeline → send_text 全链路。

用 mock IProvider + mock IAdapter（send_text 拦截到 sent_messages），但 event 解析、
EventBus、MessagePipeline、AgentRunner、ToolExecutor 全是真实代码。

这是 P1.9 的集成验证：任何跨模块接口不一致 / 调用方实现方不匹配都会在这里暴露。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from adapters.base import IAdapter, Target
from adapters.napcat.events import parse_napcat_event
from adapters.types import UserInfo
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
from core.event_bus import EventBus
from core.message_pipeline import MessagePipeline
from core.state import PendingRequestStore
from core.wakeup import WakeupScheduler
from memory import HistoryManager, ImportantMemoryManager
from providers.base import CompletionResult, IProvider, ToolCall, Usage
from tools import build_default_registry

# ============================================================
# Fakes
# ============================================================


class ScriptedProvider(IProvider):
    def __init__(self, responses: list[CompletionResult]) -> None:
        super().__init__(name="scripted")
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat_completion(self, messages, *, model, tools=None, **kw) -> CompletionResult:
        self.calls.append({"messages": messages, "model": model, "tools": tools})
        if not self._responses:
            return CompletionResult(
                tool_calls=[ToolCall(id="done", name="no_action", arguments="{}")],
                finish_reason="tool_calls",
                usage=Usage(),
            )
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


class E2EFakeAdapter(IAdapter):
    """实现完整 IAdapter，send_text 拦截到 sent_messages。"""

    def __init__(self, name: str = "fake") -> None:
        super().__init__(name)
        self.sent_messages: list[tuple[Target, str]] = []
        self._connected = True
        self._next_id = 1000

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send_text(self, target: Target, content: str) -> str:
        mid = str(self._next_id)
        self._next_id += 1
        self.sent_messages.append((target, content))
        return mid

    async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None):
        return str(self._next_id)

    async def recall(self, message_id: str) -> bool:
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


# ============================================================
# Helpers
# ============================================================


def _make_persona() -> Persona:
    return Persona(
        name="test",
        prompt="<identity>你是测试 AI</identity>",
        vars={"name": "test", "admins": []},
    )


def _make_agent_cfg() -> AgentConfig:
    return AgentConfig(
        provider="fake", model="fake-1", temperature=0.6, max_tokens=512,
        max_loops=2, refocus_interval=0, first_token_timeout_seconds=5.0,
    )


def _make_config() -> RootConfig:
    return RootConfig(
        providers={"fake": ProviderConfig(
            preset=None, protocol="openai_compat",
            base_url="https://example.com", api_key_id=None,
        )},
        adapters={"fake": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=_make_agent_cfg()),
        features=FeaturesConfig(
            long_term_memory=LongTermMemoryConfig(mode="file"),
        ),
        persona=PersonaConfig(active="test"),
        behavior=BehaviorConfig(
            merge_window_seconds=0.05,
            recall_merge_window_seconds=0.05,
            proactive_think_interval_seconds=600.0,
            default_history_fetch_count=100,
            typing=TypingConfig(
                chars_per_second=999.0,
            ),
            rate_limit=RateLimitConfig(window_seconds=60, max_messages=100, enabled=False),
            summarize=SummarizeConfig(),
        ),
    )


def _make_napcat_msg_json(
    *,
    user_id: int = 123456,
    group_id: int | None = None,
    text: str = "你好",
    message_id: int = 1,
    adapter_name: str = "fake",
) -> dict:
    """构造一个 OneBot V11 消息 JSON（与 NapCat 真实上报格式一致）。"""
    msg_type = "group" if group_id else "private"
    msg = {
        "post_type": "message",
        "message_type": msg_type,
        "time": 1700000000,
        "self_id": 999999,
        "sub_type": "normal",
        "message_id": message_id,
        "user_id": user_id,
        "message": text,
        "raw_message": text,
        "font": 0,
        "sender": {
            "user_id": user_id,
            "nickname": f"User{user_id}",
            "sex": "unknown",
            "age": 0,
        },
    }
    if group_id:
        msg["group_id"] = group_id
    return msg


# ============================================================
# Tests
# ============================================================


@pytest.mark.asyncio
async def test_private_message_napcat_json_to_send(tmp_path):
    """私聊: NapCat JSON → parse_napcat_event → EventBus → Pipeline → send_text。

    真测试 parse_napcat_event（JSON→IncomingMessage 的解析）、EventBus、
    MessagePipeline、AgentRunner、ToolExecutor 全链路。
    """
    cfg = _make_config()
    provider = ScriptedProvider([
        CompletionResult(
            tool_calls=[
                ToolCall(
                    id="tc-1",
                    name="send_private_messages",
                    arguments=json.dumps({
                        "targets": [
                            {
                                "target_qq": "123456",
                                "content": "收到，回复中",
                                "order": 1,
                                "delay": 0,
                            }
                        ],
                    }),
                ),
            ],
            finish_reason="tool_calls",
        ),
    ])

    persona = _make_persona()
    history = HistoryManager(tmp_path / "history.jsonl")
    important = ImportantMemoryManager(tmp_path / "important.json")
    await history.load()
    await important.load()

    chat_agent = ChatAgent(provider, cfg.agents.chat)
    registry = build_default_registry(cfg)
    adapter = E2EFakeAdapter("fake")

    scheduler = WakeupScheduler(
        on_fire=lambda r, target=None, mode="wakeup", message_text=None: asyncio.sleep(0)
    )
    pipeline = MessagePipeline(
        adapter=adapter, chat_agent=chat_agent, persona=persona,
        history=history, important=important, tool_registry=registry,
        wakeup_scheduler=scheduler,
        pending_requests=PendingRequestStore(),
        behavior_cfg=cfg.behavior, features_cfg=cfg.features,
        emoji_dir=None, workspace_dir=None,
        rate_limiter=None, summary_agent=None,
    )
    scheduler._on_fire = pipeline.run_wakeup_turn

    bus = EventBus()
    bus.on_message(pipeline.enqueue)
    bus.bind_adapter(adapter)

    # 模拟 NapCat 上报一条私聊 JSON
    raw = _make_napcat_msg_json(user_id=123456, text="你好", group_id=None, adapter_name="fake")
    event = parse_napcat_event("fake", raw)
    assert event is not None, "parse_napcat_event 返回 None —— JSON 格式可能不匹配"

    await bus.dispatch(event)

    # 等待 pipeline 跑完
    await asyncio.sleep(0.3)

    assert len(adapter.sent_messages) >= 1, f"未发送: sent={adapter.sent_messages}"
    target, content = adapter.sent_messages[0]
    assert target.scope == "private"
    assert target.target_id == "123456"
    assert "回复中" in content

    records = await history.records()
    roles = [r.get("role") for r in records]
    assert "user" in roles, f"history 无 user: {records}"
    assert "assistant" in roles, f"history 无 assistant: {records}"

    assert len(provider.calls) >= 1, "provider 从未被调用"
    await pipeline.shutdown()
    await provider.aclose()


@pytest.mark.asyncio
async def test_group_message_napcat_json_to_send(tmp_path):
    """群聊: NapCat JSON → send_group_message 发出。"""
    cfg = _make_config()
    provider = ScriptedProvider([
        CompletionResult(
            tool_calls=[
                ToolCall(
                    id="tc-g",
                    name="send_group_message",
                    arguments=json.dumps({
                        "group_id": 5555,
                        "targets": [{"content": "收到群聊", "order": 1, "delay": 0}],
                    }),
                ),
            ],
            finish_reason="tool_calls",
        ),
    ])

    persona = _make_persona()
    history = HistoryManager(tmp_path / "history.jsonl")
    important = ImportantMemoryManager(tmp_path / "important.json")
    await history.load()
    await important.load()

    chat_agent = ChatAgent(provider, cfg.agents.chat)
    registry = build_default_registry(cfg)
    adapter = E2EFakeAdapter("fake")

    pipeline = MessagePipeline(
        adapter=adapter, chat_agent=chat_agent, persona=persona,
        history=history, important=important, tool_registry=registry,
        wakeup_scheduler=WakeupScheduler(
            on_fire=lambda r, target=None, mode="wakeup", message_text=None: asyncio.sleep(0)
        ),
        pending_requests=PendingRequestStore(),
        behavior_cfg=cfg.behavior, features_cfg=cfg.features,
        emoji_dir=None, workspace_dir=None,
        rate_limiter=None, summary_agent=None,
    )

    bus = EventBus()
    bus.on_message(pipeline.enqueue)
    bus.bind_adapter(adapter)

    raw = _make_napcat_msg_json(user_id=9, group_id=5555, text="在吗", adapter_name="fake")
    event = parse_napcat_event("fake", raw)
    assert event is not None

    await bus.dispatch(event)
    await asyncio.sleep(0.3)

    assert any(
        t.scope == "group" and t.target_id == "5555" and "收到群聊" in c
        for t, c in adapter.sent_messages
    ), f"群 send_text 未触发: sent={adapter.sent_messages}"

    records = await history.records()
    roles = [r.get("role") for r in records]
    assert "user" in roles and "assistant" in roles

    await pipeline.shutdown()
    await provider.aclose()


@pytest.mark.asyncio
async def test_parse_napcat_json_fields_match_incoming_message(tmp_path):
    """验证 parse_napcat_event 输出字段与 pipeline 消费端一致。

    关键：user_id / group_id / message_id / text / raw_message / scope / source_target
    这些字段在 pipeline._build_readable_text、batch.append、
    rate_limiter.check_and_log 等处被访问，字段名必须对得上。
    """
    raw = _make_napcat_msg_json(user_id=777, group_id=888, text="测试内容", message_id=99)
    event = parse_napcat_event("fake", raw)

    assert event is not None
    assert event.user_id == "777"
    assert event.group_id == "888"
    assert event.text == "测试内容"
    assert event.message_id == "99"
    assert event.raw_message == "测试内容"
    assert event.is_group() is True
    # source_target 用于 adapter.send_text 获取返回目标
    assert event.source_target.adapter == "fake"
    assert event.source_target.scope == "group"
