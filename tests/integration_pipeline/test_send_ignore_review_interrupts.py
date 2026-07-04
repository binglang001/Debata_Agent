"""Ignore-review-interrupt send integration pipeline tests."""

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
    _wait_until,
)


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
