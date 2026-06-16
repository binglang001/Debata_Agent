"""Send review and interrupt integration pipeline tests."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from adapters.types import IncomingNotice, NoticeType, Target
from core.recall_handler import RecallHandler
from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _drain_pipeline,
    _msg,
    _wait_until,
)


@pytest.mark.asyncio
async def test_cross_conversation_clean_send_receipt_visible_in_unified_window(build_pipeline):
    """群里触发的私聊异步发送：accepted 在群轮，完成记录在私聊目标，但统一窗口都能看到。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "私聊第一条", "order": 1, "delay": 0.05},
            {"target_qq": 123, "content": "私聊第二条", "order": 2, "delay": 0.05},
        ],
    }
    pipeline, _, _, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-cross-send",
                        name="send_private_messages",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="群里触发私聊发送"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    records = await history.records()
    assert any(
        r.get("role") == "tool"
        and r.get("conversation_id") == "group:5555"
        and json.loads(r.get("content") or "{}").get("status") == "accepted"
        for r in records
    )
    assert any(
        r.get("role") == "user"
        and r.get("conversation_id") == "private:123"
        and r.get("metadata", {}).get("kind") == "send_done_snapshot"
        and "发送完成（全部消息已发出）" in (r.get("content") or "")
        for r in records
    )

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(str(r.get("content", "")) for r in selected)
    assert '"status": "accepted"' in joined
    assert "发送完成（全部消息已发出）" in joined


@pytest.mark.asyncio
async def test_same_conversation_interrupt_flushes_async_send_queue(build_pipeline):
    """同会话插话会打断后台发送，未发气泡进回执，不再正常批处理重复一轮。"""
    args = {
        "targets": [
            {"target_qq": 123, "content": "一", "order": 1, "delay": 0.2},
            {"target_qq": 123, "content": "二", "order": 2, "delay": 0.2},
            {"target_qq": 123, "content": "三", "order": 3, "delay": 0.2},
        ],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
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
            _ai_no_action("看到插话后先不补发"),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", text="开始连发", message_id="m-start"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="123", text="插话", message_id="m-interrupt"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["一"]
    assert len(provider.calls) == 3
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "m-interrupt" in joined
    assert "<send_receipt>" in joined
    assert "状态：部分发送；发送期间被新消息打断（interrupted=true）。" in joined
    assert "未发送 2 条：" in joined
    assert "二；order=2" in joined
    assert "三；order=3" in joined
    assert "新消息 1 条：" in joined
    assert "最新 seq=2/" in joined
    receipt_turn_context = "\n".join(
        str(m.get("content", ""))
        for m in provider.calls[-1]["messages"]
        if m.get("role") == "user" and "<send_receipt_task" in str(m.get("content", ""))
    )
    assert "<send_receipt>" in receipt_turn_context
    assert "interrupted=true" in receipt_turn_context
    assert "最新 seq=2/" in receipt_turn_context
    assert "seq=无" not in receipt_turn_context
    assert '"new_messages"' not in receipt_turn_context
    assert "按回执摘要判断" in receipt_turn_context
    assert "按 JSON 字段判断" not in receipt_turn_context


@pytest.mark.asyncio
async def test_group_priority_interrupt_allows_unrelated_async_chat(build_pipeline):
    """群聊默认 interrupt_priority：其他人普通插话不冲掉已排队的短回应。"""
    args = {
        "group_id": 5555,
        "targets": [
            {"content": "一", "order": 1, "delay": 0.2},
            {"content": "二", "order": 2, "delay": 0.2},
        ],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action("处理普通插话"),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="开始", message_id="m-start"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="456", group_id="5555", text="路过插话", message_id="m-other"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["一", "二"]
    assert not pipeline._send_manager.should_defer_batch("group:5555")
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "路过插话" in joined
    assert '"interrupted": true' not in joined
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_group_priority_interrupt_stops_same_trigger_user_followup(build_pipeline):
    """同触发用户追问是确定性高优先级事件，仍会阻断剩余发送。"""
    args = {
        "group_id": 5555,
        "targets": [
            {"content": "一", "order": 1, "delay": 0.2},
            {"content": "二", "order": 2, "delay": 0.2},
        ],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-send",
                        name="send_group_message",
                        arguments=json.dumps(args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action("看到追问后先不补发"),
        ]
    )

    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="开始", message_id="m-start"))
    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)
    await pipeline.enqueue(_msg(user_id="123", group_id="5555", text="等下", message_id="m-follow"))
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["一"]
    assert len(provider.calls) == 3
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "m-follow" in joined
    assert "<send_receipt>" in joined
    assert "interrupted=true" in joined
    assert "priority_reasons=same_trigger_user" in joined
    assert "二；order=2" in joined


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


@pytest.mark.asyncio
async def test_late_inbound_after_final_async_send_restarts_deferred_batch(build_pipeline):
    """最后一条异步发送期间来的新消息不能留下 sticky stale。"""
    second_send_entered = asyncio.Event()
    release_second_send = asyncio.Event()
    first_args = {
        "targets": [
            {"target_qq": 123, "content": "第一条", "order": 1, "delay": 0.01},
            {"target_qq": 123, "content": "第二条", "order": 2, "delay": 0.01},
        ],
    }
    second_args = {
        "targets": [{"target_qq": 123, "content": "新回复", "order": 1, "delay": 0}],
    }
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-first",
                        name="send_private_messages",
                        arguments=json.dumps(first_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
            _ai_no_action(),
            CompletionResult(
                tool_calls=[
                    ToolCall(
                        id="tc-second",
                        name="send_private_messages",
                        arguments=json.dumps(second_args),
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    original_send_text = adapter.send_text

    async def blocking_second_send(target: Target, content: str) -> str:
        msg_id = await original_send_text(target, content)
        if content == "第二条":
            second_send_entered.set()
            await release_second_send.wait()
        return msg_id

    adapter.send_text = blocking_second_send  # type: ignore[method-assign]

    await pipeline.enqueue(_msg(user_id="123", text="开始", message_id="m-start"))
    await asyncio.wait_for(second_send_entered.wait(), timeout=1.0)
    await pipeline.enqueue(_msg(user_id="123", text="补一句", message_id="m-late"))
    release_second_send.set()
    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["第一条", "第二条", "新回复"]
    assert len(provider.calls) == 4
    assert not pipeline._send_manager.should_defer_batch("private:123")
    records = await history.records()
    joined = "\n".join(str(r.get("content", "")) for r in records)
    assert "补一句" in joined

