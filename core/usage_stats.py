"""Persistent model usage statistics."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import orjson

from providers.base import Usage

UsageRange = Literal["today", "7d", "30d", "all"]


@dataclass(slots=True)
class UsageSummary:
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens


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


def _cutoff_timestamp(range_name: UsageRange) -> float | None:
    now = time.time()
    if range_name == "today":
        local = time.localtime(now)
        start = time.struct_time(
            (
                local.tm_year,
                local.tm_mon,
                local.tm_mday,
                0,
                0,
                0,
                local.tm_wday,
                local.tm_yday,
                local.tm_isdst,
            )
        )
        return time.mktime(start)
    if range_name == "7d":
        return now - 7 * 86400
    if range_name == "30d":
        return now - 30 * 86400
    return None


__all__ = ["UsageRange", "UsageStatsStore", "UsageSummary"]
