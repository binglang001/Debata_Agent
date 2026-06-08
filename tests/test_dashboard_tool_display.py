from __future__ import annotations

import json

from ui.dashboard.tool_display import format_tool_call, format_tool_result


def test_tool_display_formats_commit_send_attempt_call_readably():
    display = format_tool_call(
        {
            "id": "call-1",
            "function": {
                "name": "commit_send_attempt",
                "arguments": json.dumps(
                    {
                        "send_attempt_id": "attempt-123",
                        "reviewed_until_seq": 36,
                        "ignore_review_interrupts": True,
                        "reason": "复核后确认旧回复仍适合当前对话",
                    },
                    ensure_ascii=False,
                ),
            },
        }
    )

    assert display.tool_name == "commit_send_attempt"
    assert display.title == "提交被打断的消息"
    assert "工具调用：提交发送尝试" in display.detail
    assert "ID 为 attempt-123" in display.detail
    assert "已阅读到编号 36" in display.detail
    assert "忽略打断" in display.detail
    assert "原因：复核后确认旧回复仍适合当前对话" in display.detail
    assert "send_attempt_id" not in display.detail
    assert "{" not in display.detail


def test_tool_display_formats_commit_send_attempt_result_with_grouped_interrupts():
    forced = [
        {
            "conversation_id": "private:430666862",
            "sender_name": "冰狼",
            "sender_id": "430666862",
            "text": f"打断 {i}",
        }
        for i in range(20)
    ]
    display = format_tool_result(
        json.dumps(
            {
                "status": "accepted",
                "send_id": "send-456",
                "send_attempt_id": "attempt-123",
                "delivery": "pending",
                "qq_visible": False,
                "ignored_review_interrupts": True,
                "forced_unseen_messages": forced,
            },
            ensure_ascii=False,
        ),
        tool_name="commit_send_attempt",
    )

    assert display.title == "工具返回"
    assert "工具返回：工具状态：已接受" in display.detail
    assert "发送 ID：send-456" in display.detail
    assert "打断消息 ID：attempt-123" in display.detail
    assert "正在投递" in display.detail
    assert "QQ 不可见" in display.detail
    assert "已忽略 20 条复核打断" in display.detail
    assert "其中 20 条来自 冰狼（私聊 430666862）" in display.detail
    assert "forced_unseen_messages" not in display.detail
    assert "打断 19" not in display.detail


def test_tool_display_formats_unknown_tool_call_without_raw_json():
    display = format_tool_call(
        {
            "function": {
                "name": "custom_tool",
                "arguments": json.dumps(
                    {
                        "keyword": "alpha",
                        "items": [{"id": 1}, {"id": 2}, {"id": 3}],
                        "options": {"mode": "fast", "limit": 5},
                    },
                    ensure_ascii=False,
                ),
            }
        }
    )

    assert display.title == "调用工具：custom_tool"
    assert "工具调用：custom_tool" in display.detail
    assert "keyword 为 alpha" in display.detail
    assert "items 为列表 3 项" in display.detail
    assert "options 为对象，包含 mode、limit" in display.detail
    assert "[{" not in display.detail


def test_tool_display_formats_unknown_tool_result_without_raw_json():
    display = format_tool_result(
        json.dumps(
            {
                "status": "done",
                "items": [{"id": 1}, {"id": 2}],
                "payload": {"ok": True, "count": 2},
            },
            ensure_ascii=False,
        )
    )

    assert display.title == "工具返回"
    assert "工具返回：状态 done" in display.detail
    assert "items 为列表 2 项" in display.detail
    assert "payload 为对象，包含 ok、count" in display.detail
    assert '"items"' not in display.detail
