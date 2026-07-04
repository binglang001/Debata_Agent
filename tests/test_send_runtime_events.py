from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from core.message_pipeline import MessagePipeline
from core.send_manager import _AsyncSendManager
from memory import EventStore


async def _wait_thread_event(event: threading.Event, timeout: float = 1.0) -> None:
    assert await asyncio.wait_for(
        asyncio.to_thread(event.wait, timeout),
        timeout=timeout + 0.2,
    )


class FakeAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.sent_texts: list[tuple[Any, str]] = []

    async def send_text(self, target: Any, content: str) -> str:
        self.sent_texts.append((target, content))
        return f"msg-{len(self.sent_texts)}"


class FakeTimeline:
    def recent(self, conversation_id: str, limit: int) -> list[Any]:
        return []


class FakePipeline:
    def __init__(self, event_store: Any) -> None:
        self.event_store = event_store
        self.adapter = FakeAdapter()
        self.persona = SimpleNamespace(name="Debata")
        self.chat_timeline = FakeTimeline()
        self.activity_count = 0
        self.outbound_records: list[dict[str, Any]] = []
        self.clean_receipts: list[dict[str, Any]] = []

    def mark_activity(self) -> None:
        self.activity_count += 1

    async def _record_successful_outbound(
        self,
        action: dict[str, Any],
        *,
        conversation_id: str,
        msg_id: str,
    ) -> None:
        self.outbound_records.append(
            {
                "action": action,
                "conversation_id": conversation_id,
                "msg_id": msg_id,
            }
        )

    async def _record_clean_send_receipt(self, receipt: dict[str, Any]) -> None:
        self.clean_receipts.append(receipt)

    def _schedule_send_receipt_turn(self, conversation_id: str) -> None:
        raise AssertionError("同步发送不应调度 receipt turn")

    def _schedule_deferred_batch(self, conversation_id: str) -> None:
        raise AssertionError("同步发送不应调度 deferred batch")


class FailingAppendLogEventStore:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append_event(self, **event: Any) -> int:
        self.events.append(event)
        raise RuntimeError("append log failed")


class FailingProjectionEventStore(EventStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, projection_retry_delay=0.01, **kwargs)
        self.fail_projection = True
        self.projection_failed = threading.Event()

    def _project_events_sync(self, events: Any) -> None:
        if self.fail_projection:
            self.projection_failed.set()
            raise RuntimeError("sqlite projection failed")
        return super()._project_events_sync(events)


class BackpressuredFailingProjectionEventStore(FailingProjectionEventStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, max_pending_events=1, **kwargs)
        self.append_event_calls = 0
        self.second_append_started = asyncio.Event()

    async def append_event(self, **event: Any) -> int:
        self.append_event_calls += 1
        if self.append_event_calls == 2:
            self.second_append_started.set()
        return await super().append_event(**event)


@pytest.mark.asyncio
async def test_send_runtime_events_record_order_without_full_content(tmp_path):
    event_store = EventStore(tmp_path / "events.sqlite3")
    pipeline = FakePipeline(event_store)
    manager = _AsyncSendManager(pipeline)  # type: ignore[arg-type]
    content = "要发送的正文-" + "密" * 120

    result = await manager.submit(
        [
            {
                "kind": "text",
                "target_scope": "private",
                "target_id": "123",
                "content": content,
                "order": 1,
                "delay": 0,
            }
        ],
        "send_private_messages",
    )

    assert result["status"] == "sent"
    assert pipeline.adapter.sent_texts[0][1] == content

    assert await event_store.wait_projected(timeout=1.0)
    events = await event_store.iter_events(limit=10)
    assert [event["event_type"] for event in events] == [
        "send_batch_accepted",
        "send_message_started",
        "send_message_succeeded",
        "send_receipt_recorded",
    ]
    payload_blob = json.dumps([event["payload"] for event in events], ensure_ascii=False)
    assert content not in payload_blob

    accepted = events[0]["payload"]
    assert accepted["status"] == "accepted"
    assert accepted["send_id"] == result["send_id"]
    assert accepted["counts"]["messages"] == 1
    assert accepted["conversation_ids"] == ["private:123"]

    started = events[1]["payload"]
    succeeded = events[2]["payload"]
    assert started["content_length"] == len(content)
    assert len(started["content_hash"]) == 64
    assert "msg_id" not in started
    assert succeeded["msg_id"] == "msg-1"
    assert succeeded["content_hash"] == started["content_hash"]

    receipt = events[3]["payload"]
    assert receipt["send_id"] == result["send_id"]
    assert receipt["status"] == "succeeded"
    assert receipt["counts"]["sent"] == 1
    assert receipt["counts"]["new_messages"] == 0
    await event_store.shutdown()


@pytest.mark.asyncio
async def test_send_manager_shutdown_flushes_delayed_worker_runtime_events(tmp_path):
    event_store = EventStore(tmp_path / "events.sqlite3")
    pipeline = FakePipeline(event_store)
    manager = _AsyncSendManager(pipeline)  # type: ignore[arg-type]

    result = await manager.submit(
        [
            {
                "kind": "text",
                "target_scope": "private",
                "target_id": "123",
                "content": "第一条",
                "order": 1,
                "delay": 0.02,
            },
            {
                "kind": "text",
                "target_scope": "private",
                "target_id": "123",
                "content": "第二条",
                "order": 2,
                "delay": 0,
            },
        ],
        "send_private_messages",
    )

    assert result["status"] == "accepted"
    await manager.shutdown(timeout=1.0)

    assert [content for _, content in pipeline.adapter.sent_texts] == [
        "第一条",
        "第二条",
    ]
    assert await event_store.wait_projected(timeout=1.0)
    events = await event_store.iter_events(limit=20)
    event_types = [event["event_type"] for event in events]
    assert event_types.count("send_message_succeeded") == 2
    assert "send_receipt_recorded" in event_types

    succeeded = [
        event["payload"]
        for event in events
        if event["event_type"] == "send_message_succeeded"
    ]
    assert [payload["msg_id"] for payload in succeeded] == ["msg-1", "msg-2"]

    receipt = [
        event["payload"]
        for event in events
        if event["event_type"] == "send_receipt_recorded"
    ][-1]
    assert receipt["send_id"] == result["send_id"]
    assert receipt["status"] == "succeeded"
    assert receipt["counts"]["sent"] == 2
    assert pipeline.clean_receipts[-1]["send_id"] == result["send_id"]
    await event_store.shutdown()


@pytest.mark.asyncio
async def test_message_pipeline_shutdown_waits_send_receipt_tasks():
    class RecordingSendManager:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        async def shutdown(self, timeout: float = 5.0) -> None:
            self.shutdown_calls += 1

    receipt_done = False

    async def receipt_task() -> None:
        nonlocal receipt_done
        await asyncio.sleep(0.01)
        receipt_done = True

    pipeline = object.__new__(MessagePipeline)
    manager = RecordingSendManager()
    pipeline._send_manager = manager
    pipeline._send_receipt_tasks = {"private:123": asyncio.create_task(receipt_task())}
    pipeline._batch_task = None
    pipeline._requeue_task = None
    pipeline._summary_task = None

    await MessagePipeline.shutdown(pipeline)

    assert manager.shutdown_calls == 2
    assert receipt_done is True
    assert pipeline._send_receipt_tasks["private:123"].done()


@pytest.mark.asyncio
async def test_send_runtime_event_append_log_failure_stops_send():
    event_store = FailingAppendLogEventStore()
    pipeline = FakePipeline(event_store)
    manager = _AsyncSendManager(pipeline)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="append log failed"):
        await manager.submit(
            [
                {
                    "kind": "text",
                    "target_scope": "private",
                    "target_id": "123",
                    "content": "事件未持久化不应继续发送",
                    "order": 1,
                    "delay": 0,
                }
            ],
            "send_private_messages",
        )

    assert pipeline.adapter.sent_texts == []
    assert [event["event_type"] for event in event_store.events] == [
        "send_batch_accepted",
    ]


@pytest.mark.asyncio
async def test_send_runtime_event_projection_failure_does_not_affect_send(tmp_path):
    event_store = FailingProjectionEventStore(tmp_path / "events.sqlite3")
    pipeline = FakePipeline(event_store)
    manager = _AsyncSendManager(pipeline)  # type: ignore[arg-type]

    try:
        result = await manager.submit(
            [
                {
                    "kind": "text",
                    "target_scope": "private",
                    "target_id": "123",
                    "content": "事件库失败也要发送",
                    "order": 1,
                    "delay": 0,
                }
            ],
            "send_private_messages",
        )

        await _wait_thread_event(event_store.projection_failed)

        assert result["status"] == "sent"
        assert pipeline.adapter.sent_texts[0][1] == "事件库失败也要发送"
        stats = await event_store.stats()
        assert stats["last_appended_event_id"] == 4
        assert stats["last_projected_event_id"] == 0
        assert stats["projection_error_count"] >= 1
        assert not await event_store.wait_projected(timeout=0.01)
    finally:
        await event_store.shutdown(timeout=0.01)


@pytest.mark.asyncio
async def test_send_runtime_event_projection_lag_backpressures_without_dropping(tmp_path):
    event_store = BackpressuredFailingProjectionEventStore(tmp_path / "events.sqlite3")
    pipeline = FakePipeline(event_store)
    manager = _AsyncSendManager(pipeline)  # type: ignore[arg-type]
    submit_task = asyncio.create_task(
        manager.submit(
            [
                {
                    "kind": "text",
                    "target_scope": "private",
                    "target_id": "123",
                    "content": "投影长期失败时不能静默丢事件",
                    "order": 1,
                    "delay": 0,
                }
            ],
            "send_private_messages",
        )
    )

    try:
        await _wait_thread_event(event_store.projection_failed)
        await asyncio.wait_for(event_store.second_append_started.wait(), timeout=1.0)

        assert not submit_task.done()
        assert pipeline.adapter.sent_texts == []
        stats = await event_store.stats()
        assert stats["last_appended_event_id"] == 1
        assert stats["last_projected_event_id"] == 0
        assert stats["pending_count"] == 1
        records = [
            json.loads(line)
            for line in event_store.append_log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [record["event_type"] for record in records] == ["send_batch_accepted"]

        event_store.fail_projection = False
        result = await asyncio.wait_for(submit_task, timeout=1.0)

        assert result["status"] == "sent"
        assert pipeline.adapter.sent_texts[0][1] == "投影长期失败时不能静默丢事件"
        assert await event_store.wait_projected(timeout=1.0)
        events = await event_store.iter_events(limit=10)
        assert [event["event_type"] for event in events] == [
            "send_batch_accepted",
            "send_message_started",
            "send_message_succeeded",
            "send_receipt_recorded",
        ]
        assert [event["event_id"] for event in events] == [1, 2, 3, 4]
    finally:
        event_store.fail_projection = False
        if not submit_task.done():
            submit_task.cancel()
            await asyncio.gather(submit_task, return_exceptions=True)
        await event_store.shutdown(timeout=0.1)
