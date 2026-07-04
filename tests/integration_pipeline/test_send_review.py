"""Send review integration pipeline tests."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _drain_pipeline,
    _msg,
)


@pytest.mark.asyncio

async def test_same_conversation_message_while_model_thinking_needs_review(build_pipeline):

    """LLM 思考时当前会话来了新消息，旧发送应 needs_review，并把新消息并入同一轮。"""

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

    assert '"status": "needs_review"' in joined

    tool_contents = [

        json.loads(r["content"])

        for r in records

        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"

    ]

    review_result = tool_contents[-1]

    assert review_result["status"] == "needs_review"

    assert review_result["qq_visible"] is False

    assert review_result["send_attempt_id"].startswith("attempt-")

    assert review_result["latest_seq"] == 2

    attempted = review_result["attempted_messages"][0]

    assert attempted["send_id"] == review_result["send_attempt_id"]

    assert attempted["conversation_id"] == "private:123"

    assert attempted["target_type"] == "private"

    assert attempted["target_id"] == "123"

    assert attempted["order"] == 1

    assert attempted["content"] == "旧回复"

    assert attempted["delay"] >= 0

    assert attempted["qq_visible"] is False

    assert review_result["unseen_messages"][0]["conversation_id"] == "private:123"

    assert review_result["unseen_messages"][0]["text"] == "我改口"

    assert review_result["unseen_messages"][0]["qq_visible"] is True

    assert review_result["priority_interrupts"][0]["priority_reasons"] == [

        "private_message",

        "focus_user",

    ]

    assert "note" not in review_result

    assert "commit_send_attempt" in review_result["next"]

    assert "m-new" in joined

    assert "<send_receipt>" in joined

    timeline_markdown = pipeline.chat_timeline.to_markdown(

        pipeline.chat_timeline.recent("private:123", 10)

    )

    assert "m-old" in timeline_markdown

    assert "m-new" in timeline_markdown

    assert "旧回复" not in timeline_markdown

@pytest.mark.asyncio

async def test_other_private_message_while_model_thinking_does_not_review_current_send(

    build_pipeline,

):

    """A 私聊思考时 B 私聊来消息，不应让 A 私聊发送 needs_review。"""

    started = asyncio.Event()

    release = asyncio.Event()

    send_args = {

        "targets": [{"target_qq": 123, "content": "A回复", "order": 1, "delay": 0}],

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

        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="A先问", message_id="a-old"))

    await started.wait()

    await pipeline.enqueue(_msg(user_id="456", text="B插话", message_id="b-new"))

    release.set()

    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["A回复"]

    records = await history.records()

    joined = "\n".join(str(r.get("content", "")) for r in records)

    assert "B插话" in joined

    assert '"status": "needs_review"' not in joined

    send_results = [

        json.loads(r["content"])

        for r in records

        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"

    ]

    assert send_results[-1]["status"] == "sent"

    assert call_count == 3

@pytest.mark.asyncio

async def test_unrelated_group_message_while_model_thinking_does_not_stale_send(build_pipeline):

    """模型思考时普通群聊插话不应让默认群短回应饿死。"""

    started = asyncio.Event()

    release = asyncio.Event()

    send_args = {

        "group_id": 5555,

        "targets": [{"content": "短回", "order": 1, "delay": 0}],

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

                        name="send_group_message",

                        arguments=json.dumps(send_args),

                    )

                ],

                finish_reason="tool_calls",

            )

        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))

    await started.wait()

    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="路过", message_id="m-new"))

    release.set()

    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["短回"]

    records = await history.records()

    joined = "\n".join(str(r.get("content", "")) for r in records)

    assert "路过" in joined

    assert '"status": "needs_review"' not in joined

    assert "<send_receipt>" not in joined

    assert call_count == 3

@pytest.mark.asyncio

async def test_group_review_all_requires_review_for_ordinary_unseen_message(build_pipeline):

    """review_all 下，模型思考期间普通群插话也会让发送先复核。"""

    started = asyncio.Event()

    release = asyncio.Event()

    send_args = {

        "group_id": 5555,

        "review_policy": "review_all",

        "targets": [{"content": "短回", "order": 1, "delay": 0}],

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

                        name="send_group_message",

                        arguments=json.dumps(send_args),

                    )

                ],

                finish_reason="tool_calls",

            )

        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))

    await started.wait()

    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="路过", message_id="m-new"))

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

    assert results[-1]["unseen_messages"][0]["text"] == "路过"

    assert results[-1]["priority_interrupts"] == []

@pytest.mark.asyncio

async def test_group_focus_user_followup_needs_review_before_send(build_pipeline):

    """focus 用户思考期间追问是确定性高优先级，发送前需复核。"""

    started = asyncio.Event()

    release = asyncio.Event()

    send_args = {

        "group_id": 5555,

        "targets": [{"content": "短回", "order": 1, "delay": 0}],

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

                        name="send_group_message",

                        arguments=json.dumps(send_args),

                    )

                ],

                finish_reason="tool_calls",

            )

        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))

    await started.wait()

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="等下", message_id="m-new"))

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

    assert results[-1]["priority_interrupts"][0]["priority_reasons"] == ["focus_user"]

@pytest.mark.asyncio

async def test_slash_hash_group_text_does_not_trigger_priority_review(build_pipeline):

    """群聊 /xxx、#xxx 普通文本不再天然视为高优先级打断。"""

    started = asyncio.Event()

    release = asyncio.Event()

    send_args = {

        "group_id": 5555,

        "targets": [{"content": "短回", "order": 1, "delay": 0}],

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

                        name="send_group_message",

                        arguments=json.dumps(send_args),

                    )

                ],

                finish_reason="tool_calls",

            )

        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))

    await started.wait()

    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="/other", message_id="m-slash"))

    await pipeline.enqueue(_msg(user_id="789", group_id="5555", text="#topic", message_id="m-hash"))

    release.set()

    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["短回"]

    records = await history.records()

    joined = "\n".join(str(r.get("content", "")) for r in records)

    assert '"status": "needs_review"' not in joined

    assert "command_message" not in joined

@pytest.mark.asyncio

async def test_atomic_delivery_policy_does_not_bypass_preflight_review(build_pipeline):

    """atomic 只影响接收后的投递中断，不能绕过发送前 focus 用户复核。"""

    started = asyncio.Event()

    release = asyncio.Event()

    send_args = {

        "group_id": 5555,

        "delivery_interrupt_policy": "atomic",

        "targets": [{"content": "短回", "order": 1, "delay": 0}],

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

                        name="send_group_message",

                        arguments=json.dumps(send_args),

                    )

                ],

                finish_reason="tool_calls",

            )

        return _ai_no_action()

    provider.chat_completion = blocking_chat_completion  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="先问", message_id="m-old"))

    await started.wait()

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="等下", message_id="m-new"))

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

    assert results[-1]["priority_interrupts"][0]["priority_reasons"] == ["focus_user"]

@pytest.mark.asyncio

async def test_needs_review_again_reuses_attempt_and_increments_revision(build_pipeline):

    """commit 前再次出现高优先级未见消息时，复用原 attempt 并递增 revision。"""

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

    assert adapter.sent == []

    records = await history.records()

    results = [

        json.loads(r["content"])

        for r in records

        if r.get("role") == "tool" and r.get("tool_call_id") in {"tc-send", "tc-commit"}

    ]

    initial = next(item for item in results if item["status"] == "needs_review")

    again = next(item for item in results if item["status"] == "needs_review_again")

    assert again["send_attempt_id"] == initial["send_attempt_id"]

    assert initial["attempt_revision"] == 1

    assert again["attempt_revision"] == 2

    assert again["revision"] == 2

    assert again["latest_seq"] == 3

    assert again["unseen_messages"][0]["text"] == "再补一句"
