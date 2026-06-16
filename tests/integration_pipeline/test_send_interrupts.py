"""Send interrupt and recall integration pipeline tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from adapters.types import IncomingNotice, NoticeType
from core.recall_handler import RecallHandler
from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    _ai_send_private,
    _drain_pipeline,
    _msg,
)


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
