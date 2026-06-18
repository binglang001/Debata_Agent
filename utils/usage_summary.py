"""模型 usage 聚合的轻量共享类型。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

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


def cutoff_timestamp(range_name: UsageRange) -> float | None:
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


__all__ = ["UsageRange", "UsageSummary", "cutoff_timestamp"]
