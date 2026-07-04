"""聊天页会话分组和跨会话发送状态回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import json

from ui.dashboard.chats_page import ChatsPage, _group_records_by_conversation, _render_record_html

from tests.test_dashboard_p2 import _dashboard_runtime


def test_chats_group_records_by_metadata_and_legacy_header():
    records = [
        {
            "role": "user",
            "content": "hello",
            "metadata": {
                "messages": [
                    {
                        "scope": "private",
                        "target_id": "10001",
                        "user_id": "10001",
                        "nickname": "Alice",
                    }
                ]
            },
        },
        {"role": "assistant", "content": "hi"},
        {
            "role": "user",
            "content": "【2026-05-27 12:00:00 群聊 20002 Bob(30003) msg_id=9】群消息",
        },
    ]

    grouped = _group_records_by_conversation(records)

    assert grouped[0]["key"] == "group:20002"
    assert grouped[0]["label"] == "群聊 20002"
    assert grouped[1]["key"] == "private:10001"
    assert len(grouped[1]["records"]) == 2


def test_chats_group_records_prefers_explicit_conversation_id():
    records = [
        {"role": "user", "content": "u", "conversation_id": "private:10001"},
        {"role": "assistant", "content": "a", "conversation_id": "private:10001"},
        {"role": "system", "content": "社交决策：本次跳过"},
        {
            "role": "tool",
            "content": "{}",
            "tool_call_id": "tc",
            "conversation_id": "group:20002",
        },
    ]

    grouped = _group_records_by_conversation(records)
    by_key = {item["key"]: item for item in grouped}

    assert by_key["private:10001"]["label"] == "私聊 10001"
    assert len(by_key["private:10001"]["records"]) == 2
    assert by_key["system:global"]["records"][0]["content"] == "社交决策：本次跳过"
    assert by_key["group:20002"]["records"][0]["tool_call_id"] == "tc"


def test_chats_keeps_send_tool_events_in_source_conversation():
    records = [
        {
            "role": "assistant",
            "content": "",
            "conversation_id": "private:10000",
            "tool_calls": [
                {
                    "id": "call-send",
                    "function": {
                        "name": "send_private_messages",
                        "arguments": json.dumps(
                            {
                                "targets": [
                                    {
                                        "target_qq": "10001",
                                        "content": "给一号",
                                        "order": 1,
                                        "delay": 0,
                                    },
                                    {
                                        "target_qq": "10002",
                                        "content": "给二号",
                                        "order": 2,
                                        "delay": 0,
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "private:10000",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "accepted_messages": [
                        {"conversation_id": "private:10001", "content": "给一号"},
                        {"conversation_id": "private:10002", "content": "给二号"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]

    grouped = _group_records_by_conversation(records)
    by_key = {item["key"]: item for item in grouped}

    assert "private:10000" in by_key
    assert "private:10001" not in by_key
    assert "private:10002" not in by_key
    assert [record.get("role") for record in by_key["private:10000"]["records"]] == ["assistant", "tool"]


def test_chats_groups_proactive_records_as_system():
    records = [
        {"role": "user", "content": "群消息", "conversation_id": "group:20002"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            "conversation_id": "system:proactive",
        },
        {
            "role": "tool",
            "content": '{"ok":true}',
            "tool_call_id": "tc-proactive",
            "conversation_id": "system:proactive",
        },
    ]

    grouped = _group_records_by_conversation(records)
    by_key = {item["key"]: item for item in grouped}

    assert "system:proactive" in by_key
    assert by_key["system:proactive"]["label"] == "系统记录 · 社交决策"
    assert by_key["system:proactive"]["records"][1]["tool_call_id"] == "tc-proactive"


def test_chats_unknown_assistant_without_context_does_not_attach_to_previous_chat():
    records = [
        {"role": "system", "content": "全局事件"},
        {"role": "assistant", "content": "后台旧记录"},
        {"role": "user", "content": "hi", "conversation_id": "private:10001"},
    ]

    grouped = _group_records_by_conversation(records)
    by_key = {item["key"]: item for item in grouped}

    assert by_key["unknown:history"]["records"][0]["content"] == "后台旧记录"
    assert by_key["private:10001"]["records"][0]["content"] == "hi"


def test_chats_target_conversation_does_not_render_copied_send_tool_events(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "assistant",
            "content": "",
            "conversation_id": "private:10000",
            "tool_calls": [
                {
                    "id": "call-send",
                    "function": {
                        "name": "send_private_messages",
                        "arguments": json.dumps(
                            {
                                "targets": [
                                    {
                                        "target_qq": "10001",
                                        "content": "给一号",
                                        "order": 1,
                                        "delay": 0,
                                    },
                                    {
                                        "target_qq": "10002",
                                        "content": "给二号",
                                        "order": 2,
                                        "delay": 0,
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "private:10000",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "sent": [
                        {"conversation_id": "private:10001", "content": "给一号"},
                        {"conversation_id": "private:10002", "content": "给二号"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    conversations = _group_records_by_conversation(records)

    source = next(item for item in conversations if item["key"] == "private:10000")
    source_html = page._render_conversation(source)

    assert {item["key"] for item in conversations} == {"private:10000"}
    assert "Debata · 发送私聊消息" in source_html
    assert "工具返回" in source_html
    assert "给一号" not in source_html
    assert "给二号" not in source_html


def test_chats_tool_result_does_not_duplicate_real_outbound_message(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "assistant",
            "content": "",
            "conversation_id": "system:request",
            "tool_calls": [
                {
                    "id": "call-send",
                    "function": {
                        "name": "send_private_messages",
                        "arguments": '{"targets":[{"target_qq":"10001","content":"已经真实发出","order":1,"delay":0}]}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "system:request",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "sent": [{"conversation_id": "private:10001", "content": "已经真实发出"}],
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "assistant",
            "direction": "outbound",
            "content": "已经真实发出",
            "conversation_id": "private:10001",
            "qq_visible": True,
            "msg_id": "300",
        },
    ]
    conv = next(item for item in _group_records_by_conversation(records) if item["key"] == "private:10001")

    html = page._render_conversation(conv)

    assert html.count("已经真实发出") == 1
    assert "chat-record chat-message-table chat-side-left chat-bot" in html
    assert "工具返回" not in html


def test_chats_cross_conversation_send_uses_timeline_for_target_bubble(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "assistant",
            "content": "",
            "conversation_id": "private:10000",
            "tool_calls": [
                {
                    "id": "call-send",
                    "function": {
                        "name": "send_private_messages",
                        "arguments": '{"targets":[{"target_qq":"10001","content":"发给 B 的正文","order":1,"delay":0}]}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "private:10000",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "send_id": "send-ab",
                    "accepted_messages": [
                        {"conversation_id": "private:10001", "content": "发给 B 的正文"}
                    ],
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "assistant",
            "direction": "outbound",
            "content": "发给 B 的正文",
            "conversation_id": "private:10001",
            "qq_visible": True,
            "msg_id": "b-1",
            "timestamp": "2026-06-08 23:07:19",
            "_source": "chat_timeline",
        },
    ]
    conversations = _group_records_by_conversation(records)
    source_html = page._render_conversation(
        next(item for item in conversations if item["key"] == "private:10000")
    )
    target_html = page._render_conversation(
        next(item for item in conversations if item["key"] == "private:10001")
    )

    assert "Debata · 发送私聊消息" in source_html
    assert "工具返回" in source_html
    assert "chat-record chat-message-table chat-side-left chat-bot" not in source_html
    assert "发给 B 的正文" not in source_html
    assert "chat-record chat-message-table chat-side-left chat-bot" in target_html
    assert "发给 B 的正文" in target_html
    assert "工具返回" not in target_html
    assert "Debata · 发送私聊消息" not in target_html


def test_chats_cross_conversation_send_receipt_sent_falls_back_only_in_target(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    receipt = json.dumps(
        {
            "status": "sent",
            "send_id": "send-receipt-ab",
            "sent": [
                {
                    "conversation_id": "private:10001",
                    "content": "发给 B 的回执正文",
                    "msg_id": "b-receipt-1",
                    "qq_visible": True,
                }
            ],
            "accepted_messages": [
                {"conversation_id": "private:10001", "content": "不要用 accepted 成泡"}
            ],
            "attempted_messages": [
                {"conversation_id": "private:10001", "content": "不要用 attempted 成泡"}
            ],
        },
        ensure_ascii=False,
    )
    records = [
        {
            "role": "user",
            "conversation_id": "private:10000",
            "content": f"<send_receipt>\n{receipt}\n</send_receipt>",
        }
    ]

    conversations = _group_records_by_conversation(records)
    source_html = page._render_conversation(
        next(item for item in conversations if item["key"] == "private:10000")
    )
    target_html = page._render_conversation(
        next(item for item in conversations if item["key"] == "private:10001")
    )
    source_body = source_html.split("</style>", 1)[1]
    target_body = target_html.split("</style>", 1)[1]

    assert {item["key"] for item in conversations} == {"private:10000", "private:10001"}
    assert "系统消息" in source_body
    assert "点击展开" in source_body
    assert "chat-record chat-message-table chat-side-left chat-bot" not in source_body
    assert "发给 B 的回执正文" not in source_body
    assert "chat-record chat-message-table chat-side-left chat-bot" in target_body
    assert "发给 B 的回执正文" in target_body
    assert "已发送 · msg_id=b-receipt-1" in target_body
    assert target_body.count("发给 B 的回执正文") == 1
    assert "chat-event" not in target_body
    assert "系统消息" not in target_body
    assert "发送回执" not in target_body
    assert "点击展开" not in target_body
    assert "不要用 accepted 成泡" not in target_body
    assert "不要用 attempted 成泡" not in target_body


def test_chats_cross_conversation_text_send_receipt_sent_falls_back_only_in_target(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    receipt = (
        "<send_receipt>\n"
        "发送回执：send-receipt-ab\n"
        "会话：private:10000\n"
        "状态：已完成（interrupted=false）。\n"
        "已发送 1 条：\n"
        "1. 发给 B 的文本回执正文；order=1；msg_id=b-receipt-1；conversation_id=private:10001\n"
        "未发送 1 条：\n"
        "1. 不要用 unsent 成泡；order=2；send_id=send-receipt-ab；conversation_id=private:10001\n"
        "新消息 0 条：\n"
        "- 无\n"
        "撤回消息 0 条：\n"
        "- 无\n"
        "错误 0 条：\n"
        "- 无\n"
        "处理要求：不要重发已发送内容。\n"
        "</send_receipt>"
    )
    records = [
        {
            "role": "user",
            "conversation_id": "private:10000",
            "content": receipt,
        }
    ]

    conversations = _group_records_by_conversation(records)
    source_html = page._render_conversation(
        next(item for item in conversations if item["key"] == "private:10000")
    )
    target_html = page._render_conversation(
        next(item for item in conversations if item["key"] == "private:10001")
    )
    source_body = source_html.split("</style>", 1)[1]
    target_body = target_html.split("</style>", 1)[1]

    assert {item["key"] for item in conversations} == {"private:10000", "private:10001"}
    assert "系统消息" in source_body
    assert "chat-record chat-message-table chat-side-left chat-bot" not in source_body
    assert "发给 B 的文本回执正文" not in source_body
    assert "不要用 unsent 成泡" not in source_body
    assert "chat-record chat-message-table chat-side-left chat-bot" in target_body
    assert "发给 B 的文本回执正文" in target_body
    assert "已发送 · msg_id=b-receipt-1" in target_body
    assert target_body.count("发给 B 的文本回执正文") == 1
    assert "chat-event" not in target_body
    assert "发送回执" not in target_body
    assert "不要用 unsent 成泡" not in target_body


def test_chats_text_send_receipt_only_sent_section_promotes_to_bubble():
    receipt_html = _render_record_html(
        {
            "role": "user",
            "conversation_id": "private:10001",
            "content": (
                "<send_receipt>\n"
                "发送回执：send-text-unsent\n"
                "会话：private:10001\n"
                "状态：有未发送内容（interrupted=false）。\n"
                "已发送 0 条：\n"
                "- 无\n"
                "未发送 1 条：\n"
                "1. 不要用 unsent 文本成泡；order=1；send_id=send-text-unsent；conversation_id=private:10001\n"
                "attempted 1 条：\n"
                "1. 不要用 attempted 文本成泡；conversation_id=private:10001\n"
                "accepted 1 条：\n"
                "1. 不要用 accepted 文本成泡；conversation_id=private:10001\n"
                "新消息 0 条：\n"
                "- 无\n"
                "撤回消息 0 条：\n"
                "- 无\n"
                "错误 0 条：\n"
                "- 无\n"
                "处理要求：不要重发已发送内容。\n"
                "</send_receipt>"
            ),
        },
        persona_name="玖",
    )

    assert "系统消息" in receipt_html
    assert "不要用 unsent 文本成泡" not in receipt_html
    assert "不要用 attempted 文本成泡" not in receipt_html
    assert "不要用 accepted 文本成泡" not in receipt_html
    assert "chat-message-table chat-side-left chat-bot" not in receipt_html


def test_chats_send_status_combines_accepted_messages_into_left_bubble(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "system:request",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "send_id": "send-77",
                    "accepted_messages": [
                        {"conversation_id": "private:10001", "content": "测试完了？"}
                    ],
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "user",
            "conversation_id": "system:request",
            "content": (
                "<send_status>\n"
                "系统说明：以下内容由运行时系统提供，不是用户新发言。\n"
                "2026-06-08 19:44:00 发送完成 send_id=send-77 msg_ids=[901]\n"
                "</send_status>"
            ),
        },
    ]
    conv = next(item for item in _group_records_by_conversation(records) if item["key"] == "private:10001")

    html = page._render_conversation(conv)
    body_html = html.split("</style>", 1)[1]

    assert "chat-record chat-message-table chat-side-left chat-bot" in body_html
    assert "测试完了？" in body_html
    assert "已发送 · msg_id=901" in body_html
    assert body_html.count("测试完了？") == 1
    assert "send_id=send-77" not in body_html


def test_chats_send_status_pending_does_not_promote_accepted_messages(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "system:request",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "send_id": "send-pending",
                    "accepted_messages": [
                        {"conversation_id": "private:10001", "content": "还没确认发送"}
                    ],
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "user",
            "conversation_id": "system:request",
            "content": "<send_status>等待发送 send_id=send-pending msg_ids=[903]</send_status>",
        },
    ]
    conversations = _group_records_by_conversation(records)

    assert "private:10001" not in {item["key"] for item in conversations}
    system_html = page._render_conversation(
        next(item for item in conversations if item["key"] == "system:request")
    )
    assert "chat-record chat-message-table chat-side-left chat-bot" not in system_html
    assert "还没确认发送" not in system_html


def test_chats_send_status_does_not_duplicate_existing_real_outbound(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "system:request",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "send_id": "send-88",
                    "accepted_messages": [
                        {"conversation_id": "private:10001", "content": "已经真实发出"}
                    ],
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "user",
            "conversation_id": "system:request",
            "content": "<send_status>发送完成 send_id=send-88 msg_ids=[902]</send_status>",
        },
        {
            "role": "assistant",
            "direction": "outbound",
            "content": "已经真实发出",
            "conversation_id": "private:10001",
            "qq_visible": True,
            "msg_id": "902",
        },
    ]
    conv = next(item for item in _group_records_by_conversation(records) if item["key"] == "private:10001")

    html = page._render_conversation(conv)

    assert html.count("已经真实发出") == 1
    assert html.count("msg_id=902") == 1


def test_chats_cross_conversation_send_status_creates_each_target_bubble(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "tool",
            "tool_call_id": "call-send",
            "conversation_id": "system:request",
            "content": json.dumps(
                {
                    "status": "accepted",
                    "send_id": "send-99",
                    "accepted_messages": [
                        {"conversation_id": "private:10001", "content": "第一边"},
                        {"conversation_id": "private:10002", "content": "第二边"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "user",
            "conversation_id": "system:request",
            "content": "<send_status>发送完成 send_id=send-99 msg_ids=[911,912]</send_status>",
        },
    ]
    conversations = _group_records_by_conversation(records)

    first_html = page._render_conversation(next(item for item in conversations if item["key"] == "private:10001"))
    second_html = page._render_conversation(next(item for item in conversations if item["key"] == "private:10002"))

    assert "第一边" in first_html
    assert "msg_id=911" in first_html
    assert "第二边" not in first_html
    assert "第二边" in second_html
    assert "msg_id=912" in second_html
    assert "第一边" not in second_html
