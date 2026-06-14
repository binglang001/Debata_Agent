"""人格状态 SQLite 存储。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_MISSING = object()

_STATE_TYPES = ("PersonaState",)
_STATE_LOG_TYPES = ("PersonaStateLog", "StateLogEntry")
_EFFECT_TYPES = ("Effect", "PersonaEffect")
_TODO_TYPES = ("Todo", "PersonaTodo")
_CUE_TYPES = ("Cue", "PersonaCue")
_PROFILE_TYPES = ("UserProfile", "PersonaUserProfile")
_MONOLOGUE_TYPES = ("InnerMonologue",)
_TRAJECTORY_TYPES = ("DailyTrajectory",)
_ARC_TYPES = ("PersonaArcEvent", "PersonaArc")
_SLEEP_TYPES = ("SleepRecord",)
_EAT_TYPES = ("EatRecord",)
_COMPLETED_STATUS_VALUES = {
    "1",
    "true",
    "yes",
    "y",
    "completed",
    "complete",
    "done",
    "finished",
    "closed",
    "cancelled",
    "canceled",
    "missed",
}
_INCOMPLETE_STATUS_VALUES = {
    "0",
    "false",
    "no",
    "n",
    "pending",
    "open",
    "active",
    "in_progress",
    "in-progress",
    "todo",
    "new",
}


class PersonaDB:
    """人格系统 sqlite3 后端。

    连接按操作打开，所有阻塞 sqlite3 调用通过 asyncio.to_thread 执行，并用
    asyncio.Lock 串行化写入与读取。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """初始化数据库 schema。"""
        async with self._lock:
            await asyncio.to_thread(self._load_sync)

    async def close(self) -> None:
        """按操作连接，无常驻连接需要关闭。"""
        return None

    async def get_state(self, default: Any = _MISSING) -> Any:
        async with self._lock:
            data = await asyncio.to_thread(self._get_state_sync, default)
        if data is _MISSING:
            return _adapt_record({}, _STATE_TYPES)
        if data is default:
            return default
        return _adapt_record(data, _STATE_TYPES)

    async def save_state(self, state: Any) -> None:
        data = _record_to_dict(state)
        async with self._lock:
            await asyncio.to_thread(self._save_state_sync, data)

    async def append_state_log(self, entry: Any) -> int:
        data = _record_to_dict(entry)
        async with self._lock:
            return await asyncio.to_thread(self._append_state_log_sync, data)

    async def add_effect(self, effect: Any) -> str:
        data = _record_to_dict(effect)
        effect_id = _ensure_record_id(data, ("effect_id", "id"), "effect")
        async with self._lock:
            await asyncio.to_thread(self._upsert_effect_sync, effect_id, data)
        return effect_id

    async def get_active_effects(self, now: Any = None) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(self._get_active_effects_sync, _now_value(now))
        return [_adapt_record(row, _EFFECT_TYPES) for row in rows]

    async def remove_effects(self, ids: str | Iterable[str]) -> int:
        effect_ids = _clean_ids(ids)
        if not effect_ids:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._delete_by_ids_sync, "effects", "effect_id", effect_ids)

    async def expire_effects(self, now: Any = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._expire_records_sync,
                "effects",
                "effect_id",
                "effect_json",
                _now_value(now),
            )

    async def get_todos(self, include_completed: bool = True) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(self._get_todos_sync, include_completed)
        return [_adapt_record(row, _TODO_TYPES) for row in rows]

    async def upsert_todo(self, todo: Any) -> str:
        data = _record_to_dict(todo)
        todo_id = _ensure_record_id(data, ("todo_id", "id"), "todo")
        async with self._lock:
            await asyncio.to_thread(self._upsert_todo_sync, todo_id, data)
        return todo_id

    async def mark_expired_todos_missed(self, now: Any = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._mark_expired_todos_missed_sync, _now_value(now))

    async def remove_todos(self, ids: str | Iterable[str]) -> int:
        todo_ids = _clean_ids(ids)
        if not todo_ids:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._delete_by_ids_sync, "todos", "todo_id", todo_ids)

    async def get_cues(self, now: Any = None) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(self._get_cues_sync, _now_value(now))
        return [_adapt_record(row, _CUE_TYPES) for row in rows]

    async def upsert_cue(self, cue: Any) -> str:
        data = _record_to_dict(cue)
        cue_id = _ensure_record_id(data, ("cue_id", "id"), "cue")
        async with self._lock:
            await asyncio.to_thread(self._upsert_cue_sync, cue_id, data)
        return cue_id

    async def remove_cues(self, ids: str | Iterable[str]) -> int:
        cue_ids = _clean_ids(ids)
        if not cue_ids:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._delete_by_ids_sync, "cues", "cue_id", cue_ids)

    async def expire_cues(self, now: Any = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._expire_records_sync,
                "cues",
                "cue_id",
                "cue_json",
                _now_value(now),
            )

    async def get_profile(self, user_id: str) -> Any | None:
        clean_user_id = str(user_id or "").strip()
        if not clean_user_id:
            return None
        async with self._lock:
            data = await asyncio.to_thread(self._get_profile_sync, clean_user_id)
        return _adapt_record(data, _PROFILE_TYPES) if data is not None else None

    async def upsert_profile(self, profile: Any) -> str:
        data = _record_to_dict(profile)
        user_id = _optional_text(data, ("user_id", "profile_id", "id"))
        if not user_id:
            raise ValueError("user profile requires user_id/profile_id/id")
        data.setdefault("user_id", user_id)
        async with self._lock:
            await asyncio.to_thread(self._upsert_profile_sync, user_id, data)
        return user_id

    async def all_profiles(self) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(self._all_profiles_sync)
        return [_adapt_record(row, _PROFILE_TYPES) for row in rows]

    async def add_monologue(self, monologue: Any) -> int:
        data = _record_to_dict(monologue)
        async with self._lock:
            return await asyncio.to_thread(
                self._insert_json_row_sync,
                "inner_monologues",
                "monologue_json",
                data,
            )

    async def recent_monologues(self, limit: int = 20) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._recent_json_rows_sync,
                "inner_monologues",
                "monologue_json",
                limit,
            )
        return [_adapt_record(row, _MONOLOGUE_TYPES) for row in rows]

    async def add_trajectory(self, trajectory: Any) -> int:
        data = _record_to_dict(trajectory)
        async with self._lock:
            return await asyncio.to_thread(
                self._insert_json_row_sync,
                "daily_trajectories",
                "trajectory_json",
                data,
            )

    async def recent_trajectories(self, limit: int = 20) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._recent_json_rows_sync,
                "daily_trajectories",
                "trajectory_json",
                limit,
            )
        return [_adapt_record(row, _TRAJECTORY_TYPES) for row in rows]

    async def add_arc_event(self, event: Any) -> int:
        data = _record_to_dict(event)
        async with self._lock:
            return await asyncio.to_thread(
                self._insert_json_row_sync,
                "persona_arc",
                "event_json",
                data,
            )

    async def recent_arc_events(self, limit: int = 20) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_json_rows_desc_sync,
                "persona_arc",
                "event_json",
                limit,
            )

    async def add_sleep_record(self, record: Any) -> str:
        data = _record_to_dict(record)
        record_id = _ensure_record_id(data, ("record_id", "sleep_id", "id"), "sleep")
        async with self._lock:
            await asyncio.to_thread(self._upsert_sleep_record_sync, record_id, data)
        return record_id

    async def recent_sleep_records(self, limit: int = 20) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_json_rows_desc_sync,
                "sleep_records",
                "record_json",
                limit,
                "created_at",
                "rowid",
            )

    async def update_sleep_record(
        self,
        record_id_or_record: str | Any,
        updates: Mapping[str, Any] | None = None,
        **fields_to_update: Any,
    ) -> bool:
        if isinstance(record_id_or_record, str):
            record_id = record_id_or_record.strip()
            new_data = dict(updates or {})
            new_data.update(fields_to_update)
        else:
            new_data = _record_to_dict(record_id_or_record)
            if updates:
                new_data.update(dict(updates))
            new_data.update(fields_to_update)
            record_id = _optional_text(new_data, ("record_id", "sleep_id", "id")) or ""
        if not record_id:
            return False
        async with self._lock:
            return await asyncio.to_thread(self._update_sleep_record_sync, record_id, new_data)

    async def add_eat_record(self, record: Any) -> int:
        data = _record_to_dict(record)
        async with self._lock:
            return await asyncio.to_thread(self._add_eat_record_sync, data)

    async def update_eat_record(
        self,
        record_id_or_record: str | Any,
        updates: Mapping[str, Any] | None = None,
        **fields_to_update: Any,
    ) -> bool:
        if isinstance(record_id_or_record, str):
            record_id = record_id_or_record.strip()
            new_data = dict(updates or {})
            new_data.update(fields_to_update)
        else:
            new_data = _record_to_dict(record_id_or_record)
            if updates:
                new_data.update(dict(updates))
            new_data.update(fields_to_update)
            record_id = _optional_text(new_data, ("record_id", "eat_id", "id")) or ""
        if not record_id:
            return False
        async with self._lock:
            return await asyncio.to_thread(self._update_eat_record_sync, record_id, new_data)

    async def recent_eat_records(self, limit: int = 20) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_json_rows_desc_sync,
                "eat_records",
                "record_json",
                limit,
            )

    async def recent_state_logs(self, limit: int = 50) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_json_rows_desc_sync,
                "persona_state_log",
                "state_json",
                limit,
            )

    async def read_important(self, default: Any = None) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._read_important_sync, default)

    async def write_important(self, data: Any) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_important_sync, data)

    async def important_count(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._important_count_sync)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema(conn)
        return conn

    def _load_sync(self) -> None:
        with self._connect():
            return None

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        previous_schema_version = _current_schema_version(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_state_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS effects (
                effect_id TEXT PRIMARY KEY,
                effect_json TEXT NOT NULL,
                expires_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                todo_id TEXT PRIMARY KEY,
                todo_json TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cues (
                cue_id TEXT PRIMARY KEY,
                cue_json TEXT NOT NULL,
                expires_at TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inner_monologues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monologue_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS important_memories (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                memories_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_trajectories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trajectory_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS persona_arc (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sleep_records (
                record_id TEXT PRIMARY KEY,
                record_json TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eat_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT,
                record_json TEXT NOT NULL,
                ended_at TEXT,
                status TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        had_eat_record_id = _column_exists(conn, "eat_records", "record_id")
        _ensure_column(conn, "eat_records", "record_id", "TEXT")
        _ensure_column(conn, "eat_records", "ended_at", "TEXT")
        _ensure_column(conn, "eat_records", "status", "TEXT")
        if previous_schema_version is None or not had_eat_record_id:
            _backfill_eat_record_ids(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_effects_expires ON effects(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_completed ON todos(completed)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cues_expires ON cues(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_monologues_recent ON inner_monologues(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trajectories_recent ON daily_trajectories(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sleep_started ON sleep_records(started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eat_record_id ON eat_records(record_id)")
        conn.execute(
            """
            INSERT INTO schema_version (id, version, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET version = excluded.version, updated_at = excluded.updated_at
            """,
            (SCHEMA_VERSION, _now_text()),
        )
        conn.commit()

    def _get_state_sync(self, default: Any) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT state_json FROM persona_state WHERE id = 1").fetchone()
        if row is None:
            return default
        return _json_loads(row["state_json"], default={})

    def _save_state_sync(self, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO persona_state (id, state_json, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (_json_dumps(data), _now_text()),
            )
            conn.commit()

    def _append_state_log_sync(self, data: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO persona_state_log (state_json, created_at)
                VALUES (?, ?)
                """,
                (_json_dumps(data), _now_text()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _upsert_effect_sync(self, effect_id: str, data: dict[str, Any]) -> None:
        now = _now_text()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO effects (effect_id, effect_json, expires_at, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(effect_id) DO UPDATE SET
                    effect_json = excluded.effect_json,
                    expires_at = excluded.expires_at,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    effect_id,
                    _json_dumps(data),
                    _optional_text(data, ("expires_at", "expire_at", "until", "end_at")),
                    1 if _record_active(data) else 0,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _get_active_effects_sync(self, now: Any) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT effect_json FROM effects
                WHERE active = 1
                ORDER BY created_at ASC, effect_id ASC
                """
            ).fetchall()
        records = [_json_loads(row["effect_json"], default={}) for row in rows]
        return [
            record for record in records
            if isinstance(record, dict) and _record_active(record) and not _is_expired(record, now)
        ]

    def _get_todos_sync(self, include_completed: bool) -> list[dict[str, Any]]:
        where = "" if include_completed else "WHERE completed = 0"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT todo_json FROM todos
                {where}
                ORDER BY expires_at IS NULL ASC, expires_at ASC, created_at ASC, todo_id ASC
                """
            ).fetchall()
        records = [
            record for row in rows
            if isinstance(record := _json_loads(row["todo_json"], default={}), dict)
        ]
        if include_completed:
            return sorted(records, key=_todo_readable_sort_key)
        now = _now_value(None)
        open_records = [
            record for record in records
            if not _is_expired(record, now)
        ]
        return sorted(open_records, key=_todo_readable_sort_key)

    def _upsert_todo_sync(self, todo_id: str, data: dict[str, Any]) -> None:
        now = _now_text()
        with self._connect() as conn:
            existing_row = conn.execute(
                "SELECT todo_json FROM todos WHERE todo_id = ?",
                (todo_id,),
            ).fetchone()
            if existing_row is not None:
                existing = _json_loads(existing_row["todo_json"], default={})
                if isinstance(existing, dict):
                    data = {**existing, **data}
                    data["id"] = todo_id
            conn.execute(
                """
                INSERT INTO todos (todo_id, todo_json, completed, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(todo_id) DO UPDATE SET
                    todo_json = excluded.todo_json,
                    completed = excluded.completed,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    todo_id,
                    _json_dumps(data),
                    1 if _record_completed(data) else 0,
                    _optional_text(data, ("expires_at", "expire_at", "until", "end_at")),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _mark_expired_todos_missed_sync(self, now: Any) -> int:
        updated = 0
        updated_at = _now_text()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT todo_id, todo_json FROM todos
                WHERE completed = 0
                """
            ).fetchall()
            for row in rows:
                record = _json_loads(row["todo_json"], default={})
                if not isinstance(record, dict) or not _is_expired(record, now):
                    continue
                missed = {**record, "id": str(row["todo_id"]), "status": "missed", "completed": True}
                cur = conn.execute(
                    """
                    UPDATE todos
                    SET todo_json = ?, completed = 1, updated_at = ?
                    WHERE todo_id = ? AND completed = 0
                    """,
                    (_json_dumps(missed), updated_at, str(row["todo_id"])),
                )
                updated += int(cur.rowcount)
            if updated:
                conn.commit()
            return updated

    def _get_cues_sync(self, now: Any) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT cue_json FROM cues
                WHERE active = 1
                ORDER BY created_at ASC, cue_id ASC
                """
            ).fetchall()
        records = [_json_loads(row["cue_json"], default={}) for row in rows]
        return [
            record for record in records
            if isinstance(record, dict) and _record_active(record) and not _is_expired(record, now)
        ]

    def _upsert_cue_sync(self, cue_id: str, data: dict[str, Any]) -> None:
        now = _now_text()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cues (cue_id, cue_json, expires_at, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cue_id) DO UPDATE SET
                    cue_json = excluded.cue_json,
                    expires_at = excluded.expires_at,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    cue_id,
                    _json_dumps(data),
                    _optional_text(data, ("expires_at", "expire_at", "until", "end_at")),
                    1 if _record_active(data) else 0,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _get_profile_sync(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT profile_json FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        data = _json_loads(row["profile_json"], default={})
        return data if isinstance(data, dict) else {}

    def _upsert_profile_sync(self, user_id: str, data: dict[str, Any]) -> None:
        now = _now_text()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_profiles (user_id, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, _json_dumps(data), now, now),
            )
            conn.commit()

    def _all_profiles_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT profile_json FROM user_profiles ORDER BY user_id ASC"
            ).fetchall()
        return [
            record for row in rows
            if isinstance(record := _json_loads(row["profile_json"], default={}), dict)
        ]

    def _insert_json_row_sync(
        self,
        table: str,
        json_column: str,
        data: dict[str, Any],
    ) -> int:
        _validate_table_name(table)
        _validate_table_name(json_column)
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                INSERT INTO {table} ({json_column}, created_at)
                VALUES (?, ?)
                """,
                (_json_dumps(data), _now_text()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _recent_json_rows_sync(
        self,
        table: str,
        json_column: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        _validate_table_name(table)
        _validate_table_name(json_column)
        limit = _clamp_int(limit, default=20, minimum=1, maximum=500)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {json_column}
                FROM (
                    SELECT id, {json_column} FROM {table}
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (limit,),
            ).fetchall()
        return [
            record for row in rows
            if isinstance(record := _json_loads(row[json_column], default={}), dict)
        ]

    def _recent_json_rows_desc_sync(
        self,
        table: str,
        json_column: str,
        limit: int,
        order_column: str = "id",
        tie_breaker_column: str = "id",
    ) -> list[dict[str, Any]]:
        _validate_table_name(table)
        _validate_table_name(json_column)
        _validate_table_name(order_column)
        _validate_table_name(tie_breaker_column)
        limit = _clamp_int(limit, default=20, minimum=1, maximum=500)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {json_column}
                FROM {table}
                ORDER BY {order_column} DESC, {tie_breaker_column} DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            record for row in rows
            if isinstance(record := _json_loads(row[json_column], default={}), dict)
        ]

    def _upsert_sleep_record_sync(self, record_id: str, data: dict[str, Any]) -> None:
        now = _now_text()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sleep_records (
                    record_id, record_json, started_at, ended_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    record_json = excluded.record_json,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    updated_at = excluded.updated_at
                """,
                (
                    record_id,
                    _json_dumps(data),
                    _optional_text(data, ("started_at", "start_at", "start")),
                    _optional_text(data, ("ended_at", "end_at", "end")),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _update_sleep_record_sync(self, record_id: str, updates: dict[str, Any]) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM sleep_records WHERE record_id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                return False
            data = _json_loads(row["record_json"], default={})
            if not isinstance(data, dict):
                data = {}
            data.update(updates)
            data.setdefault("id", record_id)
            data.setdefault("record_id", record_id)
            conn.execute(
                """
                UPDATE sleep_records
                SET record_json = ?,
                    started_at = ?,
                    ended_at = ?,
                    updated_at = ?
                WHERE record_id = ?
                """,
                (
                    _json_dumps(data),
                    _optional_text(data, ("started_at", "start_at", "start")),
                    _optional_text(data, ("ended_at", "end_at", "end")),
                    _now_text(),
                    record_id,
                ),
            )
            conn.commit()
            return True

    def _add_eat_record_sync(self, data: dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO eat_records (record_id, record_json, ended_at, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _optional_text(data, ("record_id", "eat_id", "id")),
                    _json_dumps(data),
                    _optional_text(data, ("ended_at", "end_at", "end")),
                    _optional_text(data, ("status",)),
                    _now_text(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _update_eat_record_sync(self, record_id: str, updates: dict[str, Any]) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, record_json FROM eat_records WHERE record_id = ? ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if row is None:
                return False
            data = _json_loads(row["record_json"], default={})
            if not isinstance(data, dict):
                data = {}
            data.update(updates)
            data.setdefault("id", record_id)
            data.setdefault("record_id", record_id)
            conn.execute(
                """
                UPDATE eat_records
                SET record_json = ?,
                    ended_at = ?,
                    status = ?
                WHERE id = ?
                """,
                (
                    _json_dumps(data),
                    _optional_text(data, ("ended_at", "end_at", "end")),
                    _optional_text(data, ("status",)),
                    row["id"],
                ),
            )
            conn.commit()
            return True

    def _delete_by_ids_sync(self, table: str, id_column: str, ids: list[str]) -> int:
        _validate_table_name(table)
        _validate_table_name(id_column)
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {id_column} IN ({placeholders})",
                ids,
            )
            conn.commit()
            return int(cur.rowcount)

    def _expire_records_sync(
        self,
        table: str,
        id_column: str,
        json_column: str,
        now: Any,
    ) -> int:
        _validate_table_name(table)
        _validate_table_name(id_column)
        _validate_table_name(json_column)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {id_column}, {json_column} FROM {table}"
            ).fetchall()
            expired_ids: list[str] = []
            for row in rows:
                record = _json_loads(row[json_column], default={})
                if isinstance(record, dict) and _is_expired(record, now):
                    expired_ids.append(str(row[id_column]))
            if not expired_ids:
                return 0
            placeholders = ",".join("?" for _ in expired_ids)
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {id_column} IN ({placeholders})",
                expired_ids,
            )
            conn.commit()
            return int(cur.rowcount)

    def _read_important_sync(self, default: Any) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT memories_json FROM important_memories WHERE id = 1"
            ).fetchone()
        if row is None:
            return default if default is not None else []
        return _json_loads(row["memories_json"], default=default if default is not None else [])

    def _write_important_sync(self, data: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO important_memories (id, memories_json, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    memories_json = excluded.memories_json,
                    updated_at = excluded.updated_at
                """,
                (_json_dumps(data), _now_text()),
            )
            conn.commit()

    def _important_count_sync(self) -> int:
        data = self._read_important_sync(default=[])
        return len(data) if isinstance(data, list) else 0


def _record_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return dict(record)
    if is_dataclass(record) and not isinstance(record, type):
        return dict(asdict(record))
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True))
    if isinstance(record, Mapping):
        return dict(record)
    attrs = getattr(record, "__dict__", None)
    if isinstance(attrs, dict):
        return {
            key: value
            for key, value in attrs.items()
            if not key.startswith("_") and not callable(value)
        }
    raise TypeError(f"unsupported persona record type: {type(record)!r}")


def _adapt_record(data: Any, type_names: tuple[str, ...]) -> Any:
    if data is None:
        return None
    if not isinstance(data, dict):
        return data
    cls = _find_mind_type(type_names)
    if cls is None:
        return dict(data)
    try:
        prepared = _prepare_dataclass_data(cls, data)
        if is_dataclass(cls):
            allowed = {field.name for field in fields(cls)}
            return cls(**{key: value for key, value in prepared.items() if key in allowed})
        return cls(**prepared)
    except Exception as e:  # noqa: BLE001
        logger.debug("mind.types 记录实例化失败，退回 dict: %s", e)
        return dict(data)


def _prepare_dataclass_data(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(data)
    name = cls.__name__
    if name == "PersonaState":
        if "action" in prepared and "current_action" not in prepared:
            prepared["current_action"] = prepared["action"]
    elif name == "Todo":
        prepared = {
            "id": _text_from(prepared, ("id", "todo_id"), default=""),
            "title": _text_from(prepared, ("title", "text", "name", "summary"), default=""),
            "reason": _text_from(prepared, ("reason", "source_detail", "detail"), default=""),
            "priority": _int_from(prepared, ("priority",), default=0),
            "scope": _text_from(prepared, ("scope",), default="persona"),
            "created_at": _time_from(prepared, ("created_at", "timestamp"), default=0.0),
            "expires_at": _optional_time_from(prepared, ("expires_at", "expire_at", "until", "end_at")),
            "status": _text_from(prepared, ("status",), default="completed" if _record_completed(prepared) else "open"),
            "completed": _record_completed(prepared),
        }
    elif name == "Effect":
        prepared = {
            "id": _text_from(prepared, ("id", "effect_id"), default=""),
            "name": _text_from(prepared, ("name", "title", "summary"), default=""),
            "effect_type": _text_from(prepared, ("effect_type", "type", "kind"), default="general"),
            "intensity": _float_from(prepared, ("intensity", "value"), default=0.0),
            "prompt_hint": _text_from(prepared, ("prompt_hint", "hint", "description"), default=""),
            "source_detail": _text_from(prepared, ("source_detail", "source", "reason"), default=""),
            "created_at": _time_from(prepared, ("created_at", "timestamp"), default=0.0),
            "expires_at": _time_from(prepared, ("expires_at", "expire_at", "until", "end_at"), default=0.0),
        }
    elif name == "UserProfile":
        prepared = {
            "user_id": _text_from(prepared, ("user_id", "profile_id", "id"), default=""),
            "display_name": _text_from(prepared, ("display_name", "nickname", "name"), default=""),
            "affinity": _float_from(prepared, ("affinity",), default=0.0),
            "summary": _text_from(prepared, ("summary", "description"), default=""),
            "traits": _text_list_from(prepared, ("traits", "facts")),
            "interaction_count": _int_from(prepared, ("interaction_count",), default=0),
            "last_interaction_at": _time_from(prepared, ("last_interaction_at",), default=0.0),
        }
        attributes = data.get("attributes")
        if not prepared["traits"] and isinstance(attributes, Mapping):
            prepared["traits"] = _text_list_value(attributes.get("traits") or attributes.get("facts"))
    elif name == "Cue":
        prepared = {
            "id": _text_from(prepared, ("id", "cue_id"), default=""),
            "cue_type": _text_from(prepared, ("cue_type", "type", "kind"), default="general"),
            "summary": _text_from(prepared, ("summary", "text", "name"), default=""),
            "conversation_id": _text_from(prepared, ("conversation_id", "conversation", "scope"), default=""),
            "created_at": _time_from(prepared, ("created_at", "timestamp"), default=0.0),
            "expires_at": _time_from(prepared, ("expires_at", "expire_at", "until", "end_at"), default=0.0),
        }
    return prepared


def _first_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _text_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: str) -> str:
    value = _first_value(data, keys)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _float_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: float) -> float:
    value = _first_value(data, keys)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: int) -> int:
    value = _first_value(data, keys)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _time_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: float) -> float:
    value = _first_value(data, keys)
    if value in (None, ""):
        return default
    kind, normalized = _time_sort_value(value)
    if kind == 0 and isinstance(normalized, int | float):
        return float(normalized)
    return default


def _optional_time_from(data: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    value = _first_value(data, keys)
    if value in (None, ""):
        return None
    return _time_from(data, keys, default=0.0)


def _text_list_from(data: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    return _text_list_value(_first_value(data, keys))


def _text_list_value(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _find_mind_type(type_names: tuple[str, ...]) -> type | None:
    try:
        module = importlib.import_module("mind.types")
    except ModuleNotFoundError:
        return None
    for name in type_names:
        value = getattr(module, name, None)
        if isinstance(value, type):
            return value
    return None


def _ensure_record_id(data: dict[str, Any], keys: tuple[str, ...], prefix: str) -> str:
    record_id = _optional_text(data, keys)
    if not record_id:
        record_id = f"{prefix}_{uuid4().hex[:16]}"
    for key in keys:
        if key in data:
            data[key] = record_id
            break
    else:
        data["id"] = record_id
    return record_id


def _optional_text(data: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _record_active(data: Mapping[str, Any]) -> bool:
    for key in ("active", "enabled"):
        if key in data:
            return bool(data.get(key))
    return True


def _record_completed(data: Mapping[str, Any]) -> bool:
    for key in ("completed", "done", "finished"):
        if key in data:
            return _explicit_completed_value(data.get(key))
    status = str(data.get("status") or "").strip().lower()
    return status in _COMPLETED_STATUS_VALUES


def _todo_readable_sort_key(data: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -_int_from(data, ("priority",), default=0),
        _time_sort_value(_first_value(data, ("expires_at", "expire_at", "until", "end_at"))),
        _time_sort_value(_first_value(data, ("created_at", "timestamp"))),
        str(_first_value(data, ("id", "todo_id")) or ""),
    )


def _explicit_completed_value(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return False
        if text in _COMPLETED_STATUS_VALUES:
            return True
        if text in _INCOMPLETE_STATUS_VALUES:
            return False
    return bool(value)


def _is_expired(data: Mapping[str, Any], now: Any) -> bool:
    expires_at = _optional_text(data, ("expires_at", "expire_at", "until", "end_at"))
    if not expires_at:
        return False
    expires_value = _time_value(expires_at)
    now_value = _time_value(now)
    if expires_value is None or now_value is None:
        return False
    return expires_value <= now_value


def _compare_time(left: Any, right: Any) -> int:
    left_value = _time_sort_value(left)
    right_value = _time_sort_value(right)
    if left_value < right_value:
        return -1
    if left_value > right_value:
        return 1
    return 0


def _time_sort_value(value: Any) -> tuple[int, float | str]:
    parsed = _time_value(value)
    if parsed is not None:
        return (0, parsed)
    if value is None:
        return (1, _now_text())
    text = str(value).strip()
    return (1, text)


def _time_value(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    normalized = _normalize_iso_time_text(text)
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        pass
    legacy = text.replace("T", " ")
    for candidate in (legacy[:19], legacy[:16], legacy[:10]):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                return datetime.strptime(candidate, fmt).timestamp()
            except ValueError:
                pass
    return None


def _normalize_iso_time_text(value: str) -> str:
    text = value.strip()
    if text.endswith("Z"):
        return f"{text[:-1]}+00:00"
    if text.endswith("z"):
        return f"{text[:-1]}+00:00"
    return text


def _now_value(now: Any) -> Any:
    return _now_text() if now is None else now


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: Any, *, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _clean_ids(ids: str | Iterable[str]) -> list[str]:
    if isinstance(ids, str):
        ids = [ids]
    return [text for item in ids if (text := str(item or "").strip())]


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    _validate_table_name(table)
    _validate_table_name(column)
    return any(
        str(row[1]) == column
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _current_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _backfill_eat_record_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT rowid, id, record_json
        FROM eat_records
        WHERE record_id IS NULL OR TRIM(record_id) = ''
        ORDER BY id ASC
        """
    ).fetchall()
    for rowid_value, id_value, record_json in rows:
        data = _json_loads(record_json, default={})
        if isinstance(data, dict):
            record_id = _optional_text(data, ("record_id", "eat_id", "id"))
        else:
            record_id = None
        if not record_id:
            fallback = str(id_value if id_value not in (None, "") else rowid_value).strip()
            record_id = f"eat_{fallback}"
        if isinstance(data, dict):
            changed = False
            if not _optional_text(data, ("record_id",)):
                data["record_id"] = record_id
                changed = True
            if not _optional_text(data, ("id",)):
                data["id"] = record_id
                changed = True
            if changed:
                conn.execute(
                    """
                    UPDATE eat_records
                    SET record_id = ?,
                        record_json = ?
                    WHERE rowid = ?
                    """,
                    (record_id, _json_dumps(data), rowid_value),
                )
                continue
        conn.execute(
            "UPDATE eat_records SET record_id = ? WHERE rowid = ?",
            (record_id, rowid_value),
        )


def _validate_table_name(name: str) -> None:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"invalid sqlite identifier: {name!r}")
