"""Persistent model usage statistics."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import orjson

from providers.base import Usage
from utils.usage_summary import (
    UsageRange,
    UsageSummary,
)
from utils.usage_summary import (
    cutoff_timestamp as _cutoff_timestamp,
)


class UsageStatsStore:
    """Append-only JSONL usage recorder.

    The file is intentionally simple and durable enough for dashboard statistics.
    It records real model API calls only; callers decide which operations count.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: list[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records = []
        if not self.path.exists():
            return
        for raw in self.path.read_bytes().splitlines():
            if not raw.strip():
                continue
            try:
                item = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if isinstance(item, dict):
                self._records.append(item)

    async def record(
        self,
        usage: Usage,
        *,
        provider: str = "",
        model: str = "",
        agent: str = "",
        operation: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        if usage.total_tokens <= 0 and usage.prompt_tokens <= 0 and usage.completion_tokens <= 0:
            return
        record = {
            "ts": time.time(),
            "provider": provider,
            "model": model,
            "agent": agent,
            "operation": operation,
            "prompt_tokens": int(usage.prompt_tokens),
            "completion_tokens": int(usage.completion_tokens),
            "reasoning_tokens": int(usage.reasoning_tokens),
            "cached_tokens": int(usage.cached_tokens),
            "cache_creation_tokens": int(usage.cache_creation_tokens),
            "total_tokens": int(
                usage.total_tokens
                or (usage.prompt_tokens + usage.completion_tokens)
            ),
        }
        if extra:
            record.update(extra)
        async with self._lock:
            if self._records is None:
                self._load_unlocked()
            self._records.append(record)  # type: ignore[union-attr]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as f:
                f.write(orjson.dumps(record))
                f.write(b"\n")

    def summarize(self, range_name: UsageRange = "today") -> UsageSummary:
        records = self._records or []
        cutoff = _cutoff_timestamp(range_name)
        summary = UsageSummary()
        for record in records:
            ts = float(record.get("ts") or 0)
            if cutoff is not None and ts < cutoff:
                continue
            summary.request_count += 1
            summary.prompt_tokens += int(record.get("prompt_tokens") or 0)
            summary.completion_tokens += int(record.get("completion_tokens") or 0)
            summary.reasoning_tokens += int(record.get("reasoning_tokens") or 0)
            summary.cached_tokens += int(record.get("cached_tokens") or 0)
            summary.cache_creation_tokens += int(record.get("cache_creation_tokens") or 0)
            summary.total_tokens += int(record.get("total_tokens") or 0)
        return summary

    @property
    def count(self) -> int:
        return len(self._records or [])

__all__ = ["UsageRange", "UsageStatsStore", "UsageSummary"]
