"""Async send interrupt integration pipeline tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from adapters.types import Target
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
