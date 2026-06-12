"""磁盘 append-only 事件库。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingEvent:
    event_type: str
    event_uuid: str
    conversation_id: str | None
    session_id: str | None
    turn_id: str | None
    source: str | None
    external_id: str | None
    tool_call_id: str | None
    parent_event_id: int | None
    idempotency_key: str | None
    timestamp_unix: float
    created_at_unix: float
    payload_json: str
    payload_hash: str
    schema_version: int


@dataclass(slots=True)
class _StoredEvent:
    event_id: int
    event: _PendingEvent


@dataclass(slots=True)
class _LoadedState:
    idempotency_index: dict[str, int]
    last_appended_event_id: int
    last_projected_event_id: int
    projection_error: str | None = None


class EventStore:
    """先写 durable append log，再由后台 worker 投影到 SQLite。"""

    def __init__(
        self,
        path: Path,
        *,
        append_log_path: Path | None = None,
        projection_batch_size: int = 100,
        max_pending_events: int = 10000,
        projection_retry_delay: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.append_log_path = (
            Path(append_log_path)
            if append_log_path is not None
            else self.path.with_name(f"{self.path.name}.append.jsonl")
        )
        self._projection_batch_size = max(1, int(projection_batch_size))
        self._max_pending_events = max(1, int(max_pending_events))
        self._projection_retry_delay = max(0.001, float(projection_retry_delay))

        self._append_lock = asyncio.Lock()
        self._sqlite_lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()
        self._projection_condition = asyncio.Condition()

        self._loaded = False
        self._closed = False
        self._stop_projection_requested = False
        self._force_stop_projection = False
        self._projection_task: asyncio.Task | None = None

        self._idempotency_index: dict[str, int] = {}
        self._last_appended_event_id = 0
        self._last_projected_event_id = 0
        self._projection_error_count = 0
        self._last_projection_error: str | None = None
        self._last_projection_error_event_id: int | None = None

    async def start_projection(self) -> None:
        """显式启动投影 worker；append/wait_projected 也会按需启动。"""
        await self._ensure_loaded()
        await self._ensure_projection_worker()

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
        """追加单个事件；返回时只保证 append log 已持久化。"""
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
        """追加一批事件，返回与输入顺序对应的全局 event_id 列表。"""
        if not events:
            return []

        pending = [_normalize_event(event) for event in events]
        await self._ensure_loaded()
        async with self._append_lock:
            ids: list[int] = []
            new_events: list[_StoredEvent] = []
            batch_idempotency: dict[str, int] = {}
            next_event_id = self._last_appended_event_id + 1

            for event in pending:
                existing_id = None
                if event.idempotency_key is not None:
                    existing_id = batch_idempotency.get(
                        event.idempotency_key,
                        self._idempotency_index.get(event.idempotency_key),
                    )
                if existing_id is not None:
                    ids.append(existing_id)
                    continue

                stored = _StoredEvent(next_event_id, event)
                new_events.append(stored)
                ids.append(next_event_id)
                if event.idempotency_key is not None:
                    batch_idempotency[event.idempotency_key] = next_event_id
                next_event_id += 1

            if not new_events:
                return ids

            for segment in _event_segments(new_events, self._max_pending_events):
                await self._wait_for_backpressure(len(segment))
                await asyncio.to_thread(self._append_log_events_sync, segment)

                for stored in segment:
                    key = stored.event.idempotency_key
                    if key is not None:
                        self._idempotency_index[key] = stored.event_id
                async with self._projection_condition:
                    self._last_appended_event_id = segment[-1].event_id
                    self._projection_condition.notify_all()
                await self._ensure_projection_worker()
            return ids

    async def wait_projected(
        self,
        event_id: int | None = None,
        *,
        timeout: float | None = None,
    ) -> bool:
        """等待 SQLite 投影至少追到 event_id；None 表示当前 append 进度。"""
        await self._ensure_loaded()
        target_event_id = await self._projection_target(event_id)
        if target_event_id <= 0:
            return True
        async with self._projection_condition:
            if self._last_projected_event_id >= target_event_id:
                return True

        await self._ensure_projection_worker()

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        async with self._projection_condition:
            self._projection_condition.notify_all()
            while self._last_projected_event_id < target_event_id:
                if deadline is None:
                    await self._projection_condition.wait()
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._projection_condition.wait(), timeout=remaining)
                except TimeoutError:
                    return False
            return True

    async def stats(self) -> dict[str, Any]:
        """返回投影延迟与错误计数等基础运行状态。"""
        await self._ensure_loaded()
        async with self._projection_condition:
            pending_count = max(0, self._last_appended_event_id - self._last_projected_event_id)
            task = self._projection_task
            return {
                "last_appended_event_id": self._last_appended_event_id,
                "last_projected_event_id": self._last_projected_event_id,
                "projection_lag": pending_count,
                "pending_count": pending_count,
                "projection_error_count": self._projection_error_count,
                "last_projection_error": self._last_projection_error,
                "last_projection_error_event_id": self._last_projection_error_event_id,
                "projection_running": task is not None and not task.done(),
                "closed": self._closed,
            }

    async def shutdown(self, *, timeout: float | None = 5.0) -> bool:
        """停止接收新事件，尽量等待投影追平；未追平时保留 append log 供重启重放。"""
        await self._ensure_loaded()
        async with self._projection_condition:
            self._closed = True
            self._projection_condition.notify_all()

        async with self._append_lock:
            pass

        await self._ensure_projection_worker()
        target_event_id = await self._projection_target(None)
        projected = await self.wait_projected(target_event_id, timeout=timeout)

        async with self._projection_condition:
            self._stop_projection_requested = True
            self._force_stop_projection = not projected
            self._projection_condition.notify_all()

        task = self._projection_task
        if task is not None and not task.done():
            wait_timeout = None if projected else max(0.1, self._projection_retry_delay * 2)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_timeout)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return projected

    async def close(self, *, timeout: float | None = 5.0) -> bool:
        return await self.shutdown(timeout=timeout)

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        event_id = _positive_int_or_none(event_id)
        if event_id is None:
            return None
        await self._ensure_loaded()
        return await asyncio.to_thread(self._get_event_sync, event_id)

    async def get_events(self, event_ids: list[int]) -> list[dict[str, Any] | None]:
        """批量读取已投影事件，返回顺序与输入 event_ids 一致；缺失项为 None。"""
        normalized_ids = [_positive_int_or_none(event_id) for event_id in event_ids]
        query_ids = list(dict.fromkeys(event_id for event_id in normalized_ids if event_id))
        if not query_ids:
            return [None for _ in normalized_ids]
        await self._ensure_loaded()
        events_by_id = await asyncio.to_thread(self._get_events_sync, query_ids)
        return [
            events_by_id.get(event_id) if event_id is not None else None
            for event_id in normalized_ids
        ]

    async def iter_events(
        self,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
        """按 event_id keyset 游标读取当前已投影的全局事件页。"""
        limit = _clamp_limit(limit)
        after_event_id = _positive_int_or_none(after_event_id)
        before_event_id = _positive_int_or_none(before_event_id)
        order = _normalize_order(order)
        await self._ensure_loaded()
        return await asyncio.to_thread(
            self._iter_events_sync,
            limit,
            after_event_id,
            before_event_id,
            order,
        )

    async def events_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        before_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """读取某会话已投影的最近一页事件，返回值始终按 event_id 升序排列。"""
        conversation_id = _clean_optional(conversation_id)
        if conversation_id is None:
            return []
        limit = _clamp_limit(limit)
        before_event_id = _positive_int_or_none(before_event_id)
        await self._ensure_loaded()
        return await asyncio.to_thread(
            self._events_for_conversation_sync,
            conversation_id,
            limit,
            before_event_id,
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
        """按事件类型读取当前已投影的一页事件。"""
        event_type = _clean_required(event_type, "event_type")
        limit = _clamp_limit(limit)
        after_event_id = _positive_int_or_none(after_event_id)
        before_event_id = _positive_int_or_none(before_event_id)
        order = _normalize_order(order)
        await self._ensure_loaded()
        return await asyncio.to_thread(
            self._events_by_type_sync,
            event_type,
            limit,
            after_event_id,
            before_event_id,
            order,
        )

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            loaded = await asyncio.to_thread(self._load_state_sync)
            self._idempotency_index = loaded.idempotency_index
            async with self._projection_condition:
                self._last_appended_event_id = loaded.last_appended_event_id
                self._last_projected_event_id = loaded.last_projected_event_id
                if loaded.projection_error is not None:
                    self._projection_error_count += 1
                    self._last_projection_error = loaded.projection_error
                    self._last_projection_error_event_id = None
                self._loaded = True
                self._projection_condition.notify_all()

    async def _ensure_projection_worker(self) -> None:
        async with self._projection_condition:
            task = self._projection_task
            if task is not None and not task.done():
                return
            if (
                self._closed
                and self._last_projected_event_id >= self._last_appended_event_id
            ):
                return
            self._stop_projection_requested = False
            self._force_stop_projection = False
            self._projection_task = asyncio.create_task(
                self._run_projection_worker(),
                name="event-store-projector",
            )
            self._projection_task.add_done_callback(self._on_projection_done)
            self._projection_condition.notify_all()

    async def _projection_target(self, event_id: int | None) -> int:
        async with self._projection_condition:
            if event_id is None:
                return self._last_appended_event_id
            normalized = _positive_int_or_none(event_id)
            return normalized if normalized is not None else 0

    async def _wait_for_backpressure(self, new_event_count: int) -> None:
        if new_event_count <= 0:
            return
        while True:
            await self._ensure_projection_worker()
            async with self._projection_condition:
                if self._closed:
                    raise RuntimeError("EventStore is closed")
                pending_count = self._last_appended_event_id - self._last_projected_event_id
                if pending_count + new_event_count <= self._max_pending_events:
                    return
                self._projection_condition.notify_all()
                await self._projection_condition.wait()

    async def _run_projection_worker(self) -> None:
        while True:
            async with self._projection_condition:
                if self._force_stop_projection:
                    return
                if self._last_projected_event_id >= self._last_appended_event_id:
                    return
                after_event_id = self._last_projected_event_id
                target_event_id = self._last_appended_event_id

            batch: list[_StoredEvent] = []
            try:
                batch = await asyncio.to_thread(
                    self._read_unprojected_events_sync,
                    after_event_id,
                    target_event_id,
                    self._projection_batch_size,
                )
                if not batch:
                    async with self._projection_condition:
                        if self._stop_projection_requested:
                            return
                        self._projection_condition.notify_all()
                    await asyncio.sleep(self._projection_retry_delay)
                    continue

                async with self._sqlite_lock:
                    await asyncio.to_thread(self._project_events_sync, batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._record_projection_error(batch[0].event_id if batch else None, exc)
                async with self._projection_condition:
                    if self._stop_projection_requested:
                        return
                await asyncio.sleep(self._projection_retry_delay)
                continue

            async with self._projection_condition:
                self._last_projected_event_id = max(
                    self._last_projected_event_id,
                    batch[-1].event_id,
                )
                self._last_projection_error = None
                self._last_projection_error_event_id = None
                self._projection_condition.notify_all()

    async def _record_projection_error(
        self,
        event_id: int | None,
        exc: BaseException,
    ) -> None:
        async with self._projection_condition:
            self._projection_error_count += 1
            self._last_projection_error = str(exc)
            self._last_projection_error_event_id = event_id
            self._projection_condition.notify_all()
        logger.warning(
            "EventStore SQLite 投影失败 event_id=%s error=%s",
            event_id,
            exc,
            exc_info=exc,
        )

    @staticmethod
    def _on_projection_done(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("EventStore projection worker 异常退出: %s", exc, exc_info=exc)

    def _connect(self, *, ensure_schema: bool = True) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        if ensure_schema:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_log (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                event_uuid TEXT NOT NULL,
                conversation_id TEXT,
                session_id TEXT,
                turn_id TEXT,
                source TEXT,
                external_id TEXT,
                tool_call_id TEXT,
                parent_event_id INTEGER,
                idempotency_key TEXT UNIQUE,
                timestamp_unix REAL NOT NULL,
                created_at_unix REAL NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_projection_state (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_log_conversation_event "
            "ON event_log(conversation_id, event_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_log_type_event "
            "ON event_log(event_type, event_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_log_session_event "
            "ON event_log(session_id, event_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_log_external "
            "ON event_log(source, external_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_log_parent "
            "ON event_log(parent_event_id)"
        )
        conn.commit()

    def _load_state_sync(self) -> _LoadedState:
        idempotency_index: dict[str, int] = {}
        max_log_id = 0
        for record in _iter_append_log_records(self.append_log_path):
            stored = _stored_event_from_log_record(record)
            max_log_id = max(max_log_id, stored.event_id)
            if stored.event.idempotency_key is not None:
                idempotency_index.setdefault(stored.event.idempotency_key, stored.event_id)

        max_db_id = 0
        state_row_value: str | None = None
        projection_error = None
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT event_id, idempotency_key
                    FROM event_log
                    WHERE idempotency_key IS NOT NULL
                    ORDER BY event_id ASC
                    """
                ).fetchall()
                for row in rows:
                    idempotency_index.setdefault(
                        str(row["idempotency_key"]),
                        int(row["event_id"]),
                    )
                max_db_id = int(
                    conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM event_log").fetchone()[0]
                )
                state_row = conn.execute(
                    "SELECT value FROM event_projection_state WHERE name = ?",
                    ("last_projected_event_id",),
                ).fetchone()
                if state_row is not None:
                    state_row_value = str(state_row["value"])
        except Exception as exc:
            projection_error = str(exc)

        if state_row_value is not None:
            last_projected_event_id = _positive_int_or_none(state_row_value) or 0
        else:
            last_projected_event_id = 0 if projection_error is not None else max_db_id
        last_appended_event_id = max(max_db_id, max_log_id)
        return _LoadedState(
            idempotency_index=idempotency_index,
            last_appended_event_id=last_appended_event_id,
            last_projected_event_id=min(last_projected_event_id, last_appended_event_id),
            projection_error=projection_error,
        )

    def _append_log_events_sync(self, events: list[_StoredEvent]) -> None:
        self.append_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.append_log_path.open("a", encoding="utf-8", newline="\n") as file:
            for event in events:
                file.write(
                    json.dumps(
                        _stored_event_to_log_record(event),
                        ensure_ascii=False,
                        sort_keys=False,
                        separators=(",", ":"),
                    )
                )
                file.write("\n")
            file.flush()
            os.fsync(file.fileno())

    def _read_unprojected_events_sync(
        self,
        after_event_id: int,
        target_event_id: int,
        limit: int,
    ) -> list[_StoredEvent]:
        events: list[_StoredEvent] = []
        for record in _iter_append_log_records(self.append_log_path):
            stored = _stored_event_from_log_record(record)
            if stored.event_id <= after_event_id:
                continue
            if stored.event_id > target_event_id:
                break
            events.append(stored)
            if len(events) >= limit:
                break
        return events

    def _project_events_sync(self, events: list[_StoredEvent]) -> None:
        if not events:
            return
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for event in events:
                    _insert_projected_event(conn, event)
                _set_projection_progress(conn, events[-1].event_id)
            except Exception:
                conn.rollback()
                raise
            conn.commit()

    def _get_event_sync(self, event_id: int) -> dict[str, Any] | None:
        with self._connect(ensure_schema=False) as conn:
            row = conn.execute(
                "SELECT * FROM event_log WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return _row_to_event(row) if row is not None else None

    def _get_events_sync(self, event_ids: list[int]) -> dict[int, dict[str, Any]]:
        placeholders = ", ".join("?" for _ in event_ids)
        with self._connect(ensure_schema=False) as conn:
            rows = conn.execute(
                f"SELECT * FROM event_log WHERE event_id IN ({placeholders})",
                event_ids,
            ).fetchall()
        return {int(row["event_id"]): _row_to_event(row) for row in rows}

    def _iter_events_sync(
        self,
        limit: int,
        after_event_id: int | None,
        before_event_id: int | None,
        order: str,
    ) -> list[dict[str, Any]]:
        where, params = _cursor_where(after_event_id, before_event_id)
        sql = "SELECT * FROM event_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY event_id {order.upper()} LIMIT ?"
        params.append(limit)
        with self._connect(ensure_schema=False) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(row) for row in rows]

    def _events_for_conversation_sync(
        self,
        conversation_id: str,
        limit: int,
        before_event_id: int | None,
    ) -> list[dict[str, Any]]:
        where = ["conversation_id = ?"]
        params: list[Any] = [conversation_id]
        if before_event_id is not None:
            where.append("event_id < ?")
            params.append(before_event_id)
        params.append(limit)
        with self._connect(ensure_schema=False) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM event_log
                WHERE {" AND ".join(where)}
                ORDER BY event_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_event(row) for row in reversed(rows)]

    def _events_by_type_sync(
        self,
        event_type: str,
        limit: int,
        after_event_id: int | None,
        before_event_id: int | None,
        order: str,
    ) -> list[dict[str, Any]]:
        where = ["event_type = ?"]
        params: list[Any] = [event_type]
        cursor_where, cursor_params = _cursor_where(after_event_id, before_event_id)
        where.extend(cursor_where)
        params.extend(cursor_params)
        params.append(limit)
        with self._connect(ensure_schema=False) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM event_log
                WHERE {" AND ".join(where)}
                ORDER BY event_id {order.upper()}
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_event(row) for row in rows]


def _insert_projected_event(conn: sqlite3.Connection, stored: _StoredEvent) -> None:
    event = stored.event
    try:
        conn.execute(
            """
            INSERT INTO event_log (
                event_id, event_type, event_uuid, conversation_id, session_id, turn_id,
                source, external_id, tool_call_id, parent_event_id,
                idempotency_key, timestamp_unix, created_at_unix, payload_json,
                payload_hash, schema_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored.event_id,
                event.event_type,
                event.event_uuid,
                event.conversation_id,
                event.session_id,
                event.turn_id,
                event.source,
                event.external_id,
                event.tool_call_id,
                event.parent_event_id,
                event.idempotency_key,
                event.timestamp_unix,
                event.created_at_unix,
                event.payload_json,
                event.payload_hash,
                event.schema_version,
            ),
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT event_type, payload_hash FROM event_log WHERE event_id = ?",
            (stored.event_id,),
        ).fetchone()
        if (
            row is not None
            and row["event_type"] == event.event_type
            and row["payload_hash"] == event.payload_hash
        ):
            return
        raise


def _set_projection_progress(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute(
        """
        INSERT INTO event_projection_state(name, value)
        VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET value = excluded.value
        """,
        ("last_projected_event_id", str(event_id)),
    )


def _event_segments(
    events: list[_StoredEvent],
    max_segment_size: int,
) -> Iterable[list[_StoredEvent]]:
    segment_size = max(1, int(max_segment_size))
    for index in range(0, len(events), segment_size):
        yield events[index : index + segment_size]


def _stored_event_to_log_record(stored: _StoredEvent) -> dict[str, Any]:
    event = stored.event
    return {
        "event_id": stored.event_id,
        "event_type": event.event_type,
        "event_uuid": event.event_uuid,
        "conversation_id": event.conversation_id,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "source": event.source,
        "external_id": event.external_id,
        "tool_call_id": event.tool_call_id,
        "parent_event_id": event.parent_event_id,
        "idempotency_key": event.idempotency_key,
        "timestamp_unix": event.timestamp_unix,
        "created_at_unix": event.created_at_unix,
        "payload_json": event.payload_json,
        "payload_hash": event.payload_hash,
        "schema_version": event.schema_version,
    }


def _stored_event_from_log_record(record: Mapping[str, Any]) -> _StoredEvent:
    event_id = _positive_int_or_none(record.get("event_id"))
    if event_id is None:
        raise ValueError("append log event_id is required")
    return _StoredEvent(
        event_id=event_id,
        event=_PendingEvent(
            event_type=_clean_required(record.get("event_type"), "event_type"),
            event_uuid=_clean_required(record.get("event_uuid"), "event_uuid"),
            conversation_id=_clean_optional(record.get("conversation_id")),
            session_id=_clean_optional(record.get("session_id")),
            turn_id=_clean_optional(record.get("turn_id")),
            source=_clean_optional(record.get("source")),
            external_id=_clean_optional(record.get("external_id")),
            tool_call_id=_clean_optional(record.get("tool_call_id")),
            parent_event_id=_positive_int_or_none(record.get("parent_event_id")),
            idempotency_key=_clean_optional(record.get("idempotency_key")),
            timestamp_unix=_float_or_default(record.get("timestamp_unix"), time.time()),
            created_at_unix=_float_or_default(record.get("created_at_unix"), time.time()),
            payload_json=_clean_required(record.get("payload_json"), "payload_json"),
            payload_hash=_clean_required(record.get("payload_hash"), "payload_hash"),
            schema_version=_positive_int_or_default(record.get("schema_version"), 1),
        ),
    )


def _iter_append_log_records(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"append log JSON decode failed at line {line_number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"append log record must be object at line {line_number}")
            yield record


def _normalize_event(event: Mapping[str, Any]) -> _PendingEvent:
    now = time.time()
    payload = event.get("payload", {})
    payload_json = _payload_json(payload)
    return _PendingEvent(
        event_type=_clean_required(event.get("event_type"), "event_type"),
        event_uuid=_clean_optional(event.get("event_uuid")) or str(uuid.uuid4()),
        conversation_id=_clean_optional(event.get("conversation_id")),
        session_id=_clean_optional(event.get("session_id")),
        turn_id=_clean_optional(event.get("turn_id")),
        source=_clean_optional(event.get("source")),
        external_id=_clean_optional(event.get("external_id")),
        tool_call_id=_clean_optional(event.get("tool_call_id")),
        parent_event_id=_positive_int_or_none(event.get("parent_event_id")),
        idempotency_key=_clean_optional(event.get("idempotency_key")),
        timestamp_unix=_float_or_default(event.get("timestamp_unix"), now),
        created_at_unix=_float_or_default(event.get("created_at_unix"), now),
        payload_json=payload_json,
        payload_hash=_payload_hash(payload),
        schema_version=_positive_int_or_default(event.get("schema_version"), 1),
    )


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    return {
        "event_id": int(row["event_id"]),
        "event_type": row["event_type"],
        "event_uuid": row["event_uuid"],
        "conversation_id": row["conversation_id"],
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "source": row["source"],
        "external_id": row["external_id"],
        "tool_call_id": row["tool_call_id"],
        "parent_event_id": row["parent_event_id"],
        "idempotency_key": row["idempotency_key"],
        "timestamp_unix": row["timestamp_unix"],
        "created_at_unix": row["created_at_unix"],
        "payload_json": row["payload_json"],
        "payload_hash": row["payload_hash"],
        "schema_version": row["schema_version"],
        "payload": payload,
    }


def _cursor_where(
    after_event_id: int | None,
    before_event_id: int | None,
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if after_event_id is not None:
        where.append("event_id > ?")
        params.append(after_event_id)
    if before_event_id is not None:
        where.append("event_id < ?")
        params.append(before_event_id)
    return where, params


def _payload_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=False, default=str)


def _payload_hash(payload: Any) -> str:
    canonical = json.dumps(
        _canonical_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_payload(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list | tuple):
        return [_canonical_payload(child) for child in value]
    if isinstance(value, set | frozenset):
        return sorted((_canonical_payload(child) for child in value), key=repr)
    return value


def _clean_required(value: Any, name: str) -> str:
    cleaned = _clean_optional(value)
    if cleaned is None:
        raise ValueError(f"{name} is required")
    return cleaned


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_int_or_default(value: Any, default: int) -> int:
    number = _positive_int_or_none(value)
    return number if number is not None else default


def _float_or_default(value: Any, default: float) -> float:
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 100
    return min(max(limit, 1), 1000)


def _normalize_order(order: str) -> str:
    normalized = str(order or "asc").strip().lower()
    if normalized not in {"asc", "desc"}:
        raise ValueError("order must be 'asc' or 'desc'")
    return normalized
