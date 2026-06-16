"""Working-history and send-receipt integration tests."""

from __future__ import annotations

import json

import pytest

from tests.integration_pipeline.helpers import _ai_no_action, _drain_pipeline, _msg


@pytest.mark.asyncio
async def test_working_window_uses_unified_recent_timeline(build_pipeline):
    pipeline, provider, _, history, _ = await build_pipeline([_ai_no_action()])
    await history.add_user_message("群里的旧内容也属于统一人格时间线", conversation_id="group:1")
    await history.add_user_message("私聊旧内容应该保留", conversation_id="private:123")

    await pipeline.enqueue(_msg(user_id="123", text="继续私聊"))
    await _drain_pipeline(pipeline)

    joined = "\n".join(str(m.get("content", "")) for m in provider.calls[-1]["messages"])
    assert "私聊旧内容应该保留" in joined
    assert "群里的旧内容也属于统一人格时间线" in joined


@pytest.mark.asyncio
async def test_working_window_guarantees_current_conversation_recent_records(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.behavior_cfg.context.max_context_tokens = 14_000
    await history.add_user_message("私聊关键口令：KEEP-ME", conversation_id="private:123")
    for idx in range(30):
        await history.add_user_message(
            f"高频群聊消息 {idx} " + ("占位内容 " * 500),
            conversation_id="group:1",
        )

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "私聊关键口令：KEEP-ME" in joined
    assert "高频群聊消息 29" in joined


@pytest.mark.asyncio
async def test_working_history_without_conversation_uses_normal_budget(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(20):
        await history.add_user_message(
            f"全局消息 {idx} " + ("占位内容 " * 200),
            conversation_id=f"private:{idx}",
        )

    selected = await pipeline._select_working_history(None)
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "全局消息 19" in joined
    assert "全局消息 0" in joined


@pytest.mark.asyncio
async def test_working_history_starts_after_rolling_summary_active_start_index(
    build_pipeline,
):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(5):
        await history.add_user_message(
            f"活跃游标消息 {idx}",
            conversation_id="private:123",
        )
    await pipeline.rolling_summary.update(
        "已摘要前两条",
        archived_until={"legacy": "old"},
        active_start_index=2,
        updated_at="test",
    )

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(m.get("content", "")) for m in selected)

    assert "活跃游标消息 0" not in joined
    assert "活跃游标消息 1" not in joined
    assert "活跃游标消息 2" in joined
    assert "活跃游标消息 4" in joined


@pytest.mark.asyncio
async def test_working_history_budget_uses_context_budget_and_prompt_overhead(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    context = pipeline.behavior_cfg.context
    context.max_context_tokens = 20_000
    context.reserve_output_tokens = 2_000
    context.memory_token_budget = 3_000
    context.summary_token_budget = 4_000
    context.prompt_overhead_estimate_tokens = 5_000

    assert pipeline._working_history_budget() == 6_000

    context.prompt_overhead_estimate_tokens = 20_000

    assert pipeline._working_history_budget() == 1


@pytest.mark.asyncio
async def test_working_history_keeps_runtime_context_records_in_active_window(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_user_message("群聊真实旧消息仍属于统一时间线", conversation_id="group:1")
    await history.add_user_message("私聊真实旧消息仍应保留", conversation_id="private:123")
    runtime_records = []
    for idx in range(20):
        runtime_records.extend(
            [
                {
                    "role": "user",
                    "content": (
                        "<task_context priority=\"medium\">\n"
                        f"旧运行时上下文 {idx}\n"
                        "</task_context>"
                    ),
                    "metadata": {"kind": "task_context_snapshot"},
                    "conversation_id": "group:runtime",
                },
                {
                    "role": "user",
                    "content": (
                        "<send_status>\n"
                        f"旧清洁发送状态 {idx}\n"
                        "</send_status>"
                    ),
                    "metadata": {"kind": "send_done_snapshot"},
                    "conversation_id": "group:runtime",
                },
            ]
        )
    await history.add_records(runtime_records)
    await history.add_user_message("当前触发消息", conversation_id="private:123")

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(r.get("content", "")) for r in selected)

    assert "群聊真实旧消息仍属于统一时间线" in joined
    assert "私聊真实旧消息仍应保留" in joined
    assert "当前触发消息" in joined
    assert "旧运行时上下文 0" in joined
    assert "旧清洁发送状态 0" in joined
    assert "旧运行时上下文 19" in joined
    assert "旧清洁发送状态 19" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_runtime_noise_before_budget_cutoff(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    pipeline.behavior_cfg.context.max_context_tokens = 18_000
    await history.add_user_message("预算内应保留的真实旧聊天 KEEP-REAL", conversation_id="group:1")
    for idx in range(80):
        await history.add_records(
            [
                {
                    "role": "user",
                    "content": (
                        "<task_context priority=\"medium\">\n"
                        f"巨大旧运行时噪声 {idx} " + ("填充 " * 120) + "\n"
                        "</task_context>"
                    ),
                    "metadata": {"kind": "task_context_snapshot"},
                    "conversation_id": "group:noise",
                }
            ]
        )
    await history.add_user_message("当前触发消息", conversation_id="private:123")

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(r.get("content", "")) for r in selected)

    assert "预算内应保留的真实旧聊天 KEEP-REAL" in joined
    assert "巨大旧运行时噪声 0" in joined
    assert "当前触发消息" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_all_active_current_conversation_runtime(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(16):
        await history.add_records(
            [
                {
                    "role": "user",
                    "content": (
                        "<task_context priority=\"medium\">\n"
                        f"当前会话近期运行时 {idx}\n"
                        "</task_context>"
                    ),
                    "metadata": {"kind": "task_context_snapshot"},
                    "conversation_id": "private:123",
                }
            ]
        )

    selected = await pipeline._select_working_history("private:123")
    joined = "\n".join(str(r.get("content", "")) for r in selected)

    assert "当前会话近期运行时 0" in joined
    assert "当前会话近期运行时 15" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_recent_send_receipt_fields(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(16):
        legacy_receipt = {
            "interrupted": True,
            "send_id": f"legacy-{idx}",
            "sent": [{"content": f"已发-{idx}", "order": 1, "msg_id": f"s-{idx}"}],
            "unsent": [
                {
                    "content": f"旧 JSON 未发-{idx}",
                    "order": 2,
                    "send_id": f"legacy-{idx}",
                    "conversation_id": "group:5555",
                }
            ],
            "new_messages": [
                {
                    "conversation_id": "group:5555",
                    "user_id": "123",
                    "nickname": f"用户{idx}",
                    "text": f"旧 JSON 新消息 {idx}-1",
                    "seq": idx * 10 + 1,
                    "time": "10:00",
                    "msg_id": f"legacy-new-{idx}-1",
                },
                {
                    "conversation_id": "group:5555",
                    "user_id": "123",
                    "nickname": f"用户{idx}",
                    "text": f"旧 JSON 新消息 {idx}-2",
                    "seq": idx * 10 + 2,
                    "time": "10:01",
                    "msg_id": f"legacy-new-{idx}-2",
                },
            ],
            "recalled_messages": [
                {
                    "msg_id": f"legacy-recall-{idx}",
                    "conversation_id": "group:5555",
                    "note": "旧 JSON 撤回",
                }
            ],
            "errors": [{"order": 3, "error": "旧 JSON 错误"}],
            "accepted_messages": [{"content": "不应进入 prompt 的 accepted"}],
            "irrelevant_raw_payload": "不应进入 prompt 的无关字段",
        }
        await history.add_records(
            [
                {
                    "role": "user",
                    "content": (
                        "<send_receipt>\n"
                        "系统说明：运行时发送状态；按 JSON 字段判断。\n"
                        f"{json.dumps(legacy_receipt, ensure_ascii=False)}\n"
                        "</send_receipt>"
                    ),
                    "conversation_id": "group:5555",
                }
            ]
        )
    await history.add_records(
        [
            {
                "role": "user",
                "content": (
                    "<send_receipt>\n"
                    "发送回执：send-latest\n"
                    "会话：group:5555\n"
                    "状态：部分发送；发送期间被新消息打断（interrupted=true）。\n"
                    "未发送 1 条：\n"
                    "1. 未发；order=2；send_id=send-latest；conversation_id=group:5555\n"
                    "新消息 1 条：\n"
                    "- 用户（group:5555；user_id=123）1 条；样例：\"新消息\"；"
                    "最新 seq=8/time=10:00/msg_id=m-new\n"
                    "撤回消息 1 条：\n"
                    "1. msg_id=m1；conversation_id=group:5555；note=用户撤回\n"
                    "</send_receipt>"
                ),
                "conversation_id": "group:5555",
            }
        ]
    )

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(str(r.get("content", "")) for r in selected)
    raw_joined = "\n".join(str(r.get("content", "")) for r in await history.records())

    assert '"new_messages"' in joined
    assert "accepted_messages" in joined
    assert "irrelevant_raw_payload" in joined
    assert "不应进入 prompt 的 accepted" in joined
    assert "不应进入 prompt 的无关字段" in joined
    assert '"new_messages"' in raw_joined
    assert "accepted_messages" in raw_joined
    assert '"send_id": "legacy-15"' in joined
    assert "旧 JSON 未发-15" in joined
    assert "旧 JSON 新消息 15-1" in joined
    assert "legacy-recall-15" in joined
    assert "旧 JSON 错误" in joined
    assert "发送回执：send-latest" in joined
    assert "未发；order=2；send_id=send-latest" in joined
    assert "样例：\"新消息\"" in joined
    assert "msg_id=m1" in joined


@pytest.mark.asyncio
async def test_format_send_receipt_summarizes_spam_without_full_json(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    long_sent_content = "已发-" + "长" * 220
    receipt = {
        "type": "send_receipt",
        "send_id": "send-token",
        "conversation_id": "private:123",
        "interrupted": True,
        "sent": [
            {
                "content": long_sent_content if idx == 0 else f"已发-{idx}",
                "order": idx + 1,
                "msg_id": f"sent-{idx}",
                "conversation_id": "private:123",
            }
            for idx in range(3)
        ]
        + [
            {
                "content": "已发-隐藏",
                "order": 4,
                "msg_id": "sent-hidden",
                "conversation_id": "private:123",
            }
        ],
        "unsent": [
            {
                "content": f"未发-{idx}",
                "order": idx + 10,
                "send_id": "send-token",
                "conversation_id": "private:123",
            }
            for idx in range(5)
        ]
        + [
            {
                "content": "未发-隐藏",
                "order": 99,
                "send_id": "send-token",
                "conversation_id": "private:123",
            }
        ],
        "new_messages": [
            {
                "conversation_id": "private:123",
                "user_id": "123",
                "nickname": "用户",
                "text": f"刷屏 {idx}",
                "seq": idx + 1,
                "time": f"10:{idx:02d}",
                "msg_id": f"m-spam-{idx}",
                "priority_reasons": ["private_message"] if idx == 0 else [],
                "priority_reason": "focus_user" if idx == 1 else "",
            }
            for idx in range(20)
        ],
        "recalled_messages": [
            {
                "conversation_id": "private:123",
                "time": "10:30",
                "msg_id": "m-recall",
                "note": "用户撤回",
                "qq_visible": False,
            }
        ],
        "errors": ["order=9: boom", {"order": 10, "error": "timeout"}],
        "accepted_messages": [{"content": "不应重复出现的 accepted 内容"}],
    }

    summary = pipeline._format_send_receipt(receipt)

    assert "<send_receipt>" in summary
    assert "</send_receipt>" in summary
    assert '"new_messages"' not in summary
    assert '{"conversation_id"' not in summary
    assert summary.count("msg_id=m-spam-") == 1
    assert "新消息 20 条：" in summary
    assert "用户（private:123；user_id=123）20 条" in summary
    assert "样例：\"刷屏 0\"" in summary
    assert "最新 seq=20/time=10:19/msg_id=m-spam-19" in summary
    assert "priority_reasons=private_message,focus_user" in summary
    assert "已发送 4 条：" in summary
    expected_sent_content = f"{long_sent_content[:157]}..."
    assert (
        f"{expected_sent_content}；order=1；msg_id=sent-0；conversation_id=private:123"
        in summary
    )
    assert long_sent_content not in summary
    assert "已发-隐藏" not in summary
    assert "... 另有 1 条未列出。" in summary
    assert "未发送 6 条：" in summary
    assert "未发-4；order=14；send_id=send-token；conversation_id=private:123" in summary
    assert "未发-隐藏" not in summary
    assert "撤回消息 1 条：" in summary
    assert "msg_id=m-recall；conversation_id=private:123；time=10:30；note=用户撤回" in summary
    assert "错误 2 条：" in summary
    assert "order=9: boom" in summary
    assert "error=timeout；order=10" in summary
    assert "不应重复出现的 accepted 内容" not in summary


@pytest.mark.asyncio
async def test_format_send_receipt_limits_new_message_groups(build_pipeline):
    pipeline, _, _, _, _ = await build_pipeline([])
    receipt = {
        "type": "send_receipt",
        "send_id": "send-groups",
        "conversation_id": "group:0",
        "interrupted": True,
        "sent": [],
        "unsent": [],
        "new_messages": [
            {
                "conversation_id": f"group:{idx}",
                "user_id": str(idx),
                "nickname": f"用户{idx}",
                "text": f"消息 {idx}",
                "seq": idx + 1,
                "time": f"10:{idx:02d}",
                "msg_id": f"m-group-{idx}",
            }
            for idx in range(8)
        ],
    }

    summary = pipeline._format_send_receipt(receipt)

    assert "新消息 8 条：" in summary
    assert "用户0（group:0；user_id=0）1 条" in summary
    assert "用户5（group:5；user_id=5）1 条" in summary
    assert "用户6（group:6；user_id=6）1 条" not in summary
    assert "... 另有 2 组未列出。" in summary
    assert '"new_messages"' not in summary


@pytest.mark.asyncio
async def test_working_history_keeps_complete_no_action_pairs_in_active_window(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    for idx in range(14):
        await history.add_records(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"tc-na-{idx}",
                            "type": "function",
                            "function": {"name": "no_action", "arguments": "{}"},
                        }
                    ],
                    "conversation_id": "group:5555",
                },
                {
                    "role": "tool",
                    "tool_call_id": f"tc-na-{idx}",
                    "content": '{"ok": true, "no_action": true}',
                    "conversation_id": "group:5555",
                },
            ]
        )
    await history.add_user_message("真实聊天不能被 no_action 清理影响", conversation_id="group:5555")

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(json.dumps(r, ensure_ascii=False) for r in selected)

    assert "真实聊天不能被 no_action 清理影响" in joined
    assert "tc-na-0" in joined
    assert "tc-na-13" in joined


@pytest.mark.asyncio
async def test_working_history_keeps_incomplete_or_non_no_action_tool_pairs(build_pipeline):
    pipeline, _, _, history, _ = await build_pipeline([])
    await history.add_records(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-send",
                        "type": "function",
                        "function": {"name": "send_group_message", "arguments": "{}"},
                    }
                ],
                "conversation_id": "group:5555",
            },
            {
                "role": "tool",
                "tool_call_id": "tc-send",
                "content": '{"ok": true, "status": "accepted"}',
                "conversation_id": "group:5555",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc-na-incomplete",
                        "type": "function",
                        "function": {"name": "no_action", "arguments": "{}"},
                    }
                ],
                "conversation_id": "group:5555",
            },
        ]
    )
    for idx in range(14):
        await history.add_records(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"tc-old-na-{idx}",
                            "type": "function",
                            "function": {"name": "no_action", "arguments": "{}"},
                        }
                    ],
                    "conversation_id": "group:old",
                },
                {
                    "role": "tool",
                    "tool_call_id": f"tc-old-na-{idx}",
                    "content": '{"ok": true, "no_action": true}',
                    "conversation_id": "group:old",
                },
            ]
        )

    selected = await pipeline._select_working_history("group:5555")
    joined = "\n".join(json.dumps(r, ensure_ascii=False) for r in selected)

    assert "tc-send" in joined
    assert "accepted" in joined
    assert "tc-na-incomplete" in joined
    assert "tc-old-na-0" in joined

