"""P2/体验修复的轻量回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
QApplication = QtWidgets.QApplication

from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
)
from core.chat_timeline import ChatTimelineMessage
from ui.dashboard.chats_page import ChatsPage


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
