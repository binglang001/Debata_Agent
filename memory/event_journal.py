"""事件日志门面。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from .event_store import EventStore

logger = logging.getLogger(__name__)


class EventJournal:
    """保持旧调用入口，append 只等待 EventStore durable append log。"""

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._state_lock = asyncio.Lock()
        self._append_lock = asyncio.Lock()
        self._started = False
        self._shutting_down = False

    @property
    def store(self) -> EventStore:
        return self._store

    async def start(self) -> None:
        """启动后台投影 worker；重复调用无副作用。"""
        async with self._state_lock:
            if self._started and not self._shutting_down:
                return
            self._started = True
            self._shutting_down = False
        await self._store.start_projection()

    async def shutdown(self, *, timeout: float | None = 5.0) -> bool:
        """停止接收新事件，并让底层 store 尽量投影追平。"""
        async with self._state_lock:
            if not self._started and self._shutting_down:
                return True
            self._shutting_down = True

        projected = await self._store.shutdown(timeout=timeout)
        async with self._append_lock:
            pass
        async with self._state_lock:
            self._started = False
        return projected

    async def drain(self) -> None:
        """兼容旧入口：显式等待当前 append 进度投影完成。"""
        await self.wait_projected()

    async def wait_projected(
        self,
        event_id: int | None = None,
        *,
        timeout: float | None = None,
    ) -> bool:
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        started_at = time.perf_counter() if debug_enabled else 0.0
        projected = await self._store.wait_projected(event_id, timeout=timeout)
        if debug_enabled:
            stats = await self._store.stats()
            logger.debug(
                "EventJournal drain 指标 wait_ms=%.3f pending_count=%d projected=%s",
                (time.perf_counter() - started_at) * 1000,
                stats["pending_count"],
                projected,
            )
        return projected

    async def stats(self) -> dict[str, Any]:
        return await self._store.stats()

    async def append_event(
        self,
        *,
        event_type: str,
        payload: Any,
        event_uuid: str | None = None,
        conversation_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | int | None = None,
        source: str | None = None,
        external_id: str | None = None,
        tool_call_id: str | None = None,
        parent_event_id: int | None = None,
        idempotency_key: str | None = None,
        timestamp_unix: float | int | None = None,
        created_at_unix: float | int | None = None,
        schema_version: int = 1,
    ) -> int:
        ids = await self.append_events(
            [
                {
                    "event_type": event_type,
                    "event_uuid": event_uuid,
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "source": source,
                    "external_id": external_id,
                    "tool_call_id": tool_call_id,
                    "parent_event_id": parent_event_id,
                    "idempotency_key": idempotency_key,
                    "timestamp_unix": timestamp_unix,
                    "created_at_unix": created_at_unix,
                    "payload": payload,
                    "schema_version": schema_version,
                }
            ]
        )
        return ids[0]

    async def append_events(self, events: list[Mapping[str, Any]]) -> list[int]:
        if not events:
            return []

        event_list = list(events)
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        started_at = time.perf_counter() if debug_enabled else 0.0
        async with self._append_lock:
            async with self._state_lock:
                if not self._started:
                    raise RuntimeError("EventJournal is not started")
                if self._shutting_down:
                    raise RuntimeError("EventJournal is shutting down")
            try:
                ids = await self._store.append_events(event_list)
            except Exception:
                if debug_enabled:
                    logger.debug(
                        "EventJournal append_events 指标 event_count=%d append_log_ms=%.3f ok=%s",
                        len(event_list),
                        (time.perf_counter() - started_at) * 1000,
                        False,
                    )
                raise
            if debug_enabled:
                stats = await self._store.stats()
                logger.debug(
                    "EventJournal append_events 指标 event_count=%d append_log_ms=%.3f "
                    "pending_count=%d projection_error_count=%d ok=%s",
                    len(event_list),
                    (time.perf_counter() - started_at) * 1000,
                    stats["pending_count"],
                    stats["projection_error_count"],
                    True,
                )
            return ids

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        return await self._store.get_event(event_id)

    async def get_events(self, event_ids: list[int]) -> list[dict[str, Any] | None]:
        return await self._store.get_events(event_ids)

    async def iter_events(
        self,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        return await self._store.iter_events(
            limit=limit,
            after_event_id=after_event_id,
            before_event_id=before_event_id,
            order=order,
        )

    async def events_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        before_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return await self._store.events_for_conversation(
            conversation_id,
            limit=limit,
            before_event_id=before_event_id,
        )

    async def events_by_type(
        self,
        event_type: str,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        return await self._store.events_by_type(
            event_type,
            limit=limit,
            after_event_id=after_event_id,
            before_event_id=before_event_id,
            order=order,
        )
