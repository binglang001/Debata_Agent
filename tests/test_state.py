from __future__ import annotations

import asyncio

import pytest

from core.state import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_refreshes_whitelist_without_blocking_hot_path():
    release = asyncio.Event()
    calls = 0

    async def whitelist_provider() -> set[str]:
        nonlocal calls
        calls += 1
        await release.wait()
        return {"friend"}

    limiter = RateLimiter(
        window_seconds=60,
        max_messages=1,
        whitelist_provider=whitelist_provider,
        whitelist_cache_ttl_seconds=300,
    )

    limited = await asyncio.wait_for(limiter.check_and_log("friend"), timeout=0.1)

    assert limited is False
    await asyncio.sleep(0)
    assert calls == 1

    release.set()
    assert limiter._whitelist_refresh_task is not None
    await limiter._whitelist_refresh_task

    # The same user already has one counted message, but once the background
    # whitelist refresh completes they bypass rate limiting.
    assert await limiter.check_and_log("friend") is False


@pytest.mark.asyncio
async def test_rate_limiter_uses_stale_whitelist_while_refreshing():
    release = asyncio.Event()
    calls = 0

    async def whitelist_provider() -> set[str]:
        nonlocal calls
        calls += 1
        await release.wait()
        return {"friend"}

    limiter = RateLimiter(
        window_seconds=60,
        max_messages=1,
        whitelist_provider=whitelist_provider,
        whitelist_cache_ttl_seconds=300,
    )
    limiter._whitelist_cache = {"friend"}
    limiter._whitelist_cache_expires_at = 0.0

    assert await asyncio.wait_for(limiter.check_and_log("friend"), timeout=0.1) is False
    await asyncio.sleep(0)
    assert calls == 1

    release.set()
    assert limiter._whitelist_refresh_task is not None
    await limiter._whitelist_refresh_task
