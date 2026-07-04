from __future__ import annotations

import asyncio

import pytest

from core.wakeup import WakeupScheduler


@pytest.mark.asyncio
async def test_wakeup_scheduler_uses_single_runner_for_many_wakeups():
    fired: list[str] = []

    async def on_fire(reminder, target=None, mode="wakeup", message_text=None):
        fired.append(reminder)

    scheduler = WakeupScheduler(on_fire)
    try:
        for index in range(20):
            await scheduler.schedule(60, f"task-{index}")

        assert scheduler.pending_count() == 20
        assert scheduler._runner_task is not None
        runner = scheduler._runner_task

        await scheduler.schedule(60, "task-later")

        assert scheduler.pending_count() == 21
        assert scheduler._runner_task is runner
        assert fired == []
    finally:
        await scheduler.cancel_all()

    assert scheduler.pending_count() == 0


@pytest.mark.asyncio
async def test_wakeup_scheduler_fires_due_items_in_order():
    fired: list[str] = []

    async def on_fire(reminder, target=None, mode="wakeup", message_text=None):
        fired.append(reminder)

    scheduler = WakeupScheduler(on_fire)
    try:
        await scheduler.schedule(2, "second")
        await scheduler.schedule(1, "first")
        await asyncio.wait_for(_wait_until(lambda: fired == ["first"]), timeout=1.5)
        assert scheduler.pending_count() == 1
        await asyncio.wait_for(_wait_until(lambda: fired == ["first", "second"]), timeout=1.5)
        assert scheduler.pending_count() == 0
    finally:
        await scheduler.cancel_all()


async def _wait_until(predicate):
    while not predicate():
        await asyncio.sleep(0.01)
