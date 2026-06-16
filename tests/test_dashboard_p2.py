"""P2/体验修复的轻量回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
QApplication = QtWidgets.QApplication
Qt = QtCore.Qt

import ui.dashboard.chats_page as chats_page_module
from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
)
from core.chat_timeline import ChatTimelineMessage, ChatTimelineStore
from ui.dashboard.chats_page import (
    DEFAULT_VISIBLE_RECORD_LIMIT,
    EVENT_STORE_CHAT_PAGE_EVENT_TYPES,
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
from ui.theme import palette_for_theme


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


class _FakeEventStore:
    def __init__(self, events=None, *, fail: bool = False, include_mismatched: bool = False):
        self._events = list(events or [])
        self.fail = fail
        self.include_mismatched = include_mismatched
        self.calls = []
        self.wait_projected_calls = []

    async def wait_projected(self, event_id=None, timeout=None):
        self.wait_projected_calls.append({"event_id": event_id, "timeout": timeout})
        return True

    async def events_by_type(
        self,
        event_type,
        *,
        limit=100,
        after_event_id=None,
        before_event_id=None,
        order="asc",
    ):
        self.calls.append(
            {
                "event_type": event_type,
                "limit": limit,
                "after_event_id": after_event_id,
                "before_event_id": before_event_id,
                "order": order,
            }
        )
        if self.fail:
            raise RuntimeError("event store failed")
        selected = [event for event in self._events if event.get("event_type") == event_type]
        if self.include_mismatched:
            selected.extend(
                event for event in self._events if event.get("event_type") != event_type
            )
        selected = sorted(
            selected,
            key=lambda event: int(event.get("event_id") or 0),
            reverse=order == "desc",
        )
        return [dict(event) for event in selected[: int(limit)]]


class _FakeTimeline:
    def __init__(self):
        self.listeners = []
        self.unsubscribe_calls = 0

    def subscribe(self, listener):
        self.listeners.append(listener)
        active = True

        def unsubscribe():
            nonlocal active
            if not active:
                return
            active = False
            self.unsubscribe_calls += 1
            if listener in self.listeners:
                self.listeners.remove(listener)

        return unsubscribe

    def snapshot(self):
        return {}

    def emit(self):
        for listener in list(self.listeners):
            listener(None)


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


def _refresh_test_chats_page(tmp_paths, timeline=None) -> ChatsPage:
    rt = _dashboard_runtime(tmp_paths)
    if timeline is not None:
        rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    history = rt.history
    rt.history = None
    page = ChatsPage(rt)
    rt.history = history
    page._timer.stop()
    page._refresh_debounce_timer.stop()
    page._search_debounce_timer.stop()
    page._refresh_pending = False
    page._refresh_debounce_timer.setInterval(0)
    return page


async def _pump_dashboard_events(qapp, rounds: int = 1) -> None:
    for _ in range(rounds):
        qapp.processEvents()
        await asyncio.sleep(0)


async def _wait_for_dashboard_condition(qapp, condition, *, rounds: int = 50) -> None:
    for _ in range(rounds):
        if condition():
            return
        await _pump_dashboard_events(qapp)
    assert condition()


def _timeline_record(
    *,
    conversation_id: str,
    direction: str,
    text: str,
    msg_id: str,
    timestamp: float,
    time_text: str,
    sender_name: str = "用户",
    sender_id: str = "10001",
) -> ChatTimelineMessage:
    return ChatTimelineMessage(
        conversation_id=conversation_id,
        direction=direction,
        timestamp=timestamp,
        time_text=time_text,
        sender_name=sender_name,
        sender_id=sender_id,
        target_id=conversation_id.split(":", 1)[1],
        group_id=None,
        msg_id=msg_id,
        text=text,
        raw_message=text,
    )


def _qq_event(
    event_id: int,
    event_type: str,
    *,
    conversation_id: str = "private:10001",
    content: str = "消息",
    msg_id: str = "m-1",
    timestamp_unix: float = 1_780_000_001.0,
    payload: dict | None = None,
):
    base_payload = {
        "conversation_id": conversation_id,
        "content": content,
        "msg_id": msg_id,
        "timestamp_unix": timestamp_unix,
    }
    if event_type == "qq_message_received":
        base_payload.update(
            {
                "direction": "inbound",
                "user_id": "10001",
                "sender_name": "用户",
                "target_id": "10001",
                "self_id": "999",
            }
        )
    elif event_type == "qq_message_sent":
        base_payload.update(
            {
                "direction": "outbound",
                "target_scope": conversation_id.split(":", 1)[0],
                "target_id": conversation_id.split(":", 1)[1],
                "self_id": "999",
            }
        )
    if payload:
        base_payload.update(payload)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "conversation_id": conversation_id,
        "source": "fake",
        "external_id": msg_id,
        "timestamp_unix": timestamp_unix,
        "payload": base_payload,
    }


def _runtime_event(
    event_id: int,
    event_type: str,
    *,
    conversation_id: str = "private:10001",
    payload: dict | None = None,
    tool_call_id: str | None = None,
    external_id: str | None = None,
):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "conversation_id": conversation_id,
        "source": "fake",
        "external_id": external_id,
        "tool_call_id": tool_call_id,
        "timestamp_unix": 1_780_000_001.0 + event_id,
        "payload": dict(payload or {}),
    }


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
async def test_chats_load_debug_metrics_do_not_log_message_body(tmp_paths, caplog):
    rt = _dashboard_runtime(tmp_paths)
    rt.history = _StaticRecordStore(
        [
            {
                "role": "system",
                "content": "history-secret-body",
                "conversation_id": "private:debug",
            }
        ]
    )
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                1,
                "qq_message_received",
                conversation_id="private:debug",
                content="event-secret-body",
                msg_id="event-debug",
            )
        ]
    )
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:debug",
            direction="outbound",
            text="timeline-secret-body",
            msg_id="timeline-debug",
            timestamp=1_780_000_002.0,
            time_text="2026-06-08 23:07:20",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _PagedArchiveStore(
        [
            {
                "archive_id": "archive-debug",
                "role": "user",
                "content": "archive-secret-body",
                "conversation_id": "private:debug",
                "timestamp": "2026-06-08 23:07:19",
                "sender_id": "100",
                "sender_name": "Alice",
            }
        ],
        page_size=1,
    )

    with caplog.at_level(logging.DEBUG, logger="ui.dashboard.chats_page"):
        records = await _load_chat_page_records(rt)

    assert len(records) == 4
    log_text = caplog.text
    assert "对话页记录加载指标" in log_text
    assert "history_ms=" in log_text
    assert "event_store_ms=" in log_text
    assert "timeline_ms=" in log_text
    assert "archive_ms=" in log_text
    assert "merge_tag_ms=" in log_text
    assert "history_records=1" in log_text
    assert "event_store_records=1" in log_text
    assert "timeline_records=1" in log_text
    assert "archive_records=1" in log_text
    assert "total_records=4" in log_text
    assert "history-secret-body" not in log_text
    assert "event-secret-body" not in log_text
    assert "timeline-secret-body" not in log_text
    assert "archive-secret-body" not in log_text


@pytest.mark.asyncio
async def test_chats_loads_timeline_without_archive(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="实时发出",
            msg_id="tl-1",
            timestamp=1_780_000_001.0,
            time_text="2026-06-08 23:07:19",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["实时发出"]
    assert records[0]["_source"] == "chat_timeline"


@pytest.mark.asyncio
async def test_chats_loads_event_store_qq_records_before_timeline_and_archive_duplicates(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                1,
                "qq_message_received",
                content="EventStore 入站",
                msg_id="same-in",
            ),
            _qq_event(
                2,
                "qq_message_sent",
                content="EventStore 出站",
                msg_id="same-out",
            ),
        ]
    )
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="timeline 重复入站",
            msg_id="same-in",
            timestamp=1_780_000_011.0,
            time_text="2026-06-08 23:07:21",
        )
    )
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="timeline 新消息",
            msg_id="timeline-only",
            timestamp=1_780_000_012.0,
            time_text="2026-06-08 23:07:22",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _StaticRecordStore(
        [
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "archive 重复入站",
                "msg_id": "same-in",
            },
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "archive 旧消息",
                "msg_id": "archive-only",
            },
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {"role": "user", "content": "history 普通聊天不应作为气泡主来源"},
            {"role": "system", "content": "系统补充"},
        ]
    )

    records = await _load_chat_page_records(rt)

    assert [call["event_type"] for call in rt.event_store.calls] == list(
        EVENT_STORE_CHAT_PAGE_EVENT_TYPES
    )
    assert all(call["limit"] > 0 and call["order"] == "desc" for call in rt.event_store.calls)
    assert rt.event_store.wait_projected_calls == []
    by_msg_id = {
        item.get("msg_id"): item
        for item in records
        if item.get("msg_id")
    }
    assert by_msg_id["same-in"]["_source"] == "event_store"
    assert by_msg_id["same-in"]["content"] == "EventStore 入站"
    assert by_msg_id["same-out"]["_source"] == "event_store"
    assert by_msg_id["timeline-only"]["_source"] == "chat_timeline"
    assert by_msg_id["archive-only"]["content"] == "archive 旧消息"
    assert "archive 重复入站" not in [item["content"] for item in records]
    assert "history 普通聊天不应作为气泡主来源" not in [item["content"] for item in records]
    assert "系统补充" in [item["content"] for item in records]


@pytest.mark.asyncio
async def test_chats_mixed_sources_sort_by_layer_not_event_id_as_timestamp(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                1,
                "qq_message_received",
                content="EventStore 已投影",
                msg_id="event-only",
                timestamp_unix=1_780_000_021.0,
            ),
        ]
    )
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="timeline pending",
            msg_id="timeline-only",
            timestamp=1_780_000_022.0,
            time_text="2026-06-08 23:07:22",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _StaticRecordStore(
        [
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "archive cold",
                "msg_id": "archive-only",
                "timestamp": "2026-06-08 23:07:20",
            },
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "role": "system",
                "conversation_id": "private:10001",
                "content": "history fallback without timestamp",
            },
        ]
    )

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [item.text for item in items] == [
        "archive cold",
        "EventStore 已投影",
        "timeline pending",
        "history fallback without timestamp",
    ]
    event_record = next(item for item in records if item.get("_source") == "event_store")
    assert event_record["_sort_layer"] == "event_store"
    assert event_record["_sort_kind"] == "event_id"
    assert event_record["_sort_value"] == 1.0
    assert "_sort_ts" not in event_record
    assert rt.event_store.wait_projected_calls == []


@pytest.mark.asyncio
async def test_chats_event_store_failure_falls_back_to_existing_sources(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(fail=True)
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="inbound",
            text="实时 fallback",
            msg_id="tl-fallback",
            timestamp=1_780_000_001.0,
            time_text="2026-06-08 23:07:19",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.archive = _StaticRecordStore(
        [
            {
                "role": "user",
                "direction": "inbound",
                "conversation_id": "private:10001",
                "content": "归档 fallback",
                "msg_id": "arch-fallback",
            }
        ]
    )
    rt.history = _StaticRecordStore([{"role": "system", "content": "系统 fallback"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == [
        "归档 fallback",
        "实时 fallback",
        "系统 fallback",
    ]


@pytest.mark.asyncio
async def test_chats_event_store_ignores_non_qq_events(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            {
                "event_id": 1,
                "event_type": "history_record_appended",
                "conversation_id": "private:10001",
                "payload": {"content": "不应投影"},
            },
            _qq_event(
                2,
                "qq_message_received",
                content="应投影",
                msg_id="visible-in",
            ),
        ],
        include_mismatched=True,
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["应投影"]
    assert records[0]["event_id"] == 2


@pytest.mark.asyncio
async def test_chats_event_store_projects_received_and_sent_fields_by_event_id_order(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(
                2,
                "qq_message_sent",
                conversation_id="private:10001",
                content="后发送",
                msg_id="out-1",
                timestamp_unix=100.0,
                payload={"target_scope": "private", "target_id": "10001", "self_id": "999"},
            ),
            _qq_event(
                1,
                "qq_message_received",
                conversation_id="group:20002",
                content="先收到",
                msg_id="in-1",
                timestamp_unix=200.0,
                payload={
                    "group_id": "20002",
                    "user_id": "10001",
                    "sender_name": "Alice",
                    "target_id": "20002",
                    "self_id": "999",
                },
            ),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["先收到", "后发送"]
    received, sent = records
    assert received["id"] == "event:1"
    assert received["event_id"] == 1
    assert received["role"] == "user"
    assert received["direction"] == "inbound"
    assert received["qq_visible"] is True
    assert received["conversation_id"] == "group:20002"
    assert received["msg_id"] == "in-1"
    assert received["_source"] == "event_store"
    assert received["sender_name"] == "Alice"
    assert received["user_id"] == "10001"
    assert received["target_id"] == "20002"
    assert received["group_id"] == "20002"
    assert received["self_id"] == "999"
    assert received["_sort_layer"] == "event_store"
    assert received["_sort_kind"] == "event_id"
    assert received["_sort_value"] == 1.0
    assert "_sort_ts" not in received
    assert sent["id"] == "event:2"
    assert sent["role"] == "assistant"
    assert sent["direction"] == "outbound"
    assert sent["conversation_id"] == "private:10001"
    assert sent["msg_id"] == "out-1"
    assert sent["target_id"] == "10001"
    assert sent["self_id"] == "999"


@pytest.mark.asyncio
async def test_chats_event_store_runtime_events_interleave_with_qq_by_event_id(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(1, "qq_message_received", content="用户先说", msg_id="in-1"),
            _runtime_event(
                2,
                "tool_call_started",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-write",
                    "args_keys": ["content", "path"],
                    "args_length": 42,
                    "args_preview": '{"path":"result.md"}',
                    "loop": 1,
                    "step": 1,
                },
                tool_call_id="tc-write",
            ),
            _qq_event(3, "qq_message_received", content="用户中途补充", msg_id="in-2"),
            _runtime_event(
                4,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-write",
                    "ok": True,
                    "result_keys": ["ok", "path"],
                    "result_length": 64,
                    "result_hash": "a" * 64,
                    "result_preview": '{"ok":true}',
                },
                tool_call_id="tc-write",
            ),
            _qq_event(5, "qq_message_sent", content="随后回复", msg_id="out-1"),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [(item.get("event_id"), item.get("event_type")) for item in records] == [
        (1, "qq_message_received"),
        (2, "tool_call_started"),
        (3, "qq_message_received"),
        (4, "tool_result_received"),
        (5, "qq_message_sent"),
    ]
    items = normalize_history_records(records, persona_name="Debata")
    assert [item.kind for item in items] == [
        "inbound_message",
        "tool_call",
        "inbound_message",
        "tool_result",
        "outbound_message",
    ]
    tool_item = items[1]
    result_item = items[3]
    assert tool_item.related_tool_call_id == "tc-write"
    assert tool_item.tool_results == []
    assert result_item.related_tool_call_id == "tc-write"
    assert "write_file" in tool_item.summary
    assert items[2].text == "用户中途补充"
    assert "result_hash" in result_item.text


@pytest.mark.asyncio
async def test_chats_event_store_runtime_send_and_system_events_share_event_id_axis(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _qq_event(1, "qq_message_received", content="用户先说", msg_id="in-1"),
            _runtime_event(
                2,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-axis"},
                tool_call_id="tc-axis",
            ),
            _qq_event(3, "qq_message_received", content="用户补充", msg_id="in-2"),
            _runtime_event(
                4,
                "send_message_started",
                payload={
                    "send_id": "send-axis",
                    "status": "started",
                    "order": 0,
                    "target_conversation_id": "private:10001",
                    "content_length": 6,
                },
                external_id="send-axis",
            ),
            _runtime_event(
                5,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-axis",
                    "ok": True,
                    "result_hash": "e" * 64,
                },
                tool_call_id="tc-axis",
            ),
            {
                "event_id": 6,
                "event_type": "history_truncated",
                "source": "fake",
                "timestamp_unix": 1_780_000_006.0,
                "payload": {"cut_point": 120, "remaining_count": 80},
            },
            _qq_event(7, "qq_message_sent", content="最终回复", msg_id="out-1"),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [(item.get("event_id"), item.get("event_type")) for item in records] == [
        (1, "qq_message_received"),
        (2, "tool_call_started"),
        (3, "qq_message_received"),
        (4, "send_message_started"),
        (5, "tool_result_received"),
        (6, "history_truncated"),
        (7, "qq_message_sent"),
    ]
    assert [item.kind for item in items] == [
        "inbound_message",
        "tool_call",
        "inbound_message",
        "system_event",
        "tool_result",
        "system_event",
        "outbound_message",
    ]
    assert all(record["_sort_layer"] == "event_store" for record in records)
    assert all(record["_sort_kind"] == "event_id" for record in records)
    assert [record["_sort_value"] for record in records] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    assert "发送消息开始" in items[3].summary
    assert "历史截断" in items[5].summary
    assert "截断点 120" in items[5].text
    assert rt.event_store.wait_projected_calls == []


@pytest.mark.asyncio
async def test_chats_event_store_runtime_events_dedupe_semantic_duplicates(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-dup"},
                tool_call_id="tc-dup",
            ),
            _runtime_event(
                2,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-dup"},
                tool_call_id="tc-dup",
            ),
            _runtime_event(
                3,
                "send_batch_accepted",
                payload={"send_id": "send-dup", "status": "accepted"},
                external_id="send-dup",
            ),
            _runtime_event(
                4,
                "send_batch_accepted",
                payload={"send_id": "send-dup", "status": "accepted"},
                external_id="send-dup",
            ),
            _runtime_event(
                5,
                "send_message_started",
                payload={
                    "send_id": "send-dup",
                    "status": "started",
                    "order": 0,
                    "target_conversation_id": "private:10001",
                },
                external_id="send-dup",
            ),
            _runtime_event(
                6,
                "send_message_started",
                payload={
                    "send_id": "send-dup",
                    "status": "started",
                    "order": 0,
                    "target_conversation_id": "private:10001",
                },
                external_id="send-dup",
            ),
            _runtime_event(
                7,
                "send_message_started",
                payload={
                    "send_id": "send-dup",
                    "status": "started",
                    "order": 1,
                    "target_conversation_id": "private:10001",
                },
                external_id="send-dup",
            ),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)

    assert [(record["event_id"], record["event_type"]) for record in records] == [
        (1, "tool_call_started"),
        (3, "send_batch_accepted"),
        (5, "send_message_started"),
        (7, "send_message_started"),
    ]


@pytest.mark.asyncio
async def test_chats_event_store_send_and_system_runtime_events_are_displayable(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "send_batch_accepted",
                payload={
                    "send_id": "send-1",
                    "status": "accepted",
                    "source_tool": "send_private_messages",
                    "counts": {"messages": 2, "conversations": 1},
                    "conversation_ids": ["private:10001"],
                },
                external_id="send-1",
            ),
            _runtime_event(
                2,
                "send_message_succeeded",
                payload={
                    "send_id": "send-1",
                    "status": "succeeded",
                    "msg_id": "msg-1",
                    "target_conversation_id": "private:10001",
                    "content_length": 12,
                    "content_hash": "b" * 64,
                },
                external_id="send-1",
            ),
            _runtime_event(
                3,
                "system_note_recorded",
                payload={
                    "role": "system",
                    "conversation_id": "private:10001",
                    "content_length": 18,
                    "content_hash": "c" * 64,
                    "preview": "系统提示预览",
                },
            ),
        ]
    )
    rt.history = _StaticRecordStore([])

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [item.kind for item in items] == ["system_event", "system_event", "system_event"]
    detail = "\n".join(item.text for item in items)
    assert "send_id=send-1" in detail
    assert "msg_id=msg-1" in detail
    assert "counts messages=2, conversations=1" in detail
    assert "内容长度 12" in detail
    assert "内容hash=" + "b" * 64 in detail
    assert "预览：系统提示预览" in detail
    assert all(item.kind not in {"inbound_message", "outbound_message"} for item in items)


@pytest.mark.asyncio
async def test_chats_event_store_runtime_events_dedupe_history_runtime_fallback(tmp_paths):
    system_content = "系统补充"
    system_hash = hashlib.sha256(system_content.encode("utf-8")).hexdigest()
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [
            _runtime_event(
                1,
                "tool_call_started",
                payload={"tool_name": "write_file", "tool_call_id": "tc-1"},
                tool_call_id="tc-1",
            ),
            _runtime_event(
                2,
                "tool_result_received",
                payload={
                    "tool_name": "write_file",
                    "tool_call_id": "tc-1",
                    "ok": True,
                    "result_hash": "d" * 64,
                },
                tool_call_id="tc-1",
            ),
            _runtime_event(
                3,
                "send_receipt_recorded",
                payload={"send_id": "send-1", "status": "succeeded"},
                external_id="send-1",
            ),
            _runtime_event(
                4,
                "system_note_recorded",
                payload={
                    "conversation_id": "private:10001",
                    "content_length": len(system_content),
                    "content_hash": system_hash,
                    "preview": system_content,
                },
            ),
        ]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "role": "assistant",
                "content": "",
                "conversation_id": "private:10001",
                "tool_calls": [
                    {
                        "id": "tc-1",
                        "function": {"name": "write_file", "arguments": '{"path":"full.md"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-1",
                "conversation_id": "private:10001",
                "content": '{"ok":true,"content":"完整工具返回"}',
            },
            {
                "role": "system",
                "conversation_id": "private:10001",
                "content": '<send_receipt>\n{"send_id":"send-1","status":"succeeded"}\n</send_receipt>',
            },
            {"role": "system", "conversation_id": "private:10001", "content": system_content},
            {"role": "system", "conversation_id": "private:10001", "content": "旧 history fallback"},
        ]
    )

    records = await _load_chat_page_records(rt)

    assert "完整工具返回" not in json.dumps(records, ensure_ascii=False)
    assert all(record.get("_source") == "event_store" for record in records[:4])
    assert records[-1]["content"] == "旧 history fallback"
    assert records[-1].get("_source") is None


@pytest.mark.asyncio
async def test_chats_history_only_tool_records_remain_low_priority_fallback(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.event_store = _FakeEventStore(
        [_qq_event(1, "qq_message_received", content="EventStore 新消息", msg_id="event-in")]
    )
    rt.history = _StaticRecordStore(
        [
            {
                "role": "assistant",
                "content": "",
                "conversation_id": "private:10001",
                "tool_calls": [
                    {
                        "id": "tc-old",
                        "function": {"name": "read_file", "arguments": '{"path":"old.md"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-old",
                "conversation_id": "private:10001",
                "content": '{"ok":true,"content":"旧工具返回"}',
            },
        ]
    )

    records = await _load_chat_page_records(rt)
    items = normalize_history_records(records, persona_name="Debata")

    assert [record.get("_sort_layer") for record in records] == [
        "event_store",
        "history",
        "history",
    ]
    assert [item.kind for item in items] == ["inbound_message", "tool_call"]
    assert items[0].text == "EventStore 新消息"
    assert items[1].related_tool_call_id == "tc-old"
    assert items[1].tool_results[0].text == '{"ok":true,"content":"旧工具返回"}'


@pytest.mark.asyncio
async def test_chats_render_uses_timeline_outbound_when_history_has_not_flushed(qapp, tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    timeline = ChatTimelineStore()
    timeline.append(
        _timeline_record(
            conversation_id="private:10001",
            direction="outbound",
            text="history 还没有的实时回复",
            msg_id="tl-out-1",
            timestamp=1_780_000_001.0,
            time_text="2026-06-08 23:07:19",
            sender_name="我",
            sender_id="999",
        )
    )
    rt.pipeline = SimpleNamespace(chat_timeline=timeline)
    rt.history = _StaticRecordStore([{"role": "system", "content": "系统补充"}])
    page = ChatsPage(rt)

    records = await _load_chat_page_records(rt)
    conv = next(item for item in _group_records_by_conversation(records) if item["key"] == "private:10001")
    html = page._render_conversation(conv)

    assert "chat-record chat-message-table chat-side-left chat-bot" in html
    assert "history 还没有的实时回复" in html
    assert "已发送 · msg_id=tl-out-1" in html


@pytest.mark.asyncio
async def test_chats_falls_back_to_history_when_archive_fails(tmp_paths):
    rt = _dashboard_runtime(tmp_paths)
    rt.archive = _FailingRecordStore()
    rt.history = _StaticRecordStore([{"role": "system", "content": "活跃系统事件"}])

    records = await _load_chat_page_records(rt)

    assert [item["content"] for item in records] == ["活跃系统事件"]


@pytest.mark.asyncio
async def test_chats_refresh_debounce_starts_one_load_for_burst(qapp, tmp_paths, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def load_records(_rt):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return [{"role": "user", "content": "刷新结果", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        page.refresh()
        page.refresh()
        await _wait_for_dashboard_condition(qapp, started.is_set)

        task = page._refresh_task
        assert task is not None
        assert calls == 1

        release.set()
        await task

        assert calls == 1
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_refresh_pending_collapses_inflight_burst(qapp, tmp_paths, monkeypatch):
    releases: list[asyncio.Event] = []
    calls: list[int] = []

    async def load_records(_rt):
        index = len(calls)
        calls.append(index)
        release = asyncio.Event()
        releases.append(release)
        await release.wait()
        return [{"role": "user", "content": f"刷新 {index}", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 1)
        first_task = page._refresh_task
        assert first_task is not None

        page.refresh()
        page.refresh()
        page.refresh()

        assert page._refresh_pending is True
        assert calls == [0]

        releases[0].set()
        await first_task
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 2)

        second_task = page._refresh_task
        assert second_task is not None
        await _pump_dashboard_events(qapp, rounds=3)
        assert calls == [0, 1]

        releases[1].set()
        await second_task

        assert [item["content"] for item in page._records] == ["刷新 1"]
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_refresh_generation_skips_stale_pending_result(qapp, tmp_paths, monkeypatch):
    first_release = asyncio.Event()
    calls = 0

    async def load_records(_rt):
        nonlocal calls
        calls += 1
        if calls == 1:
            await first_release.wait()
            return [{"role": "user", "content": "旧结果", "conversation_id": "private:1"}]
        return [{"role": "user", "content": "新结果", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        await _wait_for_dashboard_condition(qapp, lambda: calls == 1)
        first_task = page._refresh_task
        assert first_task is not None

        page.refresh()
        first_release.set()
        await first_task

        assert [item["content"] for item in page._records] != ["旧结果"]

        await _wait_for_dashboard_condition(
            qapp,
            lambda: calls == 2 and page._refresh_task is None,
        )

        assert [item["content"] for item in page._records] == ["新结果"]
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_refresh_exception_does_not_block_next_refresh(qapp, tmp_paths, monkeypatch):
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    calls = 0

    async def load_records(_rt):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await first_release.wait()
            raise RuntimeError("load failed")
        second_started.set()
        await second_release.wait()
        return [{"role": "user", "content": "恢复刷新", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    page = _refresh_test_chats_page(tmp_paths)
    try:
        page.refresh()
        await _wait_for_dashboard_condition(qapp, first_started.is_set)
        first_task = page._refresh_task
        assert first_task is not None
        first_release.set()
        await first_task

        assert page._refresh_task is None
        assert page._refresh_pending is False

        page.refresh()
        await _wait_for_dashboard_condition(qapp, second_started.is_set)
        second_task = page._refresh_task
        assert second_task is not None
        second_release.set()
        await second_task

        assert calls == 2
        assert [item["content"] for item in page._records] == ["恢复刷新"]
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_timeline_notification_schedules_debounced_refresh(qapp, tmp_paths):
    timeline = _FakeTimeline()
    page = _refresh_test_chats_page(tmp_paths, timeline)
    page._refresh_debounce_timer.setInterval(1000)
    try:
        generation = page._refresh_generation

        timeline.emit()

        assert page._refresh_generation == generation
        assert not page._refresh_debounce_timer.isActive()

        await _pump_dashboard_events(qapp)

        assert page._refresh_generation == generation + 1
        assert page._refresh_debounce_timer.isActive()
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page._unsubscribe_chat_timeline()
        page.deleteLater()


@pytest.mark.asyncio
async def test_chats_timeline_notification_uses_existing_refresh_single_flight(
    qapp,
    tmp_paths,
    monkeypatch,
):
    releases: list[asyncio.Event] = []
    calls: list[int] = []

    async def load_records(_rt):
        index = len(calls)
        calls.append(index)
        release = asyncio.Event()
        releases.append(release)
        await release.wait()
        return [{"role": "user", "content": f"刷新 {index}", "conversation_id": "private:1"}]

    monkeypatch.setattr(chats_page_module, "_load_chat_page_records", load_records)
    timeline = _FakeTimeline()
    page = _refresh_test_chats_page(tmp_paths, timeline)
    try:
        timeline.emit()
        timeline.emit()
        timeline.emit()
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 1)
        first_task = page._refresh_task
        assert first_task is not None

        timeline.emit()
        timeline.emit()
        await _pump_dashboard_events(qapp, rounds=3)

        assert calls == [0]
        assert page._refresh_pending is True

        releases[0].set()
        await first_task
        await _wait_for_dashboard_condition(qapp, lambda: len(calls) == 2)
        second_task = page._refresh_task
        assert second_task is not None

        releases[1].set()
        await second_task

        assert calls == [0, 1]
    finally:
        for release in releases:
            release.set()
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page._unsubscribe_chat_timeline()
        page.deleteLater()


def test_chats_timeline_subscription_switches_when_runtime_changes(qapp, tmp_paths):
    first_timeline = _FakeTimeline()
    second_timeline = _FakeTimeline()
    page = _refresh_test_chats_page(tmp_paths, first_timeline)
    try:
        assert len(first_timeline.listeners) == 1

        page._runtime.pipeline = SimpleNamespace(chat_timeline=second_timeline)
        page.refresh()

        assert first_timeline.listeners == []
        assert first_timeline.unsubscribe_calls == 1
        assert len(second_timeline.listeners) == 1

        page._unsubscribe_chat_timeline()

        assert second_timeline.listeners == []
        assert second_timeline.unsubscribe_calls == 1
    finally:
        page._timer.stop()
        page._refresh_debounce_timer.stop()
        page._unsubscribe_chat_timeline()
        page.deleteLater()


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
                "content": "",
                "reasoning_content": "内部推理 raw-only",
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

    assert "状态 accepted" in items[0].summary
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
