from __future__ import annotations

import time

import pytest

from core.usage_stats import UsageStatsStore
from providers.base import Usage


@pytest.mark.asyncio
async def test_usage_stats_records_and_summarizes_ranges(tmp_path, monkeypatch):
    now = time.mktime((2026, 6, 4, 12, 0, 0, 3, 155, -1))
    monkeypatch.setattr(time, "time", lambda: now)

    store = UsageStatsStore(tmp_path / "usage.jsonl")
    await store.load()
    await store.record(
        Usage(
            prompt_tokens=100,
            completion_tokens=20,
            reasoning_tokens=5,
            cached_tokens=80,
            cache_creation_tokens=10,
            total_tokens=120,
        ),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="agent_loop",
    )

    summary = store.summarize("today")

    assert summary.request_count == 1
    assert summary.prompt_tokens == 100
    assert summary.completion_tokens == 20
    assert summary.reasoning_tokens == 5
    assert summary.cached_tokens == 80
    assert summary.cache_creation_tokens == 10
    assert summary.total_tokens == 120
    assert summary.cache_hit_rate == 0.8

    reloaded = UsageStatsStore(tmp_path / "usage.jsonl")
    await reloaded.load()
    assert reloaded.summarize("all").request_count == 1


@pytest.mark.asyncio
async def test_usage_stats_records_extra_kv_diagnostics(tmp_path):
    store = UsageStatsStore(tmp_path / "usage.jsonl")
    await store.load()

    await store.record(
        Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11),
        provider="deepseek_main",
        model="deepseek-chat",
        agent="主模型",
        operation="agent_loop",
        extra={"kv_message_count": 3, "kv_prefix_8k_hash": "abc123"},
    )

    text = (tmp_path / "usage.jsonl").read_text(encoding="utf-8")
    assert '"kv_message_count":3' in text
    assert '"kv_prefix_8k_hash":"abc123"' in text
