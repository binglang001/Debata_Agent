"""Send tool integration pipeline tests."""

from __future__ import annotations

import json

import pytest

from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    _ai_no_action,
    _ai_send_private,
    _drain_pipeline,
    _msg,
    _wait_until,
)


@pytest.mark.asyncio

async def test_send_result_msg_id_can_be_recalled_same_turn(build_pipeline):

    """发送工具即时返回 msg_id，后续工具轮可立刻撤回刚发出的消息。"""

    send_args = {

        "targets": [{"target_qq": 123, "content": "这条会撤回", "order": 1, "delay": 0}],

    }

    recall_args = {"message_id": 1000}

    pipeline, _, adapter, _, _ = await build_pipeline(

        [

            CompletionResult(

                tool_calls=[

                    ToolCall(

                        id="tc-send",

                        name="send_private_messages",

                        arguments=json.dumps(send_args),

                    )

                ],

                finish_reason="tool_calls",

            ),

            CompletionResult(

                tool_calls=[

                    ToolCall(

                        id="tc-recall",

                        name="recall_message",

                        arguments=json.dumps(recall_args),

                    )

                ],

                finish_reason="tool_calls",

            ),

            _ai_no_action(),

        ]

    )

    await pipeline.enqueue(_msg(user_id="123", text="发完撤回"))

    await _drain_pipeline(pipeline)

    assert [content for _, content in adapter.sent] == ["这条会撤回"]

    assert adapter.recalled == ["1000"]

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

async def test_send_private_immediate_path_reaches_adapter(build_pipeline):

    """send_private_messages 即时发送路径必须把内容送到 adapter。"""

    pipeline, _, adapter, _, _ = await build_pipeline(

        [_ai_send_private(target_qq="456", content="键名一致性测试")]

    )

    await pipeline.enqueue(_msg(user_id="456", text="hi"))

    await _drain_pipeline(pipeline)

    assert any(c == "键名一致性测试" for _, c in adapter.sent)

@pytest.mark.asyncio

async def test_send_private_emoji_reaches_image_adapter_and_timeline(build_pipeline, tmp_path):

    emoji_dir = tmp_path / "emoji"

    emoji_dir.mkdir()

    emoji_path = emoji_dir / "无语.png"

    emoji_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    args = {

        "targets": [{"target_qq": 456, "emoji": "无语", "order": 1, "delay": 0}],

    }

    pipeline, _, adapter, _, _ = await build_pipeline(

        [

            CompletionResult(

                tool_calls=[

                    ToolCall(

                        id="tc-emoji",

                        name="send_private_messages",

                        arguments=json.dumps(args),

                    )

                ],

                finish_reason="tool_calls",

            )

        ],

        emoji_dir=emoji_dir,

    )

    await pipeline.enqueue(_msg(user_id="456", text="发个表情"))

    await _drain_pipeline(pipeline)

    assert adapter.sent == []

    assert adapter.image_sent[0][1]["image_path"] == emoji_path

    messages = pipeline.chat_timeline.recent("private:456", 10)

    markdown = pipeline.chat_timeline.to_markdown(messages)

    assert "我(999)：[表情包: 无语] [msg_id=1000]" in markdown

@pytest.mark.asyncio

async def test_chat_timeline_records_real_inbound_and_successful_outbound(build_pipeline):

    """真实 QQ 时间线只记录已进入处理的入站和 adapter 成功返回后的出站。"""

    pipeline, _, adapter, _, _ = await build_pipeline(

        [_ai_send_private(target_qq="456", content="真实回复")]

    )

    await pipeline.enqueue(_msg(user_id="456", text="真实入站", message_id="in-1"))

    await _drain_pipeline(pipeline)

    assert [content for _, content in adapter.sent] == ["真实回复"]

    messages = pipeline.chat_timeline.recent("private:456", 10)

    markdown = pipeline.chat_timeline.to_markdown(messages)

    assert "用户(456)：真实入站 [msg_id=in-1]" in markdown

    assert "我(999)：真实回复 [msg_id=1000]" in markdown

    ctx = pipeline._build_tool_context(conversation_id="private:456")

    executor = pipeline.tool_registry.get_executor(ctx)

    result = await executor("get_recent_chat_messages", {"limit": 10})

    assert result["ok"] is True

    assert result["status"] == "inline"

    assert "真实入站" in result["content"]

    assert "真实回复" in result["content"]

@pytest.mark.asyncio

async def test_send_private_with_delay_returns_accepted_pending(build_pipeline):

    """多条且存在正 delay 时，工具先返回 accepted，后台仍按原拆条发完。"""

    args = {

        "targets": [

            {"target_qq": 123, "content": "第一条", "order": 1, "delay": 0.05},

            {"target_qq": 123, "content": "第二条", "order": 2, "delay": 0.05},

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

            )

        ]

    )

    await pipeline.enqueue(_msg(user_id="123", text="连发测试"))

    await _drain_pipeline(pipeline, max_wait=2.0)

    assert [content for _, content in adapter.sent] == ["第一条", "第二条"]

    records = await history.records()

    tool_contents = [

        json.loads(r["content"])

        for r in records

        if r.get("role") == "tool" and r.get("tool_call_id") == "tc-send"

    ]

    assert tool_contents[-1]["status"] == "accepted"

    assert tool_contents[-1]["accepted"] is True

    assert tool_contents[-1]["delivery"] == "pending"

    assert tool_contents[-1]["qq_visible"] == "pending"

    assert tool_contents[-1]["accepted_messages"][0]["content"] == "第一条"

    assert tool_contents[-1]["data"]["conversation_ids"] == ["private:123"]

    assert tool_contents[-1]["data"]["message_count"] == 2

    assert tool_contents[-1]["result_format"] == "structured_json"

    assert isinstance(tool_contents[-1]["brief"], str)

    assert tool_contents[-1]["brief"].strip()

    assert any(

        r.get("role") == "user"

        and r.get("metadata", {}).get("kind") == "send_done_snapshot"

        and "发送完成（全部消息已发出）" in (r.get("content") or "")

        for r in records

    )

    assert len(provider.calls) == 2

@pytest.mark.asyncio

async def test_other_conversation_does_not_interrupt_async_send(build_pipeline):

    """A 会话后台发送时，B 会话入站只排自己的轮，不冲掉 A 的队列。"""

    args = {

        "targets": [

            {"target_qq": 123, "content": "A1", "order": 1, "delay": 0.1},

            {"target_qq": 123, "content": "A2", "order": 2, "delay": 0.1},

        ],

    }

    pipeline, provider, adapter, _, _ = await build_pipeline(

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

            CompletionResult(

                tool_calls=[ToolCall(id="tc-na", name="no_action", arguments="{}")],

                finish_reason="tool_calls",

            ),

        ]

    )

    await pipeline.enqueue(_msg(user_id="123", text="A开始", message_id="a1"))

    await _wait_until(lambda: len(adapter.sent) == 1, max_wait=1.0)

    await pipeline.enqueue(_msg(user_id="9", group_id="5555", text="B插话", message_id="b1"))

    await _drain_pipeline(pipeline, max_wait=3.0)

    assert [content for _, content in adapter.sent] == ["A1", "A2"]
    assert len(provider.calls) == 3
