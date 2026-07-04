"""聊天页消息渲染和气泡样式回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
Qt = QtCore.Qt

from core.chat_timeline import ChatTimelineStore
from ui.dashboard.chats_page import (
    ChatsPage,
    _chat_html_style,
    _format_send_receipt_summary,
    _format_tool_call_for_display,
    _group_records_by_conversation,
    _load_chat_page_records,
    _render_record_bubbles,
    _render_record_html,
)
from ui.theme import palette_for_theme

from tests.test_dashboard_p2 import (
    _FakeEventStore,
    _StaticRecordStore,
    _dashboard_runtime,
    _runtime_event,
    _timeline_record,
)


def test_chats_formats_send_tool_call_readably():
    text = _format_tool_call_for_display(
        {
            "function": {
                "name": "send_group_message",
                "arguments": (
                    '{"group_id":1039163467,"targets":['
                    '{"content":"好好好 不说了","order":1,"delay":0.5},'
                    '{"content":"那我先待机","order":2,"delay":0.6}]}'
                ),
            }
        }
    )

    assert text == "在群 1039163467 发送消息：好好好 不说了（0.5s）；那我先待机（0.6s）"


def test_chats_render_record_uses_speaker_names_not_ambiguous_pronouns():
    user_html = _render_record_html(
        {
            "role": "user",
            "content": "【2026-05-27 12:00:00 群聊 20002 Bob(30003) msg_id=9】群消息",
        },
        persona_name="玖",
    )
    assistant_html = _render_record_html(
        {"role": "assistant", "content": "收到", "direction": "outbound", "qq_visible": True},
        persona_name="玖",
    )
    internal_html = _render_record_html(
        {"role": "assistant", "content": "内部草稿"},
        persona_name="玖",
    )

    assert "Bob(30003)" in user_html
    assert "群消息" in user_html
    assert "玖" in assistant_html
    assert "chat-record chat-message-table chat-side-right chat-peer" in user_html
    assert "chat-record chat-message-table chat-side-left chat-bot" in assistant_html
    assert "class='chat-message-cell' width='78%' align='right'" in user_html
    assert "class='chat-message-cell' width='78%' align='left'" in assistant_html
    assert "class='chat-bubble-frame' align='right'" in user_html
    assert "class='chat-bubble-frame' align='left'" in assistant_html
    assert "border-radius:" in user_html
    assert "data-rounded='qt-inline-radius'" in user_html
    assert "class='chat-name-line' align='right'>Bob(30003)" in user_html
    assert "class='chat-name-line' align='left'>玖" in assistant_html
    assert user_html.index("class='chat-spacer-cell'") < user_html.index("class='chat-message-cell'")
    assert assistant_html.index("class='chat-message-cell'") < assistant_html.index("class='chat-spacer-cell'")
    assert user_html.index("class='chat-name-line'") < user_html.index("class='chat-bubble'")
    assert assistant_html.index("class='chat-name-line'") < assistant_html.index("class='chat-bubble'")
    assert "chat-bubble" in user_html
    assert "chat-bubble" in assistant_html
    assert "chat-avatar" not in user_html + assistant_html
    assert "玖 · 内部文本" in internal_html
    assert "chat-bubble" not in internal_html
    assert ">你<" not in user_html + assistant_html
    assert ">她<" not in user_html + assistant_html


def test_chats_real_outbound_history_record_renders_as_left_bubble():
    html = _render_record_html(
        {
            "role": "user",
            "direction": "outbound",
            "content": "真实发出的归档消息",
            "conversation_id": "private:10001",
            "qq_visible": True,
            "msg_id": "200",
        },
        persona_name="玖",
    )

    assert "chat-record chat-message-table chat-side-left chat-bot" in html
    assert "真实发出的归档消息" in html
    assert "已发送 · msg_id=200" in html
    assert "chat-event" not in html


def test_chats_runtime_and_tool_results_are_readable_and_collapsed():
    receipt = (
        "<send_receipt>\n"
        "系统说明：运行时发送状态。\n"
        '{"status":"stale","sent":[],"attempted_messages":[{"content":"嗯"}],'
        '"new_messages":[{"text":"新消息"}],"note":"模型思考期间当前会话来了新消息"}'
        "\n</send_receipt>"
    )
    tool_html = _render_record_html(
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"ok":false,"status":"stale","attempted_messages":[{}],"new_visible_messages":[{}]}',
        },
        persona_name="玖",
    )

    assert _format_send_receipt_summary(receipt) == (
        "状态 stale；待发送/尝试 1 条；新消息 1 条；模型思考期间当前会话来了新消息"
    )
    assert "工具返回" in tool_html
    assert "点击展开" in tool_html
    assert "chat-event chat-event-tool" in tool_html
    assert "chat-bubble" not in tool_html
    assert "状态 stale" not in tool_html


def test_chats_send_receipt_text_summary_is_readable():
    receipt = (
        "<send_receipt>\n"
        "发送回执：send-1\n"
        "会话：private:10001\n"
        "状态：已完成（interrupted=false）。\n"
        "已发送 1 条：\n"
        "1. 收到；order=1；msg_id=900；conversation_id=private:10001\n"
        "未发送 0 条：\n"
        "- 无\n"
        "新消息 2 条：\n"
        "1. conversation_id=private:10001；user_id=10001；nickname=用户；count=2\n"
        "撤回消息 0 条：\n"
        "- 无\n"
        "错误 0 条：\n"
        "- 无\n"
        "处理要求：不要重发已发送内容。\n"
        "</send_receipt>"
    )

    summary = _format_send_receipt_summary(receipt)

    assert "<send_receipt>" not in summary
    assert summary == "状态 已完成（interrupted=false）；已发送 1 条；新消息 2 条"


def test_chats_send_receipt_renders_only_visible_sent_as_outbound_bubble():
    receipt_html = _render_record_html(
        {
            "role": "user",
            "content": (
                "<send_receipt>\n"
                '{"sent":[{"content":"真正发出","msg_id":"100","qq_visible":true}],'
                '"attempted_messages":[{"content":"未确认草稿"}],'
                '"unsent":[{"content":"未发"}]}\n'
                "</send_receipt>"
            ),
        },
        persona_name="玖",
    )

    assert "系统消息" in receipt_html
    assert "点击展开" in receipt_html
    assert "玖" in receipt_html
    assert "真正发出" in receipt_html
    assert "已发送 · msg_id=100" in receipt_html
    assistant_bubble = receipt_html.split("chat-record chat-message-table chat-side-left chat-bot")[-1]
    assert "未确认草稿" not in assistant_bubble
    assert "未发" not in assistant_bubble


def test_chats_text_send_receipt_sent_promotes_to_left_bubble_once(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "private:10001",
        "label": "私聊 10001",
        "records": [
            {
                "role": "user",
                "conversation_id": "private:10001",
                "content": (
                    "<send_receipt>\n"
                    "发送回执：send-r\n"
                    "会话：private:10001\n"
                    "状态：已完成（interrupted=false）。\n"
                    "已发送 1 条：\n"
                    "1. 收到；order=1；msg_id=900；conversation_id=private:10001；time=2026-06-08 19:43:55\n"
                    "未发送 0 条：\n"
                    "- 无\n"
                    "新消息 0 条：\n"
                    "- 无\n"
                    "撤回消息 0 条：\n"
                    "- 无\n"
                    "错误 0 条：\n"
                    "- 无\n"
                    "处理要求：不要重发已发送内容。\n"
                    "</send_receipt>"
                ),
            }
        ],
    }

    html = page._render_conversation(conv)
    body_html = html.split("</style>", 1)[1]

    assert "chat-record chat-message-table chat-side-left chat-bot" in body_html
    assert "收到" in body_html
    assert "已发送 · msg_id=900" in body_html
    assert body_html.count("收到") == 1
    event_html = body_html.split("chat-record chat-message-table chat-side-left chat-bot", 1)[0]
    assert "收到" not in event_html


def test_chats_send_receipt_sent_promotes_to_left_bubble_without_flat_event_text(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "private:10001",
        "label": "私聊 10001",
        "records": [
            {
                "role": "user",
                "conversation_id": "private:10001",
                "content": (
                    "<send_receipt>\n"
                    '{"send_id":"send-r","sent":[{"content":"收到","msg_id":"900",'
                    '"time":"2026-06-08 19:43:55","qq_visible":true}]}\n'
                    "</send_receipt>"
                ),
            }
        ],
    }

    html = page._render_conversation(conv)
    body_html = html.split("</style>", 1)[1]

    assert "chat-record chat-message-table chat-side-left chat-bot" in body_html
    assert "收到" in body_html
    assert "已发送 · msg_id=900" in body_html
    assert body_html.count("收到") == 1
    event_html = body_html.split("chat-record chat-message-table chat-side-left chat-bot", 1)[0]
    assert "收到" not in event_html


def test_chats_stale_attempted_receipt_does_not_render_outbound_bubble():
    receipt_html = _render_record_html(
        {
            "role": "user",
            "content": (
                "<send_receipt>\n"
                '{"status":"stale","attempted_messages":[{"content":"不要当成已发送"}],'
                '"new_messages":[{"text":"新消息"}]}\n'
                "</send_receipt>"
            ),
        },
        persona_name="玖",
    )

    assert "系统消息" in receipt_html
    assert "点击展开" in receipt_html
    assert "待发送/尝试 1 条" not in receipt_html
    assert "不要当成已发送" not in receipt_html
    assert "chat-message-table chat-side-left chat-bot" not in receipt_html


def test_chats_runtime_and_system_records_are_events_not_bubbles():
    runtime_html = _render_record_html(
        {
            "role": "user",
            "content": (
                "<task_context priority=\"medium\">\n"
                "现在是2026-06-07 01:20:21。\n"
                "当前会话：group:497686077。\n"
                "<recent_group_messages></recent_group_messages>\n"
                "</task_context>"
            ),
        },
        persona_name="玖",
    )
    system_html = _render_record_html(
        {"role": "system", "content": "社交决策：本次跳过"},
        persona_name="玖",
    )
    send_status_html = _render_record_html(
        {
            "role": "user",
            "content": (
                "<send_status>\n"
                "系统说明：以下内容由运行时系统提供，不是用户新发言。\n"
                "2026-06-08 10:00:00 发送完成 send_id=send-1 msg_ids=[100]\n"
                "</send_status>"
            ),
        },
        persona_name="玖",
    )

    assert "系统消息" in runtime_html
    assert "点击展开" in runtime_html
    assert "chat-event chat-event-system" in runtime_html
    assert "chat-bubble" not in runtime_html
    assert "系统" in system_html
    assert "chat-event chat-event-system" in system_html
    assert "chat-bubble" not in system_html
    assert "系统消息" in send_status_html
    assert "send_id=send-1" not in send_status_html
    assert "chat-bubble" not in send_status_html


def test_chats_event_records_do_not_use_qq_message_tables():
    tool_html = _render_record_html(
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"status":"accepted"}',
        },
        persona_name="玖",
    )
    system_html = _render_record_html(
        {"role": "system", "content": "社交决策：本次跳过"},
        persona_name="玖",
    )
    reasoning_html = _render_record_html(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "内部推理",
            "conversation_id": "group:1",
        },
        persona_name="玖",
    )

    for html in (tool_html, system_html):
        assert "chat-event" in html
        assert "chat-message-table" not in html
        assert "chat-side-left" not in html
        assert "chat-side-right" not in html
        assert "chat-bubble" not in html
    assert "chat-record chat-message-table chat-side-left chat-bot" in reasoning_html
    assert "chat-bubble" in reasoning_html
    assert "chat-event" not in reasoning_html


def test_chats_renders_assistant_tool_calls_as_separate_event():
    bubbles = _render_record_bubbles(
        {
            "role": "assistant",
            "content": "准备发",
            "tool_calls": [
                {
                    "function": {
                        "name": "send_private_messages",
                        "arguments": '{"targets":[{"target_qq":123,"content":"你好","order":1,"delay":0}]}',
                    }
                }
            ],
        },
        persona_name="玖",
    )

    assert len(bubbles) == 2
    assert "内部文本" in bubbles[0]
    assert "点击展开" in bubbles[0]
    assert "准备发" not in bubbles[0]
    assert "chat-event" in bubbles[0]
    assert "工具调用" not in bubbles[0]
    assert "chat-event-tool" in bubbles[1]
    assert "chat-bubble" not in bubbles[1]
    assert "玖 · 发送私聊消息" in bubbles[1]
    assert "向 123 发送消息：你好" not in bubbles[1]


def test_chats_formats_commit_send_attempt_call_without_raw_json():
    text = _format_tool_call_for_display(
        {
            "function": {
                "name": "commit_send_attempt",
                "arguments": (
                    '{"send_attempt_id":"attempt-1","reviewed_until_seq":8,'
                    '"delivery_interrupt_policy":"interrupt_priority",'
                    '"ignore_review_interrupts":true,'
                    '"reason":"复核后确认旧回复仍适合"}'
                ),
            }
        }
    )

    assert text == (
        "提交发送尝试：ID attempt-1；已阅读到编号 8；"
        "忽略打断；原因：复核后确认旧回复仍适合"
    )
    assert "{" not in text
    assert "ignore_review_interrupts" not in text


def test_chats_tool_call_default_hides_raw_arguments():
    html = _render_record_html(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-commit",
                    "function": {
                        "name": "commit_send_attempt",
                        "arguments": (
                            '{"send_attempt_id":"attempt-1","reviewed_until_seq":8,'
                            '"delivery_interrupt_policy":"interrupt_priority",'
                            '"ignore_review_interrupts":true,'
                            '"reason":"复核后确认旧回复仍适合"}'
                        ),
                    },
                }
            ],
        },
        persona_name="玖",
    )

    assert "玖 · 提交被打断的消息" in html
    assert "点击展开" in html
    assert "提交发送尝试：send_attempt_id=attempt-1" not in html
    assert '"ignore_review_interrupts"' not in html
    assert "chat-bubble" not in html


def test_chats_tool_result_summarizes_long_unseen_lists_by_default(qapp, tmp_paths):
    forced = [
        {
            "time": f"2026-06-08 10:{i:02d}:00",
            "conversation_id": "group:1",
            "sender_name": "Alice",
            "text": f"新消息 {i}",
        }
        for i in range(11)
    ]
    content = {
        "ok": True,
        "status": "accepted",
        "send_id": "send-1",
        "send_attempt_id": "attempt-1",
        "sent": [{"content": "第一条"}, {"content": "第二条"}],
        "ignored_review_interrupts": True,
        "forced_unseen_messages": forced,
    }
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {
                "role": "tool",
                "tool_call_id": "call-commit",
                "content": json.dumps(content, ensure_ascii=False),
                "conversation_id": "group:1",
            }
        ],
    }

    html = page._render_conversation(conv)

    assert "工具返回" in html
    assert "点击展开" in html
    assert "send_id=send-1" not in html
    assert "已忽略 11 条复核打断" not in html
    assert "forced_unseen_messages" not in html
    assert "新消息 10" not in html

    page._expanded_item_ids.add("group:1\ncall-commit:tool_result")
    expanded = page._render_conversation(conv)

    assert "收起" in expanded
    assert "工具状态：已接受" in expanded
    assert "发送 ID：send-1" in expanded
    assert "打断消息 ID：attempt-1" in expanded
    assert "已忽略 11 条复核打断" in expanded
    assert "其中 11 条来自 Alice（群聊 1）" in expanded
    assert "forced_unseen_messages" not in expanded
    assert "新消息 10" not in expanded


def test_chats_single_message_expand_is_per_item(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {
                "id": "msg-1",
                "role": "user",
                "content": '{"payload":[' + ",".join('"短预览"' for _ in range(20)) + '],"secret":"raw-only"}',
                "conversation_id": "group:1",
            },
            {
                "id": "msg-2",
                "role": "user",
                "content": '{"payload":[' + ",".join('"另一个"' for _ in range(20)) + '],"secret":"still-hidden"}',
                "conversation_id": "group:1",
            },
        ],
    }

    html = page._render_conversation(conv)

    assert "展开原文" in html
    assert "raw-only" not in html
    assert "still-hidden" not in html

    page._conversations = [conv]
    page._current_key = "group:1"
    page._list.addItem("群聊 1")
    page._list.item(0).setData(Qt.ItemDataRole.UserRole, "group:1")
    page._list.setCurrentRow(0)
    page._on_detail_anchor_clicked(QtCore.QUrl("Debata-chat-toggle:group%3A1%0Amsg-1%3Ainbound"))
    expanded = page._render_conversation(conv)

    assert "收起" in expanded
    assert "raw-only" in expanded
    assert "still-hidden" not in expanded


def test_chats_expand_state_is_scoped_by_conversation(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    private_conv = {
        "key": "private:1",
        "label": "私聊 1",
        "records": [
            {
                "id": "same-msg",
                "role": "user",
                "content": '{"payload":[' + ",".join('"私聊预览"' for _ in range(20)) + '],"secret":"private-raw"}',
                "conversation_id": "private:1",
            }
        ],
    }
    group_conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {
                "id": "same-msg",
                "role": "user",
                "content": '{"payload":[' + ",".join('"群聊预览"' for _ in range(20)) + '],"secret":"group-raw"}',
                "conversation_id": "group:1",
            }
        ],
    }

    page._expanded_item_ids.add("private:1\nsame-msg:inbound")

    private_html = page._render_conversation(private_conv)
    group_html = page._render_conversation(group_conv)

    assert "private-raw" in private_html
    assert "收起" in private_html
    assert "group-raw" not in group_html
    assert "展开原文" in group_html


def test_chats_render_conversation_separates_messages_without_hr(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {"role": "user", "content": "你好", "conversation_id": "group:1"},
            {
                "role": "assistant",
                "content": "收到",
                "direction": "outbound",
                "qq_visible": True,
                "conversation_id": "group:1",
            },
        ],
    }

    html = page._render_conversation(conv)

    assert html.count("class='chat-record chat-message-table") == 2
    assert "<hr" not in html


@pytest.mark.asyncio
async def test_chats_real_messages_sort_by_timeline_qq_time_not_tool_order(qapp, tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    timeline = ChatTimelineStore()
    for message in [
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="刷屏 23:07:18",
            msg_id="in-1",
            timestamp=1_780_000_018.0,
            time_text="2026-06-08 23:07:18",
        ),
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="行",
            msg_id="out-1",
            timestamp=1_780_000_019.0,
            time_text="2026-06-08 23:07:19",
            sender_name="我",
            sender_id="999",
        ),
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="刷屏 23:07:20",
            msg_id="in-2",
            timestamp=1_780_000_020.0,
            time_text="2026-06-08 23:07:20",
        ),
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="收到",
            msg_id="out-2",
            timestamp=1_780_000_045.0,
            time_text="2026-06-08 23:07:45",
            sender_name="我",
            sender_id="999",
        ),
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="试了",
            msg_id="out-3",
            timestamp=1_780_000_074.0,
            time_text="2026-06-08 23:08:14",
            sender_name="我",
            sender_id="999",
        ),
    ]:
        timeline.append(message)
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.history = _StaticRecordStore(
        [
            {
                "role": "assistant",
                "content": "",
                "conversation_id": "private:10001",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "send_private_messages",
                            "arguments": '{"targets":[{"target_qq":"10001","content":"行","order":1,"delay":0}]}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "conversation_id": "private:10001",
                "content": '{"status":"accepted"}',
            },
        ]
    )
    page = ChatsPage(rt)

    records = await _load_chat_page_records(rt)
    conv = next(item for item in _group_records_by_conversation(records) if item["key"] == "private:10001")
    html = page._render_conversation(conv)

    assert html.index("刷屏 23:07:18") < html.index(">行<")
    assert html.index(">行<") < html.index("刷屏 23:07:20")
    assert html.index("刷屏 23:07:20") < html.index(">收到<")
    assert html.index(">收到<") < html.index(">试了<")
    assert html.index(">试了<") < html.index("Debata · 发送私聊消息")


@pytest.mark.asyncio
async def test_chats_renders_history_reasoning_after_event_store_tool_dedupe(qapp, tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-render",
                    "args": {"path": "event-render.md"},
                },
                tool_call_id="tc-render",
            ),
            _runtime_event(
                2,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-render",
                    "result": {"ok": True, "content": "EventStore 渲染工具返回"},
                },
                tool_call_id="tc-render",
            ),
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "id": "turn-render",
                "role": "assistant",
                "content": "助手最终正文",
                "reasoning_content": "history 渲染思考",
                "conversation_id": "private:10001",
                "direction": "outbound",
                "qq_visible": True,
                "tool_calls": [
                    {
                        "id": "tc-render",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"history-render.md"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-render",
                "conversation_id": "private:10001",
                "content": '{"ok":true,"content":"history 渲染工具返回"}',
            },
        ]
    )
    page = ChatsPage(rt)
    page._expand_reasoning_cb.setChecked(True)
    page._expand_tool_call_cb.setChecked(True)
    page._expand_tool_result_cb.setChecked(True)

    try:
        records = await _load_chat_page_records(rt)
        conv = next(item for item in _group_records_by_conversation(records) if item["key"] == "private:10001")
        html = page._render_conversation(conv)

        assert "history 渲染思考" in html
        assert "助手最终正文" in html
        assert "event-render.md" in html
        assert "EventStore 渲染工具返回" in html
        assert "路径：event-render.md" in html
        assert "内容：EventStore 渲染工具返回" in html
        assert "成功" in html
        assert "history-render.md" not in html
        assert "history 渲染工具返回" not in html
        assert html.count("Debata · 工具调用：write_file") == 1
        assert html.count("class='chat-record chat-event chat-event-tool'") == 2
        assert html.find("Debata · 思考过程") < html.find("助手最终正文")
        assert "&quot;tool_name&quot;" not in html
        assert "&quot;content&quot;:" not in html
        assert "result_hash" not in html
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page._search_debounce_timer.stop()
        page.deleteLater()


def test_chats_bubble_style_separates_light_and_dark_from_card_background():
    light = _chat_html_style("light")
    dark = _chat_html_style("dark")
    light_palette = palette_for_theme("light")
    dark_palette = palette_for_theme("dark")

    assert f".chat-bot .chat-bubble{{background:{light_palette.bg_card};}}" not in light
    assert f".chat-peer .chat-bubble{{background:{light_palette.bg_card};}}" not in light
    assert f".chat-bot .chat-bubble{{background:{dark_palette.bg_card};}}" not in dark
    assert f".chat-peer .chat-bubble{{background:{dark_palette.bg_card};}}" not in dark
    assert ".chat-bot .chat-bubble{background:#F1E8D6;}" in light
    assert ".chat-bot .chat-bubble{background:#302B26;}" in dark
    assert f"color:{light_palette.text_primary};" in light
    assert f"color:{dark_palette.text_primary};" in dark


def test_chats_expanded_event_detail_is_centered_and_low_key():
    style = _chat_html_style("light")
    detail_rule = style.split(".chat-event-detail{", 1)[1].split("}", 1)[0]

    assert "text-align:center" in detail_rule
    assert "font-size:13px" in detail_rule
    assert "font-weight:400" in detail_rule
    assert "color:" in detail_rule
    assert "background:" not in detail_rule
    assert "border:" not in detail_rule
    assert "border-radius" not in detail_rule


def test_chats_reasoning_is_collapsible_left_bubble(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {
                "id": "turn-1",
                "role": "assistant",
                "content": "",
                "reasoning_content": "内部推理 raw-only",
                "conversation_id": "group:1",
            }
        ],
    }

    html = page._render_conversation(conv)

    assert "Debata · 思考过程" in html
    assert "chat-record chat-message-table chat-side-left chat-bot" in html
    assert "chat-bubble" in html
    assert "chat-event-reasoning" not in html
    assert "点击展开" in html
    assert "内部推理 raw-only" not in html

    page._expanded_item_ids.add("group:1\nturn-1:reasoning")
    expanded = page._render_conversation(conv)

    assert "收起" in expanded
    assert "内部推理 raw-only" in expanded


def test_chats_splits_multiple_legacy_header_messages_into_right_bubbles(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "private:430666862",
        "label": "私聊 冰狼(430666862)",
        "records": [
            {
                "role": "user",
                "content": (
                    "【2026-06-08 10:00:00 私聊 冰狼(430666862) msg_id=35】第一条\n"
                    "【2026-06-08 10:00:02 私聊 冰狼(430666862) msg_id=36】第二条"
                ),
            }
        ],
    }

    html = page._render_conversation(conv)

    assert html.count("chat-record chat-message-table chat-side-right chat-peer") == 2
    assert "第一条" in html
    assert "第二条" in html
    assert "msg_id=35" not in html
    assert "msg_id=36" not in html
    assert "【2026-06-08" not in html


def test_chats_system_message_is_centered_plain_and_short_by_default(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "system:global",
        "label": "系统记录",
        "records": [
            {
                "role": "system",
                "content": (
                    "<task_context priority=\"medium\">\n"
                    "现在是2026-06-08 10:00:00。\n"
                    "当前会话：private:430666862。\n"
                    "<recent_private_messages></recent_private_messages>\n"
                    "</task_context>"
                ),
                "conversation_id": "system:global",
            }
        ],
    }

    html = page._render_conversation(conv)

    assert "系统消息" in html
    assert "点击展开" in html
    assert "本轮系统上下文" not in html
    assert "当前会话 private:430666862" not in html
    assert "class='chat-record chat-event chat-event-system' align='center'" in html
    assert "font-weight:600" not in html.split("chat-event-system", 1)[1].split("</style>", 1)[0]
    body_html = html.split("</style>", 1)[1]
    assert "chat-bubble" not in body_html


def test_chats_default_expand_controls_are_separate_from_type_filters(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    labels = [cb.text() for cb in page.findChildren(QtWidgets.QCheckBox)]

    assert "聊天" in labels
    assert "系统" in labels
    assert "工具" in labels
    assert "思考" in labels
    assert "展开思考" in labels
    assert "展开系统" in labels
    assert "展开工具调用" in labels
    assert "展开工具返回/结果" in labels

    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {
                "id": "turn-1",
                "role": "assistant",
                "content": "助手回答",
                "reasoning_content": "内部推理 raw-only",
                "direction": "outbound",
                "qq_visible": True,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "commit_send_attempt",
                            "arguments": '{"send_attempt_id":"attempt-1","reason":"确认发送"}',
                        },
                    }
                ],
                "conversation_id": "group:1",
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"status":"accepted","send_id":"send-1","send_attempt_id":"attempt-1"}',
                "conversation_id": "group:1",
            },
        ],
    }

    collapsed = page._render_conversation(conv)
    assert "内部推理 raw-only" not in collapsed
    assert "工具调用：提交发送尝试" not in collapsed
    assert "工具状态：已接受" not in collapsed

    page._expand_reasoning_cb.setChecked(True)
    page._expand_tool_call_cb.setChecked(True)
    page._expand_tool_result_cb.setChecked(True)
    expanded = page._render_conversation(conv)
    assert "内部推理 raw-only" in expanded
    assert "工具调用：提交发送尝试" in expanded
    assert "工具状态：已接受" in expanded

    page._show_tools_cb.setChecked(False)
    hidden_tools = page._render_conversation(conv)
    assert "提交被打断的消息" not in hidden_tools
    assert "工具状态：已接受" not in hidden_tools
    assert "内部推理 raw-only" in hidden_tools

    page._show_reasoning_cb.setChecked(False)
    hidden_reasoning = page._render_conversation(conv)
    assert "内部推理 raw-only" not in hidden_reasoning
    assert "助手回答" in hidden_reasoning

    page._show_system_cb.setChecked(False)
    page._show_tools_cb.setChecked(False)
    page._show_reasoning_cb.setChecked(True)
    page._expand_reasoning_cb.setChecked(False)
    chat_and_reasoning = page._render_conversation(conv)
    assert chat_and_reasoning.find("Debata · 思考过程") < chat_and_reasoning.find("助手回答")
    assert "内部推理 raw-only" not in chat_and_reasoning
