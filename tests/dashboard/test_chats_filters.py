"""聊天页显示缓存、筛选和分页回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import ui.dashboard.chats_page as chats_page_module
from ui.dashboard.chats_page import (
    DEFAULT_VISIBLE_RECORD_LIMIT,
    ChatsPage,
    DisplayItem,
    _build_display_items,
    _build_render_items,
    _compact_inline_tokens,
    _conversation_list_signature,
    _filter_visible_records,
    _render_record_bubbles,
    _render_record_html,
    _scrollbar_near_bottom,
    normalize_history_records,
)

from tests.test_dashboard_p2 import _dashboard_runtime, _refresh_test_chats_page


def test_chats_conversation_signature_tracks_visible_list_changes():
    conversations = [
        {"key": "group:1", "records": [{"content": "a"}], "preview": "a"},
        {"key": "system:global", "records": [{"content": "s"}], "preview": "s"},
    ]

    assert _conversation_list_signature(conversations) == [
        ("group:1", 1, "a"),
        ("system:global", 1, "s"),
    ]


def test_chats_conversation_signature_uses_full_record_count():
    conversations = [
        {
            "key": "group:1",
            "records": [{"content": str(i)} for i in range(DEFAULT_VISIBLE_RECORD_LIMIT + 25)],
            "preview": "latest",
        }
    ]

    assert _conversation_list_signature(conversations) == [
        ("group:1", DEFAULT_VISIBLE_RECORD_LIMIT + 25, "latest")
    ]


def test_chats_display_cache_reuses_normalized_and_filtered_items(
    qapp,
    tmp_paths,
    monkeypatch,
):
    real_normalize = chats_page_module.normalize_history_records
    real_filter = chats_page_module._filter_display_items
    normalize_calls = 0
    filter_calls = 0

    def spy_normalize(records, *, persona_name, bot_user_id=None):
        nonlocal normalize_calls
        normalize_calls += 1
        return real_normalize(
            records,
            persona_name=persona_name,
            bot_user_id=bot_user_id,
        )

    def spy_filter(
        items,
        *,
        search_text,
        show_chat,
        show_system,
        show_tools,
        show_reasoning=True,
        media_only=False,
    ):
        nonlocal filter_calls
        filter_calls += 1
        return real_filter(
            items,
            search_text=search_text,
            show_chat=show_chat,
            show_system=show_system,
            show_tools=show_tools,
            show_reasoning=show_reasoning,
            media_only=media_only,
        )

    monkeypatch.setattr(chats_page_module, "normalize_history_records", spy_normalize)
    monkeypatch.setattr(chats_page_module, "_filter_display_items", spy_filter)
    page = _refresh_test_chats_page(tmp_paths)
    records = [
        {"role": "user", "content": "alpha", "conversation_id": "group:1"},
        {"role": "assistant", "content": "beta", "conversation_id": "group:1"},
    ]
    conv = {"key": "group:1", "label": "群聊 1", "records": records}
    copied_conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [dict(record) for record in records],
    }

    try:
        page._render_conversation(conv)
        assert page._filtered_display_count(copied_conv) == 2
        page._render_conversation(copied_conv)

        assert normalize_calls == 1
        assert filter_calls == 1
    finally:
        page._search_debounce_timer.stop()
        page.deleteLater()


def test_chats_display_cache_invalidates_when_records_signature_changes(
    qapp,
    tmp_paths,
    monkeypatch,
):
    real_normalize = chats_page_module.normalize_history_records
    normalize_calls = 0

    def spy_normalize(records, *, persona_name, bot_user_id=None):
        nonlocal normalize_calls
        normalize_calls += 1
        return real_normalize(
            records,
            persona_name=persona_name,
            bot_user_id=bot_user_id,
        )

    monkeypatch.setattr(chats_page_module, "normalize_history_records", spy_normalize)
    page = _refresh_test_chats_page(tmp_paths)
    first_records = [
        {"role": "user", "content": "第一条", "conversation_id": "group:1"},
    ]
    second_records = [
        {"role": "user", "content": "第二条", "conversation_id": "group:1"},
    ]

    try:
        page._set_records(first_records)
        page._render_conversation(
            {"key": "group:1", "label": "群聊 1", "records": first_records}
        )
        page._set_records([dict(record) for record in first_records])
        page._render_conversation(
            {"key": "group:1", "label": "群聊 1", "records": [dict(record) for record in first_records]}
        )

        page._set_records(second_records)
        html = page._render_conversation(
            {"key": "group:1", "label": "群聊 1", "records": second_records}
        )

        assert normalize_calls == 2
        assert "第二条" in html
    finally:
        page._search_debounce_timer.stop()
        page.deleteLater()


def test_chats_search_text_changes_debounce_detail_refresh(qapp, tmp_paths):
    page = _refresh_test_chats_page(tmp_paths)
    page._search_debounce_timer.setInterval(0)
    calls = []
    page._refresh_current_detail = lambda: calls.append(page._search_text)

    try:
        page._search_input.setText("a")
        page._search_input.setText("al")
        page._search_input.setText("alpha")

        assert page._search_text == "alpha"
        assert calls == []

        for _ in range(3):
            qapp.processEvents()

        assert calls == ["alpha"]
    finally:
        page._search_debounce_timer.stop()
        page.deleteLater()


def test_chats_checkbox_filter_refreshes_immediately(qapp, tmp_paths):
    page = _refresh_test_chats_page(tmp_paths)
    calls = []
    page._refresh_current_detail = lambda: calls.append(page._search_text)

    try:
        page._search_input.setText("alpha")
        assert calls == []
        assert page._search_debounce_timer.isActive()

        page._show_tools_cb.setChecked(False)

        assert calls == ["alpha"]
        assert not page._search_debounce_timer.isActive()
    finally:
        page._search_debounce_timer.stop()
        page.deleteLater()


def test_chats_normalizes_records_to_display_items():
    records = [
        {
            "role": "user",
            "content": "【2026-05-27 12:00:00 群聊 20002 Bob(30003) msg_id=9】群消息",
            "conversation_id": "group:20002",
        },
        {
            "role": "assistant",
            "content": "内部草稿",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "send_group_message",
                        "arguments": '{"group_id":20002,"targets":[{"content":"发出","order":1,"delay":0}]}',
                    },
                }
            ],
            "conversation_id": "group:20002",
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"status":"accepted","delivery":"pending","qq_visible":"pending"}',
            "conversation_id": "group:20002",
        },
        {
            "role": "user",
            "content": (
                "<send_receipt>\n"
                '{"status":"stale","attempted_messages":[{"content":"不要当成已发送"}]}\n'
                "</send_receipt>"
            ),
            "conversation_id": "group:20002",
        },
    ]

    items = normalize_history_records(records, persona_name="玖")

    assert all(isinstance(item, DisplayItem) for item in items)
    assert [item.kind for item in items] == [
        "inbound_message",
        "assistant_note",
        "tool_call",
        "runtime_receipt",
    ]
    assert items[0].speaker_label == "Bob(30003)"
    assert items[1].speaker_label == "玖"
    assert items[2].tool_results[0].kind == "tool_result"
    assert "QQ 可见性待确认" in items[2].tool_results[0].summary
    assert "outbound_message" not in [item.kind for item in items]


def test_chats_build_display_items_filters_reasoning_independently_from_system():
    records = [
        {
            "role": "assistant",
            "content": "助手正文",
            "reasoning_content": "独立思考内容",
            "direction": "outbound",
            "qq_visible": True,
            "conversation_id": "group:1",
        },
        {
            "role": "system",
            "content": "系统消息内容",
            "conversation_id": "group:1",
        },
    ]

    without_system = _build_display_items(
        records,
        persona_name="玖",
        search_text="",
        show_chat=True,
        show_system=False,
        show_tools=True,
        show_reasoning=True,
    )
    without_reasoning = _build_display_items(
        records,
        persona_name="玖",
        search_text="",
        show_chat=True,
        show_system=True,
        show_tools=True,
        show_reasoning=False,
    )

    assert [item.kind for item in without_system] == ["reasoning", "outbound_message"]
    assert [item.text for item in without_system] == ["独立思考内容", "助手正文"]
    assert [item.kind for item in without_reasoning] == ["outbound_message", "system_event"]
    assert "独立思考内容" not in [item.text for item in without_reasoning]


def test_chats_build_display_items_keeps_tool_parent_when_result_matches():
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "get_recent_chat_messages",
                        "arguments": '{"conversation_id":"group:1","limit":5}',
                    },
                }
            ],
            "conversation_id": "group:1",
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"status":"artifact","content":"needle old message"}',
            "conversation_id": "group:1",
        },
    ]

    items = _build_display_items(
        records,
        persona_name="玖",
        search_text="needle",
        show_chat=True,
        show_system=True,
        show_tools=True,
    )

    assert len(items) == 1
    assert items[0].kind == "tool_call"
    assert items[0].tool_results[0].related_tool_call_id == "call-1"


def test_chats_filters_assistant_text_and_tool_bubbles_independently():
    record = {
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
    }

    text_only = _render_record_bubbles(
        record,
        persona_name="玖",
        show_chat=True,
        show_tools=False,
    )
    tool_only = _render_record_bubbles(
        record,
        persona_name="玖",
        show_chat=False,
        show_system=False,
        show_tools=True,
    )

    assert len(text_only) == 1
    assert "内部文本" in text_only[0]
    assert "准备发" not in text_only[0]
    assert "工具调用" not in text_only[0]
    assert len(tool_only) == 1
    assert "准备发" not in tool_only[0]
    assert "玖 · 发送私聊消息" in tool_only[0]
    assert "向 123 发送消息：你好" not in tool_only[0]


def test_chats_attaches_tool_results_to_matching_tool_calls():
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "send_private_messages",
                        "arguments": '{"targets":[{"target_qq":123,"content":"你好","order":1,"delay":0}]}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"status":"accepted","send_id":"send-1"}',
        },
        {
            "role": "tool",
            "tool_call_id": "orphan",
            "content": '{"status":"done"}',
        },
    ]

    items = _build_render_items(
        records,
        search_text="",
        show_chat=True,
        show_system=True,
        show_tools=True,
    )

    assert len(items) == 2
    assert items[0][0] is records[0]
    assert items[0][1] == {"call-1": [records[1]]}
    assert items[1][0] is records[2]

    html = "".join(
        bubble
        for record, attached in items
        for bubble in _render_record_bubbles(
            record,
            persona_name="玖",
            attached_tool_results=attached,
        )
    )

    assert "玖 · 发送私聊消息" in html
    assert "向 123 发送消息：你好" not in html
    assert "send_id=send-1" not in html
    assert html.count("工具返回") == 2


def test_chats_tool_result_search_keeps_parent_tool_call_visible():
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "send_group_message",
                        "arguments": '{"group_id":1,"targets":[{"content":"普通","order":1,"delay":0}]}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"status":"accepted","send_id":"needle-send"}',
        },
    ]

    items = _build_render_items(
        records,
        search_text="needle-send",
        show_chat=True,
        show_system=True,
        show_tools=True,
    )

    assert len(items) == 1
    assert items[0][0] is records[0]
    assert items[0][1] == {"call-1": [records[1]]}


def test_chats_media_filter_keeps_only_image_and_file_records():
    records = [
        {"role": "user", "content": "普通聊天"},
        {"role": "user", "content": "[图片 workspace=incoming/img_1.jpg]"},
        {"role": "user", "content": "报告在 C:\\Users\\admin\\Desktop\\report.pdf"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-upload",
                    "function": {
                        "name": "upload_file",
                        "arguments": '{"file_path":"report.md"}',
                    },
                }
            ],
        },
    ]

    filtered = _filter_visible_records(
        records,
        search_text="",
        show_chat=True,
        show_system=True,
        show_tools=True,
        media_only=True,
    )

    assert filtered == records[1:]


def test_chats_media_filter_keeps_parent_for_media_tool_result():
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "get_recent_chat_messages",
                        "arguments": '{"limit":5}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"ok":true,"content":"[CQ:image,file=a.png,url=https://example.com/a.png]"}',
        },
    ]

    items = _build_render_items(
        records,
        search_text="",
        show_chat=True,
        show_system=True,
        show_tools=True,
        media_only=True,
    )

    assert len(items) == 1
    assert items[0][0] is records[0]
    assert items[0][1] == {"call-1": [records[1]]}


def test_chats_filter_visible_records_searches_metadata_and_categories():
    records = [
        {
            "role": "user",
            "content": "普通聊天",
            "metadata": {"messages": [{"nickname": "Alice"}]},
        },
        {"role": "system", "content": "系统事件"},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"status":"accepted"}',
        },
    ]

    assert _filter_visible_records(
        records,
        search_text="Alice",
        show_chat=True,
        show_system=True,
        show_tools=True,
    ) == [records[0]]
    assert _filter_visible_records(
        records,
        search_text="",
        show_chat=True,
        show_system=False,
        show_tools=False,
    ) == [records[0]]
    assert _filter_visible_records(
        records,
        search_text="accepted",
        show_chat=False,
        show_system=False,
        show_tools=True,
    ) == [records[2]]


def test_chats_compacts_long_links_and_paths():
    long_url = "https://multimedia.nt.qq.com.cn/download?" + ("a" * 120)
    long_path = "C:\\Users\\admin\\.qq-chat-exporter\\exports\\" + ("x" * 100) + ".txt"
    long_json = '{"status":"accepted","items":[' + ",".join('"x"' for _ in range(60)) + "]}"

    compact = _compact_inline_tokens(f"{long_url} {long_path}")
    compact_json = _compact_inline_tokens(long_json)

    assert "[URL multimedia.nt.qq.com.cn/" in compact
    assert "[路径 " in compact
    assert long_url not in compact
    assert long_path not in compact
    assert "[JSON对象" in compact_json
    assert "已折叠" in compact_json
    assert '"items"' not in compact_json


def test_chats_tool_result_summary_shows_pending_state():
    items = normalize_history_records(
        [
            {
                "role": "tool",
                "tool_call_id": "call-pending",
                "content": '{"status":"accepted","delivery":"pending","qq_visible":"pending"}',
            }
        ],
        persona_name="玖",
    )
    html = _render_record_html(
        {
            "role": "tool",
            "tool_call_id": "call-pending",
            "content": '{"status":"accepted","delivery":"pending","qq_visible":"pending"}',
        },
        persona_name="玖",
    )

    assert "状态 已接受" in items[0].summary
    assert "正在投递" in items[0].summary
    assert "QQ 可见性待确认" in items[0].summary
    assert "工具返回" in html
    assert "点击展开" in html
    assert "状态 accepted" not in html


def test_chats_render_conversation_paginates_without_dropping_history(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {"role": "user", "content": f"消息 {i}", "conversation_id": "group:1"}
            for i in range(DEFAULT_VISIBLE_RECORD_LIMIT + 5)
        ],
    }

    html = page._render_conversation(conv)

    assert f"已显示 {DEFAULT_VISIBLE_RECORD_LIMIT} / 共 {DEFAULT_VISIBLE_RECORD_LIMIT + 5} 条" in html
    assert "还有 5 条更早记录未显示" in html
    assert "显示全部" in html
    assert "消息 0" not in html
    assert f"消息 {DEFAULT_VISIBLE_RECORD_LIMIT + 4}" in html


def test_chats_render_conversation_paginates_display_items_not_raw_records(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {
                "role": "assistant",
                "content": f"内部 {i}",
                "tool_calls": [
                    {
                        "id": f"call-{i}",
                        "function": {"name": "no_action", "arguments": "{}"},
                    }
                ],
                "conversation_id": "group:1",
            }
            for i in range(200)
        ],
    }

    html = page._render_conversation(conv)

    assert "已显示 300 / 共 400 条" in html
    assert "还有 100 条更早记录未显示" in html
    assert "内部 0" not in html


def test_chats_search_can_find_older_display_items(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {"role": "user", "content": f"消息 {i}", "conversation_id": "group:1"}
            for i in range(DEFAULT_VISIBLE_RECORD_LIMIT + 5)
        ],
    }
    page._search_input.setText("消息 0")

    html = page._render_conversation(conv)

    assert "消息 0" in html
    assert f"消息 {DEFAULT_VISIBLE_RECORD_LIMIT + 4}" not in html
    assert "当前过滤后 1 条" in html


def test_chats_load_more_current_increases_display_item_limit(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    records = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-{i}",
                    "function": {"name": "no_action", "arguments": "{}"},
                }
            ],
            "conversation_id": "group:1",
        }
        for i in range(DEFAULT_VISIBLE_RECORD_LIMIT + 2)
    ]
    conv = {"key": "group:1", "label": "群聊 1", "records": records}
    page._conversations = [conv]
    page._current_key = "group:1"

    page._load_more_current()

    assert page._visible_record_limits["group:1"] == DEFAULT_VISIBLE_RECORD_LIMIT + 2


def test_chats_show_all_current_displays_full_conversation(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {"role": "user", "content": f"消息 {i}", "conversation_id": "group:1"}
            for i in range(DEFAULT_VISIBLE_RECORD_LIMIT + 5)
        ],
    }
    page._conversations = [conv]
    page._current_key = "group:1"

    page._show_all_current()
    html = page._render_conversation(conv)

    assert f"已显示 {DEFAULT_VISIBLE_RECORD_LIMIT + 5} / 共 {DEFAULT_VISIBLE_RECORD_LIMIT + 5} 条" in html
    assert "消息 0" in html
    assert "还有 5 条更早记录未显示" not in html


def test_chats_render_conversation_applies_search_and_filters(qapp, tmp_paths):
    page = ChatsPage(_dashboard_runtime(tmp_paths))
    conv = {
        "key": "group:1",
        "label": "群聊 1",
        "records": [
            {"role": "user", "content": "alpha 聊天", "conversation_id": "group:1"},
            {"role": "system", "content": "alpha 系统", "conversation_id": "group:1"},
            {
                "role": "tool",
                "tool_call_id": "call-alpha",
                "content": '{"status":"accepted","detail":"alpha 工具"}',
                "conversation_id": "group:1",
            },
        ],
    }
    page._search_input.setText("alpha")
    page._show_system_cb.setChecked(False)
    page._show_tools_cb.setChecked(False)

    html = page._render_conversation(conv)

    assert "alpha 聊天" in html
    assert "alpha 系统" not in html
    assert "alpha 工具" not in html
    assert "当前过滤后 1 条" in html


def test_chats_scrollbar_bottom_threshold():
    class FakeBar:
        def __init__(self, value: int, maximum: int) -> None:
            self._value = value
            self._maximum = maximum

        def value(self) -> int:
            return self._value

        def maximum(self) -> int:
            return self._maximum

    assert _scrollbar_near_bottom(FakeBar(980, 1000), threshold=24) is True
    assert _scrollbar_near_bottom(FakeBar(900, 1000), threshold=24) is False
