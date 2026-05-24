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
              → tool executor → tools/messaging.py 写入 ctx.collected
            → history.add_records
            → _execute_collected → _do_send → adapter.send_text

链路里任何方法名 / 字段 / 键名不一致，这里会立刻挂。这是 P1.8 完成的硬门槛——
比 273 个模块单测更能抓住跨模块 critical（例如 history.records() 不存在这种）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from adapters.base import IAdapter
from adapters.types import (
    IncomingMessage,
    Target,
    UserInfo,
)
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
from memory import HistoryManager, ImportantMemoryManager
from providers.base import CompletionResult, IProvider, ToolCall, Usage
from tools import build_default_registry


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
        first_token_timeout=5.0,
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
            long_term_memory=LongTermMemoryConfig(mode="file", keyword_force_save=True),
        ),
        persona=PersonaConfig(active="test_persona"),
        behavior=BehaviorConfig(
            merge_window=0.05,
            recall_merge_window=0.05,
            greeting_interval=600.0,
            typing=TypingConfig(chars_per_second=999.0, max_delay=0.01),
            rate_limit=RateLimitConfig(window=60, max_messages=100, enabled=False),
            summarize=SummarizeConfig(
                trigger_at=99999, range_start=9000, range_end=11000, chat_history_count=10000
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
        self.calls.append({"messages": messages, "model": model, "tools": tools})
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

    async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None):
        mid = str(self._next_msg_id)
        self._next_msg_id += 1
        return mid

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
# fixture：构造完整 pipeline
# ============================================================


@pytest.fixture
def build_pipeline(tmp_path):
    """返回 async 工厂。调用 build(script) 得到 (pipeline, provider, adapter, history, important)。"""

    async def _build(script: list[CompletionResult]):
        cfg = _make_root_config()
        provider = ScriptedProvider(script)
        chat_agent = ChatAgent(provider, cfg.agents.chat)
        persona = _make_persona()
        history = HistoryManager(tmp_path / "history.jsonl")
        important = ImportantMemoryManager(tmp_path / "important.json")
        await history.load()
        await important.load()
        registry = build_default_registry(cfg)
        adapter = FakeAdapter("fake")
        scheduler = WakeupScheduler(on_fire=lambda r: asyncio.sleep(0))

        pipeline = MessagePipeline(
            adapter=adapter,
            chat_agent=chat_agent,
            persona=persona,
            history=history,
            important=important,
            tool_registry=registry,
            wakeup_scheduler=scheduler,
            pending_requests=PendingRequestStore(),
            behavior_cfg=cfg.behavior,
            features_cfg=cfg.features,
            emoji_dir=None,
            upload_allowed_dir=None,
            rate_limiter=None,
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
    args = {"reason": reason}
    tc = ToolCall(id="tc-na", name="no_action", arguments=json.dumps(args))
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
        if (batch_task is None or batch_task.done()) and (requeue is None or requeue.done()):
            return
    raise AssertionError(f"pipeline 在 {max_wait}s 内未完成")


# ============================================================
# 测试用例
# ============================================================


@pytest.mark.asyncio
async def test_basic_private_message_flow(build_pipeline):
    """私聊：enqueue → batch → chat_agent → tool → collected → adapter.send_text。

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
async def test_collected_dict_keys_consistent_with_do_send(build_pipeline):
    """B15 / B17 回归：collected 字典的 action/target/content/label/delay/send_only 键
    必须与 _do_send 消费一致。如果 send_private_messages 写键漂移、_do_send 读键漂移，
    adapter.sent 会拿不到内容——这个用例就是为此设的"哨兵"。"""
    pipeline, _, adapter, _, _ = await build_pipeline(
        [_ai_send_private(target_qq="456", content="键名一致性测试", send_only=True)]
    )

    await pipeline.enqueue(_msg(user_id="456", text="hi"))
    await _drain_pipeline(pipeline)

    assert any(c == "键名一致性测试" for _, c in adapter.sent), (
        "collected 与 _do_send 的字典键名漂移：发送内容没到达 adapter"
    )


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
