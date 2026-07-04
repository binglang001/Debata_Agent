"""Wakeup/send-message/voice pipeline tests split from tests/test_integration_pipeline.py."""

from __future__ import annotations

from typing import Any

import pytest

from adapters.types import MediaSegment, MediaType
from core.state import PendingMessageItem
from providers.base import CompletionResult
from tests.integration_pipeline.helpers import (
    FailingQQSentEventStore,
    ProjectionFailingEventStore,
    _ai_no_action,
    _ai_send_private,
    _drain_pipeline,
    _msg,
)


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
