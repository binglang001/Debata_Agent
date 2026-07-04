"""聊天页刷新与防抖回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio

import pytest

import ui.dashboard.chats_page as chats_page_module

from tests.test_dashboard_p2 import (
    _FakeTimeline,
    _pump_dashboard_events,
    _refresh_test_chats_page,
    _wait_for_dashboard_condition,
)

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
