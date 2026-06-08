"""P2/体验修复的轻量回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
QApplication = QtWidgets.QApplication
QLabel = QtWidgets.QLabel
QWidget = QtWidgets.QWidget
Qt = QtCore.Qt

from app_config.loader import load_config
from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    EmbeddingFeatureConfig,
    FeaturesConfig,
    LongTermMemoryConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
    VisionFeatureConfig,
)
from memory.important import ImportantMemoryManager
from memory.rag_store import RagEntry
from ui.dashboard.chats_page import (
    DEFAULT_VISIBLE_RECORD_LIMIT,
    ChatsPage,
    DisplayItem,
    _build_display_items,
    _build_render_items,
    _chat_html_style,
    _compact_inline_tokens,
    _conversation_list_signature,
    _filter_visible_records,
    _format_send_receipt_summary,
    _format_tool_call_for_display,
    _group_records_by_conversation,
    _load_chat_page_records,
    _render_record_bubbles,
    _render_record_html,
    _scrollbar_near_bottom,
    normalize_history_records,
)
from ui.dashboard.layout import DEFAULT_LAYOUT
from ui.dashboard.logs_page import _format_record
from ui.dashboard.main_window import DashboardWindow
from ui.dashboard.memory_page import MemoryPage
from ui.dashboard.overview_page import OverviewPage
from ui.dashboard.personas_page import PersonasPage, _PersonaCreatorDialog
from ui.dashboard.settings_page import SettingsPage
from ui.theme import palette_for_theme
from ui.widgets.model_combo import ModelComboBox
from ui.widgets.window_chrome import _resize_edges_for_local_pos
from ui.wizard.components import ApiKeyInput
from ui.wizard.context import WizardContext
from ui.wizard.persona_creator import PersonaCreatorStepView
from ui.wizard.step_views.features import _TTSFeatureCard
from ui.wizard.step_views.welcome import WelcomeStepView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _minimal_root_config() -> RootConfig:
    return RootConfig(
        providers={"ds": ProviderConfig(preset="deepseek", api_key_id="ds_key")},
        adapters={"default": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=AgentConfig(provider="ds", model="deepseek-chat")),
    )


class _EmptyHistory:
    async def records(self):
        return []


class _StaticRecordStore:
    def __init__(self, records):
        self._records = records

    async def records(self):
        return list(self._records)


class _PagedArchiveStore:
    def __init__(self, records, *, page_size: int = 500):
        self._records = list(records)
        self.page_size = page_size
        self.filter_calls = []
        self.get_by_ids_calls = []
        self.records_called = False

    async def records(self):
        self.records_called = True
        raise AssertionError("archive.records should not be used when filter_records exists")

    async def filter_records(self, query):
        self.filter_calls.append(dict(query))
        offset = int(query.get("offset") or 0)
        limit = min(int(query.get("limit") or self.page_size), self.page_size)
        selected = self._records[offset:offset + limit]
        return {
            "ok": True,
            "count": len(selected),
            "total": len(self._records),
            "limit": limit,
            "offset": offset,
            "order": query.get("order") or "asc",
            "results": [
                {
                    "id": item["archive_id"],
                    "time": item.get("timestamp"),
                    "conversation_id": item.get("conversation_id"),
                    "sender": item.get("sender_name") or "-",
                    "sender_id": item.get("sender_id"),
                    "sender_name": item.get("sender_name"),
                    "direction": item.get("direction") or "inbound",
                    "kind": item.get("kind") or "text",
                    "content": item.get("content") or "",
                    "metadata": item.get("metadata") or {},
                }
                for item in selected
            ],
        }

    async def get_by_ids(self, archive_ids):
        self.get_by_ids_calls.append(list(archive_ids))
        by_id = {item["archive_id"]: item for item in self._records}
        return [dict(by_id[archive_id]) for archive_id in archive_ids if archive_id in by_id]


class _FailingRecordStore:
    async def records(self):
        raise RuntimeError("boom")


class _EmptyImportant:
    def items(self):
        return []


def _dashboard_runtime(tmp_paths, cfg: RootConfig | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        adapter=None,
        config=cfg or _minimal_root_config(),
        embedding_service=None,
        history=_EmptyHistory(),
        important=_EmptyImportant(),
        model_activity={},
        paths=tmp_paths,
        persona=SimpleNamespace(name="Debata"),
        provider_health={},
        provider_registry=SimpleNamespace(presets={}),
        providers={},
        rag_store=None,
        secrets=SimpleNamespace(get=lambda _key: ""),
        usage_stats=None,
    )


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
        {"role": "system", "content": "主动思考：本次跳过"},
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
    assert by_key["system:global"]["records"][0]["content"] == "主动思考：本次跳过"
    assert by_key["group:20002"]["records"][0]["tool_call_id"] == "tc"


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
    assert by_key["system:proactive"]["label"] == "系统记录 · 主动思考"
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


@pytest.mark.asyncio
async def test_chats_loads_archive_before_active_history(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _StaticRecordStore([{"role": "user", "content": "归档旧消息"}])
    rt.history = _StaticRecordStore([{"role": "assistant", "content": "活跃新消息"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["归档旧消息", "活跃新消息"]


@pytest.mark.asyncio
async def test_chats_loads_archive_through_filter_records_pages(tmp_paths):
    archived = [
        {
            "archive_id": f"a-{i}",
            "role": "user",
            "content": f"归档 {i}",
            "conversation_id": "group:1",
            "timestamp": f"2026-06-01 00:00:0{i}",
            "sender_id": "100",
            "sender_name": "Alice",
        }
        for i in range(3)
    ]
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _PagedArchiveStore(archived, page_size=2)
    rt.history = _StaticRecordStore([{"role": "assistant", "content": "活跃新消息"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == [
        "归档 0",
        "归档 1",
        "归档 2",
        "活跃新消息",
    ]
    assert rt.archive.records_called is False
    assert [call["offset"] for call in rt.archive.filter_calls] == [0, 2]
    assert rt.archive.get_by_ids_calls == [["a-0", "a-1"], ["a-2"]]


@pytest.mark.asyncio
async def test_chats_load_records_without_archive(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.history = _StaticRecordStore([{"role": "user", "content": "活跃消息"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["活跃消息"]


@pytest.mark.asyncio
async def test_chats_falls_back_to_history_when_archive_fails(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _FailingRecordStore()
    rt.history = _StaticRecordStore([{"role": "user", "content": "活跃消息"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["活跃消息"]


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


def test_chats_formats_send_tool_call_readably():
    text = _format_tool_call_for_display(
        {
            "function": {
                "name": "send_group_message",
                "arguments": (
                    '{"group_id":1039163467,"targets":['
                    '{"content":"好好好 不说了","delay":0.5},'
                    '{"content":"那我先待机","delay":0.6}]}'
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
    assert "工具结果 · call-1" in tool_html
    assert "chat-event chat-event-tool" in tool_html
    assert "chat-bubble" not in tool_html
    assert "状态 stale" in tool_html
    assert "展开原文" in tool_html


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

    assert "系统 · 发送回执" in receipt_html
    assert "玖" in receipt_html
    assert "真正发出" in receipt_html
    assert "已发送 · msg_id=100" in receipt_html
    assistant_bubble = receipt_html.split("chat-record chat-message-table chat-side-left chat-bot")[-1]
    assert "未确认草稿" not in assistant_bubble
    assert "未发" not in assistant_bubble


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

    assert "系统 · 发送回执" in receipt_html
    assert "待发送/尝试 1 条" in receipt_html
    assert "chat-bubble chat-assistant" not in receipt_html


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
        {"role": "system", "content": "主动思考：本次跳过"},
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

    assert "系统 · 运行时上下文" in runtime_html
    assert "chat-event chat-event-system" in runtime_html
    assert "chat-bubble" not in runtime_html
    assert "系统" in system_html
    assert "chat-event chat-event-system" in system_html
    assert "chat-bubble" not in system_html
    assert "系统 · 发送状态" in send_status_html
    assert "send_id=send-1" in send_status_html
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
        {"role": "system", "content": "主动思考：本次跳过"},
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

    for html in (tool_html, system_html, reasoning_html):
        assert "chat-event" in html
        assert "chat-message-table" not in html
        assert "chat-side-left" not in html
        assert "chat-side-right" not in html
        assert "chat-bubble" not in html


def test_chats_renders_assistant_tool_calls_as_separate_event():
    bubbles = _render_record_bubbles(
        {
            "role": "assistant",
            "content": "准备发",
            "tool_calls": [
                {
                    "function": {
                        "name": "send_private_messages",
                        "arguments": '{"targets":[{"target_qq":123,"content":"你好"}]}',
                    }
                }
            ],
        },
        persona_name="玖",
    )

    assert len(bubbles) == 2
    assert "准备发" in bubbles[0]
    assert "内部文本" in bubbles[0]
    assert "chat-event" in bubbles[0]
    assert "工具调用" not in bubbles[0]
    assert "chat-event-tool" in bubbles[1]
    assert "chat-bubble" not in bubbles[1]
    assert "调用工具：send_private_messages" in bubbles[1]
    assert "向 123 发送消息：你好" in bubbles[1]


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
        "提交发送尝试：send_attempt_id=attempt-1；已复核到 seq 8；"
        "中断策略 interrupt_priority；忽略复核打断；原因：复核后确认旧回复仍适合"
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

    assert "调用工具：commit_send_attempt" in html
    assert "提交发送尝试：send_attempt_id=attempt-1" in html
    assert "展开原始参数" in html
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

    assert "状态 accepted" in html
    assert "send_id=send-1" in html
    assert "已发送 2 条" in html
    assert "已忽略 11 条新打断" in html
    assert "2026-06-08 10:00:00-2026-06-08 10:10:00" in html
    assert "样例：Alice: 新消息 0" in html
    assert "forced_unseen_messages" not in html
    assert "新消息 10" not in html

    page._expanded_item_ids.add("group:1\ncall-commit:tool_result")
    expanded = page._render_conversation(conv)

    assert "收起" in expanded
    assert "forced_unseen_messages" in expanded
    assert "新消息 10" in expanded


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
    page._on_detail_anchor_clicked(QtCore.QUrl("diana-chat-toggle:group%3A1%0Amsg-1%3Ainbound"))
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


def test_chats_reasoning_is_collapsible_event(qapp, tmp_paths):
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

    assert "思考过程" in html
    assert "chat-event-reasoning" in html
    assert "展开原文" in html
    assert "内部推理 raw-only" not in html

    page._expanded_item_ids.add("group:1\nturn-1:reasoning")
    expanded = page._render_conversation(conv)

    assert "收起" in expanded
    assert "内部推理 raw-only" in expanded


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
                        "arguments": '{"group_id":20002,"targets":[{"content":"发出"}]}',
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
                    "arguments": '{"targets":[{"target_qq":123,"content":"你好"}]}',
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
    assert "准备发" in text_only[0]
    assert "工具调用" not in text_only[0]
    assert len(tool_only) == 1
    assert "准备发" not in tool_only[0]
    assert "向 123 发送消息：你好" in tool_only[0]


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
                        "arguments": '{"targets":[{"target_qq":123,"content":"你好"}]}',
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

    assert "向 123 发送消息：你好" in html
    assert "工具结果 · call-1" in html
    assert "send_id=send-1" in html
    assert "工具结果 · orphan" in html
    assert html.count("工具结果 · call-1") == 1


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
                        "arguments": '{"group_id":1,"targets":[{"content":"普通"}]}',
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
    html = _render_record_html(
        {
            "role": "tool",
            "tool_call_id": "call-pending",
            "content": '{"status":"accepted","delivery":"pending","qq_visible":"pending"}',
        },
        persona_name="玖",
    )

    assert "状态 accepted" in html
    assert "投递 pending" in html
    assert "QQ 可见性待确认" in html


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


@pytest.mark.asyncio
async def test_important_memory_delete_by_id_is_exact(tmp_path):
    mgr = ImportantMemoryManager(tmp_path / "important.json")
    await mgr.load()
    await mgr.replace_all(
        [
            {"timestamp": "t1", "content": "喜欢红茶"},
            {"timestamp": "t2", "content": "也喜欢红茶蛋糕"},
        ]
    )

    deleted = await mgr.delete_by_id("t1")

    assert deleted is True
    assert [item["timestamp"] for item in mgr.items()] == ["t2"]


def test_log_detail_format_includes_exception():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("tests.demo").makeRecord(
            "tests.demo",
            logging.ERROR,
            __file__,
            1,
            "failed: %s",
            ("x",),
            exc_info=sys.exc_info(),
        )

    text = _format_record(record, single_line=False)

    assert "模块：tests.demo" in text
    assert "RuntimeError: boom" in text


def test_welcome_requires_explicit_path_choice(qapp):
    view = WelcomeStepView(WizardContext())
    errors: list[str] = []
    view.invalid_input.connect(errors.append)

    assert view.save() is False
    assert errors == ["请先选择推荐路径或自定义路径。"]


def test_tts_feature_card_keeps_configured_model_dir(qapp):
    card = _TTSFeatureCard()
    card._model_dir_edit.setText("F:/models/custom-voxcpm2")

    state = card.state()

    assert state["model_dir"] == "F:/models/custom-voxcpm2"


def test_tts_feature_card_local_reference_audio_is_optional(qapp, monkeypatch):
    import ui.wizard.step_views.features as features_module

    card = _TTSFeatureCard()
    card._check.setChecked(True)
    card._type_combo.setCurrentIndex(card._type_combo.findData("local"))
    card._ref_audio_edit.clear()
    card._prompt_edit.setText("年轻女性，温柔语气")
    monkeypatch.setattr(features_module, "_directory_has_files", lambda _path: True)

    assert card.ensure_ready(card) is True
    assert card.state()["reference_audio"] == ""


def test_model_combo_focus_does_not_reopen_popup(qapp, monkeypatch):
    combo = ModelComboBox()
    try:
        combo.add_model("deepseek-chat")
        calls = []
        monkeypatch.setattr(combo, "showPopup", lambda: calls.append("popup"))

        combo.setFocus(Qt.FocusReason.MouseFocusReason)
        qapp.processEvents()

        assert calls == []
    finally:
        combo.deleteLater()


def test_persona_creator_admin_row_buttons_are_visible_and_spaced(qapp):
    view = PersonaCreatorStepView(WizardContext())
    try:
        assert len(view._admin_rows) == 1
        first = view._admin_rows[0]
        assert first.remove_btn.isHidden()

        view._add_admin_row()
        second = view._admin_rows[1]

        assert first.remove_btn.isHidden()
        assert second.remove_btn.text() == "删除"
        assert not second.remove_btn.isHidden()
        assert second.remove_btn.width() >= 48
        assert view._admins_layout.spacing() >= 8
    finally:
        view.deleteLater()


def test_api_key_input_progress_slot_keeps_layout_height(qapp):
    widget = ApiKeyInput(allow_empty_test=True)
    try:
        widget.ensurePolished()
        widget.adjustSize()
        idle_hint = widget.sizeHint().height()

        widget.set_test_state("testing")
        widget.adjustSize()
        testing_hint = widget.sizeHint().height()

        widget.set_test_state("success", "ok")
        widget.adjustSize()
        success_hint = widget.sizeHint().height()
    finally:
        widget.deleteLater()

    assert testing_hint == idle_hint
    assert success_hint == idle_hint


def test_settings_restore_opened_config_writes_snapshot(tmp_paths):
    cfg = _minimal_root_config()
    opened = cfg.model_copy(deep=True)
    cfg.app.theme = "dark"

    class FakeStatus:
        def __init__(self):
            self.calls = []
            self.error = ""

        def set_changes(self, count: int, *, needs_restart: bool) -> None:
            self.calls.append((count, needs_restart))

        def mark_error(self, msg: str) -> None:
            self.error = msg

    class Dummy:
        def __init__(self):
            self._opened_snapshot = opened
            self._runtime = type("RuntimeStub", (), {"paths": tmp_paths, "config": cfg})()
            self._baseline = cfg.model_copy(deep=True)
            self._status = FakeStatus()
            self.refreshed = False

        def refresh(self) -> None:
            self.refreshed = True

    page = Dummy()
    SettingsPage._restore_opened_config(page)

    assert page._runtime.config.app.theme == "auto"
    assert page._status.calls[-1] == (0, True)
    assert page.refreshed is True


def test_settings_page_uses_navigation_sections(qapp, tmp_paths):
    cfg = _minimal_root_config()

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        labels = [page._settings_nav.item(i).text() for i in range(page._settings_nav.count())]
    finally:
        page.deleteLater()

    assert "软件行为" in labels
    assert "Token预算" in labels
    assert "日志与诊断" in labels
    assert "表情包" not in labels
    assert "角色" not in labels
    assert "外观" not in labels


def test_settings_page_tool_loop_reminder_replaces_legacy_max_loops(qapp, tmp_paths):
    cfg = _minimal_root_config()
    cfg.agents.chat.tool_loop_reminder_interval = 11
    cfg.agents.chat.tool_loop_final_warning_count = 3
    cfg.agents.chat.tool_loop_final_grace_loops = 2
    runtime = _dashboard_runtime(tmp_paths, cfg)
    page = SettingsPage(runtime)
    try:
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "工具循环提醒" in labels
        assert "工具轮数提醒间隔" in labels
        assert "最终警告前提醒次数" in labels
        assert "最终警告后宽限轮数" in labels
        assert "普通硬停止" in labels
        assert "工具轮数上限" not in labels
        assert "最大工具轮数" not in labels

        interval = page.findChild(QtWidgets.QSpinBox, "toolLoopReminderIntervalSpin")
        warning = page.findChild(QtWidgets.QSpinBox, "toolLoopFinalWarningCountSpin")
        grace = page.findChild(QtWidgets.QSpinBox, "toolLoopFinalGraceLoopsSpin")
        assert interval is not None
        assert warning is not None
        assert grace is not None
        assert interval.value() == 11
        assert warning.value() == 3
        assert grace.value() == 2

        interval.setValue(12)
        interval.editingFinished.emit()
        warning.setValue(4)
        warning.editingFinished.emit()
        grace.setValue(5)
        grace.editingFinished.emit()

        saved = load_config(tmp_paths)
        assert saved.agents.chat.tool_loop_reminder_interval == 12
        assert saved.agents.chat.tool_loop_final_warning_count == 4
        assert saved.agents.chat.tool_loop_final_grace_loops == 5
        assert saved.agents.chat.max_loops == 25
    finally:
        page.deleteLater()


def test_settings_provider_health_status_refreshes_without_manual_test(qapp, tmp_paths):
    from providers.health import ProviderHealth

    cfg = _minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        status = page._provider_status_labels["ds"]
        assert status.text() == "尚未检测"

        runtime.provider_health["ds"] = ProviderHealth("checking", "检测中")
        page._refresh_provider_status_labels()
        assert status.text() == "启动自动检测中"

        runtime.provider_health["ds"] = ProviderHealth("ok", "可用", latency_ms=123)
        page._refresh_provider_status_labels()
        assert status.text() == "可用 · 123ms"
    finally:
        page.deleteLater()


def test_settings_page_collapses_advanced_budget_and_napcat_options(qapp, tmp_paths):
    from ui.dashboard.settings_page import CollapsibleSection

    cfg = _minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        sections = page.findChildren(CollapsibleSection)
        titles = []
        collapsed = []
        for section in sections:
            labels = section.findChildren(QtWidgets.QLabel)
            buttons = section.findChildren(QtWidgets.QPushButton)
            if labels:
                titles.append(labels[0].text())
            if buttons:
                collapsed.append(section._body.isHidden())
                assert buttons[0].property("role") == "collapse-toggle"
                assert buttons[0].minimumWidth() == 30
                assert buttons[0].maximumWidth() == 30
                assert buttons[0].minimumHeight() == 30
                assert buttons[0].maximumHeight() == 30

        assert "NapCat 连接高级参数" in titles
        assert "上下文总预算" in titles
        assert "按工具结果预算" in titles
        assert any(collapsed)

        label_text = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "API 前等待连接" in label_text
        assert "托管进程预热" in label_text
    finally:
        page.deleteLater()


def test_settings_page_scrolls_only_right_content(qapp, tmp_paths):
    cfg = _minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        assert page._settings_scroll.widget() is page._settings_stack
        assert page._settings_scroll.parentWidget() is page
        assert page._settings_nav.parentWidget() is page
        assert page._status.parentWidget() is page
        assert page._settings_scroll.isAncestorOf(page._settings_stack)
        assert not page._settings_scroll.isAncestorOf(page._settings_nav)
        assert not page._settings_scroll.isAncestorOf(page._status)
    finally:
        page.deleteLater()


def test_settings_page_short_sections_do_not_scroll_to_blank_space(qapp, tmp_paths):
    page = SettingsPage(_dashboard_runtime(tmp_paths))
    try:
        page.resize(1014, 678)
        page.show()
        for _ in range(8):
            qapp.processEvents()

        labels = [page._settings_nav.item(i).text() for i in range(page._settings_nav.count())]

        page._settings_nav.setCurrentRow(labels.index("功能"))
        for _ in range(8):
            qapp.processEvents()
        assert page._settings_scroll.verticalScrollBar().maximum() > 0

        for section_name in ("记忆", "软件行为", "Token预算", "日志与诊断"):
            page._settings_nav.setCurrentRow(labels.index(section_name))
            for _ in range(8):
                qapp.processEvents()

            assert page._settings_scroll.verticalScrollBar().maximum() == 0
            assert page._settings_nav.verticalScrollBar().maximum() == 0
            assert page._status.parentWidget() is page
            assert not page._settings_scroll.isAncestorOf(page._status)
    finally:
        page.close()
        page.deleteLater()


def test_settings_page_content_sync_reuses_single_timer(qapp, tmp_paths):
    page = SettingsPage(_dashboard_runtime(tmp_paths))
    try:
        timers_before = page.findChildren(QtCore.QTimer)
        assert page._settings_content_sync_timer in timers_before

        for _ in range(100):
            page._schedule_settings_content_sync()

        timers_after = page.findChildren(QtCore.QTimer)
        assert timers_after == timers_before
        assert page._settings_content_sync_timer.isActive()
    finally:
        page.close()
        page.deleteLater()


def test_dashboard_settings_page_does_not_use_outer_scroll(qapp, tmp_paths):
    window = DashboardWindow(_dashboard_runtime(tmp_paths))
    try:
        window.resize(DEFAULT_LAYOUT.default_width, DEFAULT_LAYOUT.default_height)
        window.show()
        qapp.processEvents()
        window._switch_to("settings")
        for _ in range(8):
            qapp.processEvents()

        settings = window._pages["settings"]
        assert window._scroll.verticalScrollBar().maximum() == 0
        assert settings._settings_nav.verticalScrollBar().maximum() == 0
        assert settings._status.parentWidget() is settings
        assert not settings._settings_scroll.isAncestorOf(settings._status)
    finally:
        window.close()
        window.deleteLater()


def test_settings_page_features_contains_emoji_without_extra_nav(qapp, tmp_paths):
    cfg = _minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        labels = [page._settings_nav.item(i).text() for i in range(page._settings_nav.count())]
        features_row = labels.index("功能")
        page._settings_nav.setCurrentRow(features_row)
        text = "\n".join(label.text() for label in page._settings_stack.currentWidget().findChildren(QtWidgets.QLabel))
        assert "表情包" in text
        assert "管理 Debata 在聊天中可用的表情包图片" in text
    finally:
        page.deleteLater()


def test_settings_page_page_wrappers_do_not_add_trailing_stretch(qapp, tmp_paths):
    cfg = _minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        for idx in range(page._settings_stack.count()):
            wrapper = page._settings_stack.widget(idx)
            layout = wrapper.layout()
            assert layout is not None
            assert layout.count() == 1
            assert layout.itemAt(0).widget() is not None
    finally:
        page.deleteLater()


def test_settings_provider_test_model_can_use_vision_provider(qapp, tmp_paths):
    cfg = _minimal_root_config()
    cfg.providers["vision"] = ProviderConfig(preset="volcengine", api_key_id="vision_key")
    cfg.features.vision = VisionFeatureConfig(
        enabled=False,
        provider="vision",
        model="doubao-seed-1-6-vision-250815",
    )
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        assert page._agent_model_for_provider("vision") == "doubao-seed-1-6-vision-250815"
    finally:
        page.deleteLater()


@pytest.mark.asyncio
async def test_settings_adapter_test_uses_running_adapter_without_new_ws(
    qapp,
    tmp_paths,
    monkeypatch,
):
    cfg = _minimal_root_config()
    running_adapter = SimpleNamespace(name="default", is_connected=True)
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
            "adapter": running_adapter,
        },
    )()
    page = SettingsPage(runtime)
    try:
        async def fail_probe(*args, **kwargs):
            raise AssertionError("当前 Runtime 渠道已存在时不应探测端口")

        monkeypatch.setattr(SettingsPage, "_probe_tcp_port", fail_probe)
        page._on_test_adapter(cfg.adapters["default"])
        await asyncio.sleep(0)
        qapp.processEvents()

        assert page._adapter_test_status.text() == "✓ 当前 Runtime 渠道已连接"
    finally:
        page.deleteLater()


def test_dashboard_content_width_uses_viewport_not_layout_stretch(qapp):
    page = SimpleNamespace()
    page._scroll = SimpleNamespace(
        viewport=lambda: SimpleNamespace(width=lambda: 960),
    )
    page._stack = SimpleNamespace(
        _min=0,
        _max=0,
        minimumWidth=lambda: page._stack._min,
        maximumWidth=lambda: page._stack._max,
        setMinimumWidth=lambda value: setattr(page._stack, "_min", value),
        setMaximumWidth=lambda value: setattr(page._stack, "_max", value),
        updateGeometry=lambda: None,
    )

    DashboardWindow._sync_content_width(page)

    assert page._stack._min == 960
    assert page._stack._max == 960

    page._scroll = SimpleNamespace(
        viewport=lambda: SimpleNamespace(width=lambda: DEFAULT_LAYOUT.page_max_width + 600),
    )
    DashboardWindow._sync_content_width(page)

    assert page._stack._min == DEFAULT_LAYOUT.page_max_width
    assert page._stack._max == DEFAULT_LAYOUT.page_max_width


def test_overview_page_shows_usage_activity_and_provider_counts(qapp):
    class FakeUsageStore:
        def summarize(self, range_name):
            assert range_name == "today"
            return SimpleNamespace(
                request_count=3,
                prompt_tokens=1000,
                completion_tokens=200,
                reasoning_tokens=50,
                cached_tokens=800,
                cache_creation_tokens=120,
                total_tokens=1250,
                cache_hit_rate=0.8,
            )

    cfg = _minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "adapter": None,
            "config": cfg,
            "providers": {"ok": object(), "bad": object()},
            "provider_health": {
                "ok": SimpleNamespace(status="ok", latency_ms=123, message="可用"),
                "bad": SimpleNamespace(status="error", message="请求超时"),
            },
            "_hist_len": 0,
            "important": None,
            "usage_stats": FakeUsageStore(),
            "model_activity": {
                "state": "tool",
                "text": "调用工具：get_weather",
                "model": "deepseek-chat",
                "agent": "主模型",
                "tool_names": ["get_weather"],
            },
            "persona": type("Persona", (), {"name": "Mika"})(),
        },
    )()
    page = OverviewPage(runtime)
    try:
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))

        assert page._providers_card._right_label.text() == "1/2 可用"
        assert page._providers_card._right_label.maximumWidth() == 112
        assert page._providers_card._right_label.maximumHeight() == 22
        assert page._providers_card._right_label.toolTip() == "部分可用 1/2 · 请求超时"
        assert "部分可用 1/2 · 请求超时" not in labels
        assert "ok" in labels
        assert "可用 · 123ms" in labels
        assert "bad" in labels
        assert "异常 · 请求超时" in labels
        assert "渠道状态" in labels
        assert "用量统计" in labels
        assert "请求数" in labels
        assert "3" in labels
        assert "输入 token" in labels
        assert "输出 token" in labels
        assert "总 token" in labels
        assert "1,250" in labels
        assert "KV 命中 token" in labels
        assert "800" in labels
        assert "KV 写入 token" in labels
        assert "120" in labels
        assert "80.0%" in labels
        assert "主模型状态" in labels
        assert "调用工具" in labels
        assert "get_weather" in labels
        assert "累计概况" not in labels
        assert "当前角色" not in labels
        assert page._usage_card._right_label.isHidden()
    finally:
        page.deleteLater()


def test_overview_cards_keep_content_driven_height(qapp):
    page = OverviewPage(
        type(
            "RuntimeStub",
            (),
            {
                "adapter": None,
                "config": _minimal_root_config(),
                "providers": {},
                "provider_health": {},
                "_hist_len": 0,
                "important": None,
                "usage_stats": None,
                "model_activity": {},
            },
        )()
    )
    try:
        cards = [
            page._activity_card,
            page._providers_card,
            page._adapter_card,
            page._usage_card,
        ]
        for card in cards:
            assert card.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Policy.Preferred
    finally:
        page.deleteLater()


def test_dashboard_theme_apply_short_circuits_same_resolved_theme(qapp, monkeypatch):
    import ui.dashboard.main_window as main_window_module

    class FakeApp:
        def __init__(self) -> None:
            self.stylesheets: list[str] = []

        def setStyleSheet(self, qss: str) -> None:
            self.stylesheets.append(qss)

    page = SimpleNamespace(
        _theme_choice="light",
        _current_theme="light",
        _applied_theme=None,
        _theme_btn=SimpleNamespace(
            text="",
            tooltip="",
            setText=lambda value: setattr(page._theme_btn, "text", value),
            setToolTip=lambda value: setattr(page._theme_btn, "tooltip", value),
        ),
    )
    fake_app = FakeApp()
    monkeypatch.setattr(main_window_module.QApplication, "instance", lambda: fake_app)
    monkeypatch.setattr(main_window_module, "cached_qss", lambda palette: f"qss:{palette.name}")

    DashboardWindow._apply_theme(page, "light")
    DashboardWindow._apply_theme(page, "light")
    DashboardWindow._apply_theme(page, "dark")

    assert fake_app.stylesheets == ["qss:light", "qss:dark"]
    assert page._theme_choice == "dark"
    assert page._current_theme == "dark"


def test_memory_page_rag_mode_keeps_important_memory_tab(qapp):
    class FakeImportant:
        def items(self):
            return [{"timestamp": "t1", "content": "用户喜欢红茶"}]

    class FakeRagStore:
        def all_entries(self):
            return [
                RagEntry(
                    id="t1",
                    text="用户喜欢红茶",
                    vector=[0.1, 0.2],
                    meta={"timestamp": "t1"},
                )
            ]

    cfg = _minimal_root_config()
    cfg.features = FeaturesConfig(
        long_term_memory=LongTermMemoryConfig(mode="rag")
    )
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "important": FakeImportant(),
            "rag_store": FakeRagStore(),
            "embedding_service": object(),
        },
    )()
    page = MemoryPage(runtime)
    try:
        page.refresh()

        assert not page._tabs.isHidden()
        assert page._tabs.tabText(0) == "重要记忆"
        assert page._tabs.tabText(1) == "RAG 历史索引"
        assert page._title.text() == "重要记忆"
        assert "重要记忆始终启用" in page._rag_status.text()
        assert page._list.count() == 1
        assert "用户喜欢红茶" in page._list.item(0).text()
        assert not page._add_row_widget.isHidden()
        assert not page._action_row_widget.isHidden()

        page._tabs.setCurrentIndex(1)
        qapp.processEvents()

        assert page._title.text() == "RAG 历史向量索引"
        assert "索引 1 条" in page._rag_status.text()
        assert "重要记忆仍单独保存和注入" in page._rag_status.text()
        assert page._list.count() == 1
        assert page._add_row_widget.isHidden()
        assert page._action_row_widget.isHidden()
        assert page._metadata_row_widget.isHidden()
    finally:
        page.deleteLater()


def test_memory_page_rag_not_ready_copy_keeps_important_memory_enabled(qapp):
    class FakeImportant:
        def items(self):
            return [{"timestamp": "t1", "content": "用户喜欢红茶"}]

    cfg = _minimal_root_config()
    cfg.features = FeaturesConfig(
        long_term_memory=LongTermMemoryConfig(mode="rag")
    )
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "important": FakeImportant(),
            "rag_store": None,
            "embedding_service": None,
        },
    )()
    page = MemoryPage(runtime)
    try:
        page._tabs.setCurrentIndex(1)
        page.refresh()

        assert "重要记忆仍照常可用" in page._rag_status.text()
        assert "不读写重要记忆文件" not in page._rag_status.text()
    finally:
        page.deleteLater()


def test_settings_longterm_memory_copy_describes_rag_as_enhancement(qapp, tmp_paths):
    cfg = _minimal_root_config()
    cfg.features = FeaturesConfig(long_term_memory=LongTermMemoryConfig(mode="file"))
    runtime = _dashboard_runtime(tmp_paths, cfg)
    page = SettingsPage(runtime)
    calls = []
    page._save_now = lambda **kwargs: calls.append(kwargs)
    try:
        labels = [w.text() for w in page.findChildren(QtWidgets.QLabel)]
        buttons = [w.text() for w in page.findChildren(QtWidgets.QAbstractButton)]
        text = "\n".join(labels + buttons)

        assert "重要记忆始终启用" in text
        assert "RAG 历史召回增强" in text
        assert "文件模式（默认" not in text
        assert "RAG 向量检索" not in text

        rag_button = next(
            rb for rb in page.findChildren(QtWidgets.QRadioButton)
            if "启用 RAG 历史召回增强" in rb.text()
        )
        rag_button.setChecked(True)

        assert cfg.features.long_term_memory.mode == "rag"
        assert calls[-1]["change_desc"] == "long_term_memory.mode=rag"
    finally:
        page.deleteLater()


def test_settings_embedding_dialog_enables_embedding(qapp, tmp_paths, monkeypatch):
    cfg = _minimal_root_config()
    cfg.features = FeaturesConfig(
        embedding=EmbeddingFeatureConfig(
            enabled=False,
            type="api",
            provider="ds",
            api_model="old-model",
        ),
        long_term_memory=LongTermMemoryConfig(mode="rag"),
    )
    runtime = _dashboard_runtime(tmp_paths, cfg)
    page = SettingsPage(runtime)

    class FakeEmbeddingDialog:
        def __init__(self, *_args, **_kwargs):
            self.result_data = {
                "type": "api",
                "provider": "ds",
                "model": "new-model",
                "api_key": "",
            }

        def exec(self):
            return True

    monkeypatch.setattr(
        "ui.dashboard.settings_page._EmbeddingEditDialog",
        FakeEmbeddingDialog,
    )
    monkeypatch.setattr(page, "_save_now", lambda **_kwargs: None)
    label = QLabel()
    try:
        page._open_embedding_dialog(label)

        assert cfg.features.embedding.enabled is True
        assert cfg.features.embedding.provider == "ds"
        assert cfg.features.embedding.api_model == "new-model"
    finally:
        page.deleteLater()


def test_personas_page_can_build_and_save_generated_persona(qapp, tmp_path):
    from agents.persona_gen_agent import PersonaBrief

    cfg = _minimal_root_config()
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    debata_dir = personas_dir / "debata"
    debata_dir.mkdir()
    (debata_dir / "persona_prompt.py").write_text("PERSONA_PROMPT = 'x'\n", encoding="utf-8")

    class FakeSecrets:
        def get(self, key_id: str) -> str | None:
            return "sk-test" if key_id == "ds_key" else None

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "secrets": FakeSecrets(),
            "paths": type("Paths", (), {"PERSONAS_DIR": personas_dir})(),
        },
    )()
    page = PersonasPage(runtime)
    try:
        assert page._create_btn.text() == "新建角色"
        context = page._build_creator_context()
        assert context is not None
        assert context.main.api_key == "sk-test"
        assert context.main.model == "deepseek-chat"

        context.persona.source = "create"
        context.persona.active = "Mika"
        context.persona.generated_xml = "<identity>你是 Mika</identity>"
        context.persona.brief = PersonaBrief(
            name="Mika",
            gender="female",
            admins=[
                {"name": "Lily", "qq": "123456", "relation": "创作者"},
                {"name": "Robin", "qq": "654321", "relation": "朋友"},
            ],
        )
        context.admin_name = "Lily"
        context.admin_qq = "123456"

        assert page._save_generated_persona(context) == "Mika"
        saved = personas_dir / "Mika" / "persona_prompt.py"
        assert saved.exists()
        text = saved.read_text(encoding="utf-8")
        assert "<identity>你是 Mika</identity>" in text
        assert "'qq': 123456" in text
        assert "'qq': 654321" in text
        assert "'gender': 'female'" in text
    finally:
        page.deleteLater()


def test_persona_creator_dialog_wires_runtime_usage_callbacks(qapp):
    context = WizardContext()

    class RuntimeStub:
        async def _record_model_usage(self, usage, metadata):
            return None

        def _update_model_activity(self, payload):
            return None

    runtime = RuntimeStub()
    dlg = _PersonaCreatorDialog(context, runtime=runtime)
    try:
        assert dlg._creator.usage_recorder == runtime._record_model_usage
        assert dlg._creator.status_callback == runtime._update_model_activity
    finally:
        dlg.deleteLater()


def test_personas_page_selects_active_persona_on_refresh(qapp, tmp_path):
    cfg = _minimal_root_config()
    cfg.persona.active = "Mika"
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    for name in ("debata", "Mika"):
        d = personas_dir / name
        d.mkdir()
        (d / "persona_prompt.py").write_text("PERSONA_PROMPT = 'x'\n", encoding="utf-8")

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "secrets": None,
            "paths": type("Paths", (), {"PERSONAS_DIR": personas_dir})(),
        },
    )()
    page = PersonasPage(runtime)
    try:
        page.refresh()
        assert page._selected() == "Mika"
    finally:
        page.deleteLater()


def test_window_resize_edges_cover_all_sides(qapp):
    window = QWidget()
    try:
        window.resize(300, 200)

        assert _resize_edges_for_local_pos(window, QtCore.QPoint(0, 100)) & Qt.Edge.LeftEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(299, 100)) & Qt.Edge.RightEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(150, 0)) & Qt.Edge.TopEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(150, 199)) & Qt.Edge.BottomEdge
        top_left = _resize_edges_for_local_pos(window, QtCore.QPoint(0, 0))
        assert top_left & Qt.Edge.LeftEdge
        assert top_left & Qt.Edge.TopEdge
    finally:
        window.deleteLater()
