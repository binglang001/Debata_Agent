from __future__ import annotations

import asyncio

import pytest

from memory import DebataHistoryStore, HistoryManager


@pytest.mark.asyncio
async def test_history_manager_uses_injected_debata_store(tmp_path):
    store = DebataHistoryStore(tmp_path / "debata.db", "yuexi")
    history = HistoryManager(tmp_path / "history.jsonl", store=store)
    appended_batches = []

    async def on_append(records: list[dict]) -> None:
        appended_batches.append(records)

    history.on_append(on_append)

    await history.load()
    await history.add_user_message(
        "你好",
        metadata={"source": "unit"},
        conversation_id="private:1",
    )
    await history.add_records(
        [
            {"role": "assistant", "content": "收到"},
            {"role": "tool", "tool_call_id": "tc_1", "content": "ok"},
        ],
        conversation_id="private:1",
    )

    for _ in range(20):
        if len(appended_batches) == 2:
            break
        await asyncio.sleep(0.01)

    expected = [
        {
            "role": "user",
            "content": "你好",
            "metadata": {"source": "unit"},
            "conversation_id": "private:1",
        },
        {"role": "assistant", "content": "收到", "conversation_id": "private:1"},
        {
            "role": "tool",
            "tool_call_id": "tc_1",
            "content": "ok",
            "conversation_id": "private:1",
        },
    ]
    assert await history.records() == expected
    assert await history.records_for_conversation("private:1") == expected
    assert await history.length() == 3
    assert appended_batches == [[expected[0]], expected[1:]]

    reloaded = HistoryManager(
        tmp_path / "history.jsonl",
        store=DebataHistoryStore(tmp_path / "debata.db", "yuexi"),
    )
    assert await reloaded.load(force_reload=True) == expected
    assert not (tmp_path / "history.jsonl").exists()
