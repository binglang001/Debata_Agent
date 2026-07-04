"""Send commit and idempotency integration pipeline tests."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from adapters.types import IncomingNotice, NoticeType
from core.recall_handler import RecallHandler
from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _drain_pipeline,
    _msg,
)


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
