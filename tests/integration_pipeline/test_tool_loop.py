"""Tool-loop pipeline tests split from tests/test_integration_pipeline.py."""

from __future__ import annotations

import json

import pytest

from providers.base import CompletionResult, ToolCall
from tests.integration_pipeline.helpers import (
    _drain_pipeline,
    _msg,
)


@pytest.mark.asyncio
async def test_multi_turn_tool_loop(build_pipeline):
    """非 no_action 工具默认把结果回填给模型，不再因 no_feedback 隐式结束。"""
    # 工具参数字段名是 memory_text 不是 content（被集成测试抓出来的）
    save_args = {"memory_text": "用户喜欢咖啡", "scope": "user:12345"}
    save_tc = ToolCall(id="tc-s", name="save_important_memory", arguments=json.dumps(save_args))
    pipeline, provider, adapter, _, important = await build_pipeline(
        [CompletionResult(tool_calls=[save_tc], finish_reason="tool_calls")]
    )

    await pipeline.enqueue(_msg(text="我喜欢咖啡"))
    await _drain_pipeline(pipeline)

    items = important.items()
    assert any("咖啡" in (i.get("content") or "") for i in items)


@pytest.mark.asyncio
async def test_max_loops_reached_no_crash(build_pipeline):
    """AgentRunner 对无工具纯文本只做纠正重试，不把文本兜底发送。"""
    # 脚本：连续返回纯文本（无工具调用）—— runner 应在一次纠正重试后 give up
    plain = CompletionResult(content="（纯文本）", finish_reason="stop")
    pipeline, provider, adapter, history, _ = await build_pipeline([plain, plain, plain])

    await pipeline.enqueue(_msg(text="测试无工具重试"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    # 不应发出消息（runner 拒绝接受纯文本输出）
    assert adapter.sent == []
    # provider 被调用 2 次：首次纯文本 + 一次纠正重试。
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_tool_loop_finalization_does_not_send_plain_text_to_qq(build_pipeline):
    """工具循环无工具收尾只能写内部记录，不能绕过发送工具发 QQ。"""
    save_args = {"memory_text": "循环中的中间结果"}
    save_tc_1 = ToolCall(id="tc-save-1", name="save_important_memory", arguments=json.dumps(save_args))
    save_tc_2 = ToolCall(id="tc-save-2", name="save_important_memory", arguments=json.dumps(save_args))
    pipeline, provider, adapter, history, _ = await build_pipeline(
        [
            CompletionResult(tool_calls=[save_tc_1], finish_reason="tool_calls"),
            CompletionResult(tool_calls=[save_tc_2], finish_reason="tool_calls"),
            CompletionResult(content="内部最终说明，不应发送到 QQ。", finish_reason="stop"),
        ]
    )
    pipeline.chat_agent.cfg.tool_loop_reminder_interval = 1
    pipeline.chat_agent.cfg.tool_loop_final_warning_count = 1
    pipeline.chat_agent.cfg.tool_loop_final_grace_loops = 1

    await pipeline.enqueue(_msg(text="测试工具循环最终收尾"))
    await _drain_pipeline(pipeline, max_wait=2.0)

    assert adapter.sent == []
    assert provider.calls[-1]["tools"] is None
    records = await history.records()
    assert any(
        record.get("role") == "assistant"
        and "内部最终说明" in str(record.get("content") or "")
        for record in records
    )


@pytest.mark.asyncio
async def test_unknown_scope_fails_gracefully(build_pipeline):
    """B17 防御：如果 collected 里的 scope 既不是 'private' 也不是 'group'，
    _do_send 应该写 system_note 而不是崩。

    这里手动塞一条非法 action 进 ctx.collected 进行验证。"""
    pipeline, _, adapter, history, _ = await build_pipeline([])

    # 直接调 _do_send 触发分支
    result = await pipeline._do_send(
        {"action": "weird_scope", "target": "1", "content": "hi", "label": "x", "delay": 0}
    )
    assert result is None
    assert adapter.sent == []

    records = await history.records()
    assert any(
        "未知 scope" in (r.get("content") or "") for r in records if r.get("role") == "system"
    ), f"应有 system_note 警告未知 scope；实际 records={records}"
