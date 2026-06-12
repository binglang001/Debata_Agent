from __future__ import annotations

import asyncio
import copy
import json
import time
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
from core.state import PendingRequestStore, RateLimiter
from core.wakeup import WakeupScheduler
from memory import (
    ArchiveStore,
    EventStore,
    HistoryManager,
    ImportantMemoryManager,
    RollingSummaryStore,
)
from providers.base import CompletionResult, IProvider, ToolCall, Usage
from tools import build_default_registry


class ScriptedProvider(IProvider):
    def __init__(self, script: list[CompletionResult] | None = None) -> None:
        super().__init__(name="scripted")
        self._script = list(script or [])
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
        if self._script:
            return self._script.pop(0)
        return _ai_no_action()

    async def aclose(self) -> None:
        pass


class FakeAdapter(IAdapter):
    def __init__(self, name: str = "fake") -> None:
        super().__init__(name)
        self.sent: list[tuple[Target, str]] = []
        self._next_msg_id = 1000

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        return True

    async def send_text(self, target: Target, content: str) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        self.sent.append((target, content))
        return msg_id

    async def send_voice(self, target: Target, audio_path: Any) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        return msg_id

    async def send_image(
        self,
        target: Target,
        *,
        image_path: Any = None,
        image_url: str | None = None,
        image_b64: str | None = None,
    ) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        return msg_id

    async def recall(self, message_id: str) -> bool:
        return True

    async def list_friends(self) -> list[Any]:
        return []

    async def list_groups(self) -> list[Any]:
        return []

    async def list_group_members(self, group_id: str) -> list[Any]:
        return []

    async def get_user_info(self, user_id: str) -> UserInfo:
        return UserInfo(user_id=user_id, nickname="unknown")

    async def handle_friend_request(self, flag: str, approve: bool, remark: str = "") -> None:
        pass

    async def handle_group_request(
        self,
        flag: str,
        sub_type: str,
        approve: bool,
        reason: str = "",
    ) -> None:
        pass

    async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
        return {}


class FailingEventStore:
    def __init__(self) -> None:
        self.appended_events: list[dict[str, Any]] = []

    async def append_event(self, **event: Any) -> int:
        self.appended_events.append(event)
        raise RuntimeError("append log failed")


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


def _make_root_config(*, merge_window_seconds: float = 0.01) -> RootConfig:
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
        agents=AgentsConfig(
            chat=AgentConfig(
                provider="fake",
                model="fake-1",
                temperature=0.6,
                max_tokens=1024,
                max_loops=3,
                refocus_interval=0,
                first_token_timeout_seconds=5.0,
            )
        ),
        features=FeaturesConfig(
            long_term_memory=LongTermMemoryConfig(mode="file"),
        ),
        persona=PersonaConfig(active="test_persona"),
        behavior=BehaviorConfig(
            merge_window_seconds=merge_window_seconds,
            recall_merge_window_seconds=0.01,
            proactive_think_interval_seconds=600.0,
            default_history_fetch_count=10000,
            typing=TypingConfig(
                chars_per_second=999.0,
            ),
            rate_limit=RateLimitConfig(window_seconds=60, max_messages=100, enabled=False),
            summarize=SummarizeConfig(),
        ),
    )


def _make_persona() -> Persona:
    return Persona(
        name="test_persona",
        prompt="<identity>你是测试用 AI</identity>",
        vars={"name": "测试机器人", "admins": []},
    )


async def _build_pipeline(
    tmp_path,
    *,
    event_store: Any = None,
    merge_window_seconds: float = 0.01,
    rate_limiter: RateLimiter | None = None,
    script: list[CompletionResult] | None = None,
) -> tuple[MessagePipeline, ScriptedProvider, FakeAdapter, HistoryManager]:
    cfg = _make_root_config(merge_window_seconds=merge_window_seconds)
    provider = ScriptedProvider(script)
    chat_agent = ChatAgent(provider, cfg.agents.chat)
    history = HistoryManager(tmp_path / f"history-{time.monotonic_ns()}.jsonl")
    important = ImportantMemoryManager(tmp_path / f"important-{time.monotonic_ns()}.json")
    archive = ArchiveStore(tmp_path / f"archive-{time.monotonic_ns()}.sqlite3")
    rolling_summary = RollingSummaryStore(tmp_path / f"summary-{time.monotonic_ns()}.json")
    await history.load()
    await important.load()
    await archive.load()
    await rolling_summary.load()
    adapter = FakeAdapter()
    scheduler = WakeupScheduler(
        on_fire=lambda r, target=None, mode="wakeup", message_text=None: asyncio.sleep(0)
    )
    pipeline = MessagePipeline(
        adapter=adapter,
        chat_agent=chat_agent,
        persona=_make_persona(),
        history=history,
        important=important,
        archive=archive,
        rolling_summary=rolling_summary,
        tool_registry=build_default_registry(cfg),
        wakeup_scheduler=scheduler,
        pending_requests=PendingRequestStore(),
        behavior_cfg=cfg.behavior,
        features_cfg=cfg.features,
        rate_limiter=rate_limiter,
        summary_agent=None,
        event_store=event_store,
    )
    scheduler._on_fire = pipeline.run_wakeup_turn
    return pipeline, provider, adapter, history


def _msg(
    *,
    user_id: str = "123",
    group_id: str | None = None,
    text: str = "你好",
    message_id: str = "m1",
    timestamp: float = 1234.0,
) -> IncomingMessage:
    scope = "group" if group_id else "private"
    return IncomingMessage(
        adapter="fake",
        timestamp=timestamp,
        self_id="999",
        message_id=message_id,
        scope=scope,
        user_id=user_id,
        nickname="用户",
        group_id=group_id,
        text=text,
        raw_message=text,
        raw={
            "message_id": message_id,
            "user_id": user_id,
            "group_id": group_id,
            "message": text,
            "sender": {"nickname": "用户"},
        },
    )


def _ai_no_action() -> CompletionResult:
    tc = ToolCall(id="tc-na", name="no_action", arguments="{}")
    return CompletionResult(tool_calls=[tc], finish_reason="tool_calls", usage=Usage())


async def _drain_pipeline(pipeline: MessagePipeline, max_wait: float = 1.0) -> None:
    elapsed = 0.0
    step = 0.02
    while elapsed < max_wait:
        await asyncio.sleep(step)
        elapsed += step
        batch_task = pipeline._batch_task
        requeue = pipeline._requeue_task
        send_states = getattr(getattr(pipeline, "_send_manager", None), "_states", {})
        send_workers_done = all(
            state.worker is None or state.worker.done() for state in send_states.values()
        )
        receipt_tasks = getattr(pipeline, "_send_receipt_tasks", {})
        receipt_tasks_done = all(task.done() for task in receipt_tasks.values())
        if (
            (batch_task is None or batch_task.done())
            and (requeue is None or requeue.done())
            and send_workers_done
            and receipt_tasks_done
        ):
            return
    raise AssertionError("pipeline 未在限定时间内完成")


async def _wait_projected(store: EventStore, event_id: int | None = None) -> None:
    assert await store.wait_projected(event_id, timeout=1.0)


@pytest.mark.asyncio
async def test_event_store_roundtrips_qq_message_events(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")

    received_event_id = await store.append_event(
        event_type="qq_message_received",
        conversation_id="private:123",
        source="fake",
        external_id="in-1",
        idempotency_key="qq_message_received:fake:private:123:in-1",
        timestamp_unix=1234.0,
        payload={"direction": "inbound", "conversation_id": "private:123", "msg_id": "in-1"},
    )
    sent_event_id = await store.append_event(
        event_type="qq_message_sent",
        conversation_id="private:123",
        source="fake",
        external_id="out-1",
        idempotency_key="qq_message_sent:private:123:out-1:sent",
        timestamp_unix=1235.0,
        payload={"direction": "outbound", "conversation_id": "private:123", "msg_id": "out-1"},
    )
    assert sent_event_id > received_event_id
    await _wait_projected(store, sent_event_id)

    received = await store.events_by_type("qq_message_received", limit=10)
    sent = await store.events_by_type("qq_message_sent", limit=10)
    conversation = await store.events_for_conversation("private:123", limit=10)

    assert received[0]["payload"]["direction"] == "inbound"
    assert sent[0]["payload"]["direction"] == "outbound"
    assert [event["event_type"] for event in conversation] == [
        "qq_message_received",
        "qq_message_sent",
    ]


@pytest.mark.asyncio
async def test_inbound_enqueue_writes_event_without_changing_history_or_model_shape(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    pipeline, provider, _adapter, history = await _build_pipeline(
        tmp_path,
        event_store=store,
        script=[_ai_no_action()],
    )

    await pipeline.enqueue(_msg(text="真实入站", message_id="in-1"))
    await _drain_pipeline(pipeline)
    await _wait_projected(store)

    events = await store.events_by_type("qq_message_received", limit=10)
    assert len(events) == 1
    event = events[0]
    assert event["conversation_id"] == "private:123"
    assert event["source"] == "fake"
    assert event["external_id"] == "in-1"
    assert event["timestamp_unix"] == 1234.0
    assert event["payload"]["direction"] == "inbound"
    assert event["payload"]["conversation_id"] == "private:123"
    assert event["payload"]["msg_id"] == "in-1"
    assert event["payload"]["message_id"] == "in-1"
    assert event["payload"]["sender_name"] == "用户"
    assert event["payload"]["target_id"] == "123"
    assert event["payload"]["content"] == "真实入站"
    assert event["payload"]["raw_event"]["raw"]["sender"]["nickname"] == "用户"

    records = await history.records()
    user_record = next(record for record in records if record.get("role") == "user")
    assert set(user_record) == {"role", "content", "metadata", "conversation_id"}
    meta_message = user_record["metadata"]["messages"][0]
    assert set(meta_message) == {
        "scope",
        "target_id",
        "group_id",
        "user_id",
        "nickname",
        "message_id",
        "timestamp",
        "location",
        "text",
        "inbound_seq",
        "received_at",
    }
    assert "direction" not in meta_message
    assert "event_type" not in meta_message

    model_messages_json = json.dumps(provider.calls[0]["messages"], ensure_ascii=False)
    assert "qq_message_received" not in model_messages_json
    assert "direction" not in model_messages_json


@pytest.mark.asyncio
async def test_inbound_raw_deep_object_is_stringified_with_length_limit(tmp_path):
    class HugeRawObject:
        def __str__(self) -> str:
            return "raw-object:" + "x" * 10000

    store = EventStore(tmp_path / "events.sqlite3")
    pipeline, _provider, _adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=store,
        merge_window_seconds=60.0,
    )
    event = _msg(text="含深层 raw", message_id="deep-raw")
    event.raw = {"level1": {"level2": {"level3": HugeRawObject()}}}

    await pipeline.enqueue(event)
    await _wait_projected(store)

    events = await store.events_by_type("qq_message_received", limit=10)
    raw_value = events[0]["payload"]["raw_event"]["raw"]["level1"]["level2"]["level3"]
    payload_json = json.dumps(events[0]["payload"], ensure_ascii=False)

    assert len(raw_value) == 2000
    assert raw_value.startswith("raw-object:")
    assert "x" * 5000 not in payload_json

    await pipeline.shutdown()


@pytest.mark.asyncio
async def test_inbound_received_event_uses_stable_idempotency_key(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    pipeline, _provider, _adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=store,
        merge_window_seconds=60.0,
    )

    event = _msg(text="重复消息", message_id="dupe-1")
    await pipeline.enqueue(event)
    await pipeline.enqueue(event)
    await _wait_projected(store)

    events = await store.events_by_type("qq_message_received", limit=10)
    assert len(events) == 1
    assert events[0]["idempotency_key"] == "qq_message_received:fake:private:123:dupe-1"

    await pipeline.shutdown()


@pytest.mark.asyncio
async def test_outbound_success_record_point_acks_sent_event_append_log(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    pipeline, _provider, _adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=store,
        merge_window_seconds=60.0,
    )

    assert await store.events_by_type("qq_message_sent", limit=10) == []
    pipeline._self_id_by_conversation["private:123"] = "999"
    await pipeline._record_successful_outbound(
        {
            "target_scope": "private",
            "target_id": "123",
            "content": "已发送",
            "label": "已发送",
            "kind": "text",
        },
        conversation_id="private:123",
        msg_id="out-1",
    )
    stats = await store.stats()
    assert stats["last_appended_event_id"] == 1

    await _wait_projected(store)
    events = await store.events_by_type("qq_message_sent", limit=10)
    assert len(events) == 1
    event = events[0]
    assert event["conversation_id"] == "private:123"
    assert event["source"] == "fake"
    assert event["external_id"] == "out-1"
    assert event["idempotency_key"] == "qq_message_sent:private:123:out-1:sent"
    assert event["payload"]["direction"] == "outbound"
    assert event["payload"]["msg_id"] == "out-1"
    assert event["payload"]["content"] == "已发送"
    assert event["payload"]["target_scope"] == "private"
    assert event["payload"]["target_id"] == "123"
    assert event["payload"]["self_id"] == "999"

    await pipeline._record_successful_outbound(
        {
            "target_scope": "private",
            "target_id": "123",
            "content": "重复",
            "label": "重复",
            "kind": "text",
        },
        conversation_id="private:123",
        msg_id="out-1",
    )
    await _wait_projected(store)
    assert len(await store.events_by_type("qq_message_sent", limit=10)) == 1


@pytest.mark.asyncio
async def test_inbound_event_store_append_log_failure_blocks_enqueue(tmp_path):
    failing_store = FailingEventStore()
    pipeline, _provider, _adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=failing_store,
        merge_window_seconds=60.0,
    )

    try:
        with pytest.raises(RuntimeError, match="append log failed"):
            await pipeline.enqueue(_msg(text="事件库失败阻断入队", message_id="fail-in"))

        assert failing_store.appended_events[0]["event_type"] == "qq_message_received"
        assert (
            failing_store.appended_events[0]["idempotency_key"]
            == "qq_message_received:fake:private:123:fail-in"
        )
        assert pipeline.batch.is_empty_unsafe()
        assert pipeline._batch_task is None
        assert "private:123" not in pipeline._send_manager._recent_inbound
    finally:
        await pipeline.shutdown()


@pytest.mark.asyncio
async def test_outbound_event_store_append_log_failure_is_raised(tmp_path):
    failing_store = FailingEventStore()
    pipeline, _provider, _adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=failing_store,
        merge_window_seconds=60.0,
    )

    with pytest.raises(RuntimeError, match="append log failed"):
        await pipeline._record_successful_outbound(
            {
                "target_scope": "private",
                "target_id": "123",
                "content": "审计失败应上抛",
                "label": "审计失败应上抛",
                "kind": "text",
            },
            conversation_id="private:123",
            msg_id="fail-out",
        )

    assert failing_store.appended_events[0]["event_type"] == "qq_message_sent"
    assert (
        failing_store.appended_events[0]["idempotency_key"]
        == "qq_message_sent:private:123:fail-out:sent"
    )
    assert pipeline.chat_timeline.recent("private:123", 10)[0].msg_id == "fail-out"

    await pipeline.shutdown()


@pytest.mark.asyncio
async def test_outbound_append_log_failure_reports_after_qq_send_side_effect(tmp_path):
    failing_store = FailingQQSentEventStore()
    pipeline, _provider, adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=failing_store,
        merge_window_seconds=60.0,
    )

    result = await pipeline._send_manager.submit(
        [
            {
                "kind": "text",
                "target_scope": "private",
                "target_id": "123",
                "content": "QQ 已发送但审计失败",
                "label": "QQ 已发送但审计失败",
                "order": 1,
                "delay": 0,
            }
        ],
        "send_private_messages",
    )

    assert adapter.sent[0][0].scope == "private"
    assert adapter.sent[0][0].target_id == "123"
    assert adapter.sent[0][1] == "QQ 已发送但审计失败"
    assert result["ok"] is False
    assert any("append log failed" in error for error in result["errors"])
    assert [event["event_type"] for event in failing_store.appended_events] == [
        "send_batch_accepted",
        "send_message_started",
        "qq_message_sent",
        "send_receipt_recorded",
    ]

    await pipeline.shutdown()


@pytest.mark.asyncio
async def test_rate_limit_auto_reply_append_log_failure_blocks_enqueue(tmp_path):
    failing_store = FailingQQSentEventStore()
    limiter = RateLimiter(window_seconds=60, max_messages=0)
    pipeline, provider, adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=failing_store,
        merge_window_seconds=60.0,
        rate_limiter=limiter,
    )

    try:
        with pytest.raises(RuntimeError, match="append log failed"):
            await pipeline.enqueue(_msg(text="超限消息", message_id="rate-limit-in"))

        assert adapter.sent[0][0].scope == "private"
        assert adapter.sent[0][0].target_id == "123"
        assert "已超出速率限制" in adapter.sent[0][1]
        assert provider.calls == []
        assert pipeline.batch.is_empty_unsafe()
        assert pipeline._batch_task is None
        assert [event["event_type"] for event in failing_store.appended_events] == [
            "qq_message_sent"
        ]
        assert (
            failing_store.appended_events[0]["idempotency_key"]
            == "qq_message_sent:private:123:1000:sent"
        )
    finally:
        await pipeline.shutdown()


@pytest.mark.asyncio
async def test_rate_limit_auto_reply_projection_failure_does_not_block_enqueue(tmp_path):
    store = ProjectionFailingEventStore(
        tmp_path / "events.sqlite3",
        projection_retry_delay=0.001,
    )
    limiter = RateLimiter(window_seconds=60, max_messages=0)
    pipeline, provider, adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=store,
        merge_window_seconds=60.0,
        rate_limiter=limiter,
    )

    await pipeline.enqueue(_msg(text="超限但投影失败", message_id="rate-limit-projection"))

    stats = await store.stats()
    assert stats["last_appended_event_id"] == 1
    assert stats["last_projected_event_id"] == 0
    assert adapter.sent[0][0].scope == "private"
    assert adapter.sent[0][0].target_id == "123"
    assert "已超出速率限制" in adapter.sent[0][1]
    assert provider.calls == []
    assert pipeline.batch.is_empty_unsafe()
    assert pipeline._batch_task is None

    await pipeline.shutdown()
    assert not await store.shutdown(timeout=0.01)


@pytest.mark.asyncio
async def test_projection_failure_does_not_break_inbound_or_outbound_flow(tmp_path):
    store = ProjectionFailingEventStore(
        tmp_path / "events.sqlite3",
        projection_retry_delay=0.001,
    )
    pipeline, _provider, _adapter, _history = await _build_pipeline(
        tmp_path,
        event_store=store,
        merge_window_seconds=60.0,
    )

    await pipeline.enqueue(_msg(text="投影失败仍入站", message_id="projection-in"))
    pipeline._self_id_by_conversation["private:123"] = "999"
    await pipeline._record_successful_outbound(
        {
            "target_scope": "private",
            "target_id": "123",
            "content": "投影失败仍出站",
            "label": "投影失败仍出站",
            "kind": "text",
        },
        conversation_id="private:123",
        msg_id="projection-out",
    )

    stats = await store.stats()
    assert stats["last_appended_event_id"] == 2
    assert stats["last_projected_event_id"] == 0
    timeline = pipeline.chat_timeline.recent("private:123", 10)
    assert [item.msg_id for item in timeline] == ["projection-in", "projection-out"]

    await pipeline.shutdown()
    assert not await store.shutdown(timeout=0.01)
