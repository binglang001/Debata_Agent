"""diana.db 轻量仓储适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

from mind import db_records as _persona_records
from mind import db_schema as _persona_schema
from providers.base import Usage
from utils.usage_summary import UsageRange, UsageSummary, cutoff_timestamp

from .archive_sqlite_filters import (
    _filter_order_sql,
    _filter_row_sort_key,
    _filter_sql_plan,
    _placeholders,
    _row_matches_filter,
    _unique_rows_by_rowid,
)
from .archive_sqlite_records import (
    _base36,
    _clamp_int,
    _clean_id,
    _extract_media,
    _json_dumps,
    _json_loads,
    _legacy_search_text,
    _normalize_record,
    _now_text,
    _query_to_dict,
    _row_is_real_chat,
    _row_to_light_result,
    _row_to_record,
    real_chat_archive_records,
)
from .diana_db import DianaDB
from .event_store import (
    _clamp_limit,
    _clean_optional,
    _clean_required,
    _cursor_where,
    _normalize_event,
    _normalize_order,
    _PendingEvent,
    _positive_int_or_none,
    _row_to_event,
)

logger = logging.getLogger(__name__)


class DianaHistoryStore:
    """使用 diana.db 的 history_records 表实现 JsonlStore 等价接口。"""

    def __init__(self, db: DianaDB | str | Path, persona_id: str) -> None:
        self._db = db if isinstance(db, DianaDB) else DianaDB(db)
        self.persona_id = str(persona_id).strip()
        if not self.persona_id:
            raise ValueError("persona_id must not be empty")
        self._lock = asyncio.Lock()
        self._cache: list[dict] | None = None

    @property
    def db(self) -> DianaDB:
        return self._db

    async def load(self, force_reload: bool = False) -> list[dict]:
        """加载当前 persona 的完整历史流，返回缓存列表的浅拷贝。"""

        async with self._lock:
            if self._cache is None or force_reload:
                self._cache = await asyncio.to_thread(self._load_sync)
            return list(self._cache or [])

    async def append(self, record: dict) -> None:
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            await asyncio.to_thread(self._append_many_sync, [record])
            self._cache.append(record)  # type: ignore[union-attr]

    async def append_many(self, records: list[dict]) -> None:
        if not records:
            return
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            await asyncio.to_thread(self._append_many_sync, records)
            self._cache.extend(records)  # type: ignore[union-attr]

    async def length(self) -> int:
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            return len(self._cache or [])

    async def get_slice(self, start: int = 0, end: int | None = None) -> list[dict]:
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            cache = self._cache or []
            if end is None:
                return list(cache[start:])
            return list(cache[start:end])

    async def truncate_head(self, cut_point: int) -> int:
        """删除当前 persona 最早的 cut_point 条记录，返回剩余长度。"""

        async with self._lock:
            await self._ensure_cache_loaded_locked()
            cache = self._cache or []
            if cut_point <= 0:
                return len(cache)
            remaining = list(cache[cut_point:])
            await asyncio.to_thread(self._replace_all_sync, remaining)
            self._cache = remaining
            return len(remaining)

    async def replace_all(self, records: list[dict]) -> None:
        """整体替换当前 persona 的历史流。"""

        async with self._lock:
            cache = list(records)
            await asyncio.to_thread(self._replace_all_sync, cache)
            self._cache = cache

    async def clear(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._clear_sync)
            self._cache = []

    async def _ensure_cache_loaded_locked(self) -> None:
        if self._cache is None:
            self._cache = await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, record_json FROM history_records
                WHERE persona_id = ?
                ORDER BY history_index ASC
                """,
                (self.persona_id,),
            ).fetchall()
        records: list[dict] = []
        for row in rows:
            try:
                record = orjson.loads(row["record_json"])
            except orjson.JSONDecodeError:
                logger.warning(
                    "跳过损坏的 diana history record persona_id=%s rowid=%s",
                    self.persona_id,
                    row["id"],
                )
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _append_many_sync(self, records: list[dict]) -> None:
        if not records:
            return
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                start_index = int(
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(history_index), -1) + 1
                        FROM history_records
                        WHERE persona_id = ?
                        """,
                        (self.persona_id,),
                    ).fetchone()[0]
                )
                self._insert_records(conn, records, start_index=start_index)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _replace_all_sync(self, records: list[dict]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM history_records WHERE persona_id = ?",
                    (self.persona_id,),
                )
                self._insert_records(conn, records, start_index=0)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _clear_sync(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM history_records WHERE persona_id = ?",
                    (self.persona_id,),
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _connect(self):
        db = DianaDB(self._db.path, busy_timeout_ms=self._db.busy_timeout_ms)
        db.load()
        return db.connect()

    def _insert_records(
        self,
        conn: sqlite3.Connection,
        records: list[dict],
        *,
        start_index: int,
    ) -> None:
        rows = []
        for offset, record in enumerate(records):
            content_length, content_hash = _content_fingerprint(record.get("content"))
            rows.append(
                (
                    self.persona_id,
                    start_index + offset,
                    _history_record_conversation_id(record),
                    _optional_text(record.get("role")),
                    content_hash,
                    content_length,
                    _record_json(record),
                    _optional_text(record.get("timestamp")),
                )
            )
        if not rows:
            return
        conn.executemany(
            """
            INSERT INTO history_records (
                persona_id, history_index, conversation_id, role,
                content_hash, content_length, record_json, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


class DianaArchiveStore:
    """使用 diana.db 的 archive_messages 表实现 ArchiveStore 等价接口。"""

    def __init__(self, db: DianaDB | str | Path, persona_id: str) -> None:
        self._db = db if isinstance(db, DianaDB) else DianaDB(db)
        self.persona_id = str(persona_id).strip()
        if not self.persona_id:
            raise ValueError("persona_id must not be empty")
        self._lock = asyncio.Lock()

    @property
    def db(self) -> DianaDB:
        return self._db

    async def load(self, force_reload: bool = False) -> list[dict]:
        _ = force_reload
        return await self.records()

    async def append_many(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        payload: list[dict[str, Any]] = []
        for record in records:
            if isinstance(record, dict):
                payload.extend(real_chat_archive_records(record))
        if not payload:
            return
        async with self._lock:
            await asyncio.to_thread(self._append_many_sync, payload)

    async def records(self) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(self._records_sync)

    async def search(
        self,
        *,
        conversation_id: str | None = None,
        keyword: str | None = None,
        time_range: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(
                self._legacy_search_sync,
                conversation_id,
                keyword,
                time_range,
                limit,
            )

    async def filter_records(self, query: Any) -> dict[str, Any]:
        query_dict = _query_to_dict(query)
        started_at = time.perf_counter()
        async with self._lock:
            result = await asyncio.to_thread(self._filter_records_sync, query_dict)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Diana archive filter_records 指标 persona_id=%s limit=%s offset=%s "
                "returned=%s total=%s elapsed_ms=%.3f",
                self.persona_id,
                result.get("limit"),
                result.get("offset"),
                result.get("count"),
                result.get("total"),
                (time.perf_counter() - started_at) * 1000,
            )
        return result

    async def get_by_ids(self, archive_ids: list[str]) -> list[dict]:
        ids = [_clean_id(value) for value in archive_ids]
        ids = [value for value in ids if value]
        if not ids:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._get_by_ids_sync, ids)

    async def context_around(
        self,
        archive_id: str,
        before: int,
        after: int,
    ) -> list[dict]:
        archive_id = _clean_id(archive_id)
        if not archive_id:
            return []
        async with self._lock:
            return await asyncio.to_thread(
                self._context_around_sync,
                archive_id,
                max(0, before),
                max(0, after),
            )

    async def rag_records(self) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(self._rag_records_sync)

    async def media_records(self, archive_id: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._media_records_sync, archive_id)

    def _connect(self) -> sqlite3.Connection:
        db = DianaDB(self._db.path, busy_timeout_ms=self._db.busy_timeout_ms)
        db.load()
        return db.connect()

    def _append_many_sync(self, records: list[dict[str, Any]]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = _now_text()
                existing_record_json = self._existing_record_json(conn, records)
                seen_record_json: set[str] = set()
                pending: list[tuple[Any, str, str]] = []
                for record in records:
                    normalized = _normalize_record(record)
                    record_json = _json_dumps(normalized.record)
                    if record_json in existing_record_json or record_json in seen_record_json:
                        continue
                    seen_record_json.add(record_json)
                    pending.append(
                        (normalized, record_json, _json_dumps(normalized.metadata))
                    )
                if not pending:
                    conn.commit()
                    return

                next_rowid = int(
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(rowid), 0) + 1
                        FROM archive_messages
                        WHERE persona_id = ?
                        """,
                        (self.persona_id,),
                    ).fetchone()[0]
                )
                next_media_id = int(
                    conn.execute(
                        """
                        SELECT COALESCE(MAX(id), 0) + 1
                        FROM archive_message_media
                        WHERE persona_id = ?
                        """,
                        (self.persona_id,),
                    ).fetchone()[0]
                )
                message_rows: list[tuple[Any, ...]] = []
                media_rows: list[tuple[Any, ...]] = []
                for offset, (normalized, record_json, metadata_json) in enumerate(pending):
                    rowid = next_rowid + offset
                    archive_id = "a" + _base36(rowid)
                    message_rows.append(
                        (
                            self.persona_id,
                            rowid,
                            archive_id,
                            normalized.timestamp,
                            normalized.timestamp_unix,
                            normalized.date_key,
                            normalized.month_key,
                            normalized.conversation_id,
                            normalized.conversation_type,
                            normalized.target_id,
                            normalized.sender_id,
                            normalized.sender_name,
                            normalized.sender_role,
                            normalized.direction,
                            normalized.message_kind,
                            normalized.content,
                            normalized.content_search,
                            normalized.original_msg_id,
                            normalized.reply_to_msg_id,
                            metadata_json,
                            record_json,
                            now,
                        )
                    )
                    for item in _extract_media(normalized.record.get("content")):
                        media_rows.append(
                            (
                                self.persona_id,
                                next_media_id + len(media_rows),
                                archive_id,
                                item["media_type"],
                                item.get("workspace_path"),
                                item.get("original_name"),
                                _json_dumps(item.get("metadata") or {}),
                            )
                        )
                conn.executemany(
                    """
                    INSERT INTO archive_messages (
                        persona_id, rowid, archive_id, timestamp, timestamp_unix,
                        date_key, month_key, conversation_id, conversation_type,
                        target_id, sender_id, sender_name, sender_role, direction,
                        message_kind, content, content_search, original_msg_id,
                        reply_to_msg_id, metadata_json, record_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    message_rows,
                )
                if media_rows:
                    conn.executemany(
                        """
                        INSERT INTO archive_message_media (
                            persona_id, id, archive_id, media_type, workspace_path,
                            original_name, metadata_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        media_rows,
                    )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _existing_record_json(
        self,
        conn: sqlite3.Connection,
        records: list[dict[str, Any]],
    ) -> set[str]:
        record_json_values = list(
            dict.fromkeys(_json_dumps(_normalize_record(record).record) for record in records)
        )
        existing: set[str] = set()
        for start in range(0, len(record_json_values), 500):
            chunk = record_json_values[start:start + 500]
            if not chunk:
                continue
            placeholders = _placeholders(len(chunk))
            rows = conn.execute(
                f"""
                SELECT record_json FROM archive_messages
                WHERE persona_id = ? AND record_json IN ({placeholders})
                """,
                [self.persona_id, *chunk],
            ).fetchall()
            existing.update(str(row["record_json"]) for row in rows if row["record_json"])
        return existing

    def _records_sync(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM archive_messages
                WHERE persona_id = ?
                ORDER BY rowid ASC
                """,
                (self.persona_id,),
            ).fetchall()
        return [_row_to_record(row) for row in rows if _row_is_real_chat(row)]

    def _legacy_search_sync(
        self,
        conversation_id: str | None,
        keyword: str | None,
        time_range: str | None,
        limit: int,
    ) -> list[dict]:
        keyword = (keyword or "").strip()
        time_range = (time_range or "").strip()
        matched: list[dict] = []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM archive_messages
                WHERE persona_id = ?
                ORDER BY rowid ASC
                """,
                (self.persona_id,),
            ).fetchall()
        for row in rows:
            if not _row_is_real_chat(row):
                continue
            if conversation_id and row["conversation_id"] != conversation_id:
                continue
            text = _legacy_search_text(row)
            if keyword and keyword not in text:
                continue
            if time_range and time_range not in text:
                continue
            matched.append(_row_to_record(row))
        return matched[-max(1, limit):]

    def _filter_records_sync(self, query: dict[str, Any]) -> dict[str, Any]:
        limit = _clamp_int(query.get("limit"), default=50, minimum=1, maximum=500)
        offset = _clamp_int(query.get("offset"), default=0, minimum=0, maximum=1_000_000)
        order = str(query.get("order") or "desc").lower()
        reverse = order != "asc"
        plan = _filter_sql_plan(query)
        order_sql = _filter_order_sql(reverse)
        base_sql = f"""
            FROM archive_messages
            WHERE persona_id = ? AND {plan.where_sql}
            """
        base_params = [self.persona_id, *plan.params]
        with closing(self._connect()) as conn:
            if plan.has_python_residual_filter:
                rows = conn.execute(
                    f"SELECT * {base_sql} ORDER BY {order_sql}",
                    base_params,
                ).fetchall()
                if plan.fallback_where_sql is not None:
                    fallback_base_sql = f"""
                        FROM archive_messages
                        WHERE persona_id = ? AND {plan.fallback_where_sql}
                        """
                    fallback_rows = conn.execute(
                        f"SELECT * {fallback_base_sql} ORDER BY {order_sql}",
                        [self.persona_id, *(plan.fallback_params or [])],
                    ).fetchall()
                    rows = _unique_rows_by_rowid([*rows, *fallback_rows])
                    rows.sort(key=_filter_row_sort_key, reverse=reverse)
                matched = [
                    row for row in rows
                    if _row_is_real_chat(row) and _row_matches_filter(row, query)
                ]
                total = len(matched)
                selected = matched[offset:offset + limit]
            else:
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) {base_sql}",
                        base_params,
                    ).fetchone()[0]
                )
                selected = conn.execute(
                    f"SELECT * {base_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    [*base_params, limit, offset],
                ).fetchall()
        return {
            "ok": True,
            "count": len(selected),
            "total": total,
            "limit": limit,
            "offset": offset,
            "order": "desc" if reverse else "asc",
            "results": [_row_to_light_result(row) for row in selected],
            "next": "如需完整上下文，把 results[].id 传给 recall_history 的 archive_ids。",
        }

    def _get_by_ids_sync(self, archive_ids: list[str]) -> list[dict]:
        placeholders = ",".join("?" for _ in archive_ids)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM archive_messages
                WHERE persona_id = ? AND archive_id IN ({placeholders})
                """,
                [self.persona_id, *archive_ids],
            ).fetchall()
        by_id = {
            row["archive_id"]: _row_to_record(row)
            for row in rows
            if _row_is_real_chat(row)
        }
        return [by_id[archive_id] for archive_id in archive_ids if archive_id in by_id]

    def _context_around_sync(
        self,
        archive_id: str,
        before: int,
        after: int,
    ) -> list[dict]:
        with closing(self._connect()) as conn:
            target = conn.execute(
                """
                SELECT * FROM archive_messages
                WHERE persona_id = ? AND archive_id = ?
                """,
                (self.persona_id, archive_id),
            ).fetchone()
            if target is None or not _row_is_real_chat(target):
                return []
            conversation_id = target["conversation_id"]
            if conversation_id is None:
                prev_rows = []
                next_rows = []
            else:
                prev_rows = conn.execute(
                    """
                    SELECT * FROM archive_messages
                    WHERE persona_id = ? AND conversation_id = ? AND rowid < ?
                    ORDER BY rowid DESC
                    """,
                    (self.persona_id, conversation_id, target["rowid"]),
                ).fetchall()
                next_rows = conn.execute(
                    """
                    SELECT * FROM archive_messages
                    WHERE persona_id = ? AND conversation_id = ? AND rowid > ?
                    ORDER BY rowid ASC
                    """,
                    (self.persona_id, conversation_id, target["rowid"]),
                ).fetchall()
        prev_real = [row for row in prev_rows if _row_is_real_chat(row)][:before]
        next_real = [row for row in next_rows if _row_is_real_chat(row)][:after]
        rows = list(reversed(prev_real)) + [target] + next_real
        return [_row_to_record(row) for row in rows]

    def _rag_records_sync(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM archive_messages
                WHERE persona_id = ?
                  AND direction IN ('inbound', 'outbound')
                  AND message_kind IN ('text', 'image', 'file', 'audio', 'forward', 'mixed')
                ORDER BY rowid ASC
                """,
                (self.persona_id,),
            ).fetchall()
        records: list[dict] = []
        for row in rows:
            if not _row_is_real_chat(row):
                continue
            record = _row_to_record(row)
            record["content"] = str(row["content_search"] or row["content"] or "")
            records.append(record)
        return records

    def _media_records_sync(self, archive_id: str | None) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            if archive_id:
                rows = conn.execute(
                    """
                    SELECT * FROM archive_message_media
                    WHERE persona_id = ? AND archive_id = ?
                    ORDER BY id ASC
                    """,
                    (self.persona_id, archive_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM archive_message_media
                    WHERE persona_id = ?
                    ORDER BY id ASC
                    """,
                    (self.persona_id,),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": row["id"],
                    "archive_id": row["archive_id"],
                    "media_type": row["media_type"],
                    "workspace_path": row["workspace_path"],
                    "original_name": row["original_name"],
                    "metadata": _json_loads(row["metadata_json"], default={}),
                }
            )
        return result


class DianaImportantStore:
    """使用 diana.db 的 important_memories 表实现 JsonStore 等价接口。"""

    def __init__(self, db: DianaDB | str | Path, persona_id: str) -> None:
        self._db = db if isinstance(db, DianaDB) else DianaDB(db)
        self.persona_id = str(persona_id).strip()
        if not self.persona_id:
            raise ValueError("persona_id must not be empty")
        self._lock = asyncio.Lock()

    @property
    def db(self) -> DianaDB:
        return self._db

    async def read(self, default: Any = None) -> Any:
        async with self._lock:
            rows = await asyncio.to_thread(self._load_rows_sync)
        if not rows:
            return default if default is not None else {}

        items: list[Any] = []
        for row in rows:
            try:
                item = orjson.loads(row["item_json"])
            except orjson.JSONDecodeError:
                logger.warning(
                    "跳过损坏的 diana important memory persona_id=%s rowid=%s",
                    self.persona_id,
                    row["id"],
                )
                continue
            if _is_legacy_raw_important_row(row):
                return item
            items.append(item)
        return items

    async def write(self, data: Any) -> None:
        async with self._lock:
            await asyncio.to_thread(self._replace_sync, data)

    def _load_rows_sync(self) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(
                """
                SELECT id, memory_id, scope, item_json FROM important_memories
                WHERE persona_id = ?
                ORDER BY id ASC
                """,
                (self.persona_id,),
            ).fetchall()

    def _replace_sync(self, data: Any) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM important_memories WHERE persona_id = ?",
                    (self.persona_id,),
                )
                self._insert_data(conn, data)
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _connect(self):
        db = DianaDB(self._db.path, busy_timeout_ms=self._db.busy_timeout_ms)
        db.load()
        return db.connect()

    def _insert_data(self, conn: sqlite3.Connection, data: Any) -> None:
        if not isinstance(data, list):
            now = _utc_timestamp()
            conn.execute(
                """
                INSERT INTO important_memories (
                    persona_id, memory_id, timestamp, scope, pinned,
                    content, item_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.persona_id,
                    _LEGACY_RAW_IMPORTANT_MEMORY_ID,
                    None,
                    _LEGACY_RAW_IMPORTANT_SCOPE,
                    0,
                    None,
                    _json_data(data),
                    now,
                ),
            )
            return

        rows = []
        used_memory_ids: set[str] = set()
        now = _utc_timestamp()
        for item in data:
            item_json = _json_data(item)
            memory_id = _unique_memory_id(
                _important_memory_id(item),
                used_memory_ids,
            )
            rows.append(
                (
                    self.persona_id,
                    memory_id,
                    _important_item_text(item, "timestamp"),
                    _important_item_text(item, "scope"),
                    _important_pinned_value(item),
                    _important_item_text(item, "content"),
                    item_json,
                    _important_item_text(item, "updated_at") or now,
                )
            )
        if not rows:
            return
        conn.executemany(
            """
            INSERT INTO important_memories (
                persona_id, memory_id, timestamp, scope, pinned,
                content, item_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


class DianaPersonaDB:
    """使用 diana.db persona_* 表实现 mind.db.PersonaDB 等价接口。"""

    _STATE_TYPES = ("PersonaState",)
    _EFFECT_TYPES = ("Effect", "PersonaEffect")
    _TODO_TYPES = ("Todo", "PersonaTodo")
    _CUE_TYPES = ("Cue", "PersonaCue")
    _PROFILE_TYPES = ("UserProfile", "PersonaUserProfile")
    _MONOLOGUE_TYPES = ("InnerMonologue",)
    _TRAJECTORY_TYPES = ("DailyTrajectory",)
    _MISSING = object()

    def __init__(self, db: DianaDB | str | Path, persona_id: str) -> None:
        self._db = db if isinstance(db, DianaDB) else DianaDB(db)
        self.persona_id = str(persona_id).strip()
        if not self.persona_id:
            raise ValueError("persona_id must not be empty")
        self._lock = asyncio.Lock()

    @property
    def db(self) -> DianaDB:
        return self._db

    async def load(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._load_sync)

    async def close(self) -> None:
        return None

    async def get_state(self, default: Any = _MISSING) -> Any:
        async with self._lock:
            data = await asyncio.to_thread(self._get_state_sync, default)
        if data is self._MISSING:
            return _persona_records._adapt_record({}, self._STATE_TYPES)
        if data is default:
            return default
        return _persona_records._adapt_record(data, self._STATE_TYPES)

    async def save_state(self, state: Any) -> None:
        data = _persona_records._record_to_dict(state)
        async with self._lock:
            await asyncio.to_thread(self._save_state_sync, data)

    async def append_state_log(self, entry: Any) -> int:
        data = _persona_records._record_to_dict(entry)
        async with self._lock:
            return await asyncio.to_thread(
                self._insert_numbered_json_row_sync,
                "persona_state_log",
                "state_json",
                data,
            )

    async def append_update_audit(self, entry: Any) -> int:
        data = _persona_records._record_to_dict(entry)
        async with self._lock:
            return await asyncio.to_thread(self._append_update_audit_sync, data)

    async def recent_update_audits(
        self,
        limit: int = 20,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_update_audits_sync,
                limit,
                conversation_id,
                user_id,
            )

    async def add_effect(self, effect: Any) -> str:
        data = _persona_records._record_to_dict(effect)
        effect_id = _persona_records._ensure_record_id(data, ("effect_id", "id"), "effect")
        async with self._lock:
            await asyncio.to_thread(self._upsert_effect_sync, effect_id, data)
        return effect_id

    async def get_active_effects(self, now: Any = None) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._get_active_records_sync,
                "persona_effects",
                "effect_json",
                "effect_id",
                _persona_records._now_value(now),
            )
        return [_persona_records._adapt_record(row, self._EFFECT_TYPES) for row in rows]

    async def remove_effects(self, ids: str | Iterable[str]) -> int:
        effect_ids = _persona_records._clean_ids(ids)
        if not effect_ids:
            return 0
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_by_ids_sync,
                "persona_effects",
                "effect_id",
                effect_ids,
            )

    async def expire_effects(self, now: Any = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._expire_records_sync,
                "persona_effects",
                "effect_id",
                "effect_json",
                _persona_records._now_value(now),
            )

    async def get_todos(self, include_completed: bool = True) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(self._get_todos_sync, include_completed)
        return [_persona_records._adapt_record(row, self._TODO_TYPES) for row in rows]

    async def upsert_todo(self, todo: Any) -> str:
        data = _persona_records._record_to_dict(todo)
        todo_id = _persona_records._ensure_record_id(data, ("todo_id", "id"), "todo")
        async with self._lock:
            await asyncio.to_thread(self._upsert_todo_sync, todo_id, data)
        return todo_id

    async def mark_expired_todos_missed(self, now: Any = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._mark_expired_todos_missed_sync,
                _persona_records._now_value(now),
            )

    async def remove_todos(self, ids: str | Iterable[str]) -> int:
        todo_ids = _persona_records._clean_ids(ids)
        if not todo_ids:
            return 0
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_by_ids_sync,
                "persona_todos",
                "todo_id",
                todo_ids,
            )

    async def get_cues(self, now: Any = None) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._get_active_records_sync,
                "persona_cues",
                "cue_json",
                "cue_id",
                _persona_records._now_value(now),
            )
        return [_persona_records._adapt_record(row, self._CUE_TYPES) for row in rows]

    async def upsert_cue(self, cue: Any) -> str:
        data = _persona_records._record_to_dict(cue)
        cue_id = _persona_records._ensure_record_id(data, ("cue_id", "id"), "cue")
        async with self._lock:
            await asyncio.to_thread(self._upsert_cue_sync, cue_id, data)
        return cue_id

    async def remove_cues(self, ids: str | Iterable[str]) -> int:
        cue_ids = _persona_records._clean_ids(ids)
        if not cue_ids:
            return 0
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_by_ids_sync,
                "persona_cues",
                "cue_id",
                cue_ids,
            )

    async def expire_cues(self, now: Any = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._expire_records_sync,
                "persona_cues",
                "cue_id",
                "cue_json",
                _persona_records._now_value(now),
            )

    async def get_profile(self, user_id: str) -> Any | None:
        clean_user_id = str(user_id or "").strip()
        if not clean_user_id:
            return None
        async with self._lock:
            data = await asyncio.to_thread(self._get_profile_sync, clean_user_id)
        return _persona_records._adapt_record(data, self._PROFILE_TYPES) if data is not None else None

    async def upsert_profile(self, profile: Any) -> str:
        data = _persona_records._record_to_dict(profile)
        user_id = _persona_records._optional_text(data, ("user_id", "profile_id", "id"))
        if not user_id:
            raise ValueError("user profile requires user_id/profile_id/id")
        data.setdefault("user_id", user_id)
        async with self._lock:
            await asyncio.to_thread(self._upsert_profile_sync, user_id, data)
        return user_id

    async def all_profiles(self) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(self._all_profiles_sync)
        return [_persona_records._adapt_record(row, self._PROFILE_TYPES) for row in rows]

    async def add_monologue(self, monologue: Any) -> int:
        data = _persona_records._record_to_dict(monologue)
        async with self._lock:
            return await asyncio.to_thread(
                self._insert_numbered_json_row_sync,
                "persona_inner_monologues",
                "monologue_json",
                data,
            )

    async def recent_monologues(self, limit: int = 20) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._recent_json_rows_sync,
                "persona_inner_monologues",
                "monologue_json",
                limit,
            )
        return [_persona_records._adapt_record(row, self._MONOLOGUE_TYPES) for row in rows]

    async def add_trajectory(self, trajectory: Any) -> int:
        data = _persona_records._record_to_dict(trajectory)
        async with self._lock:
            return await asyncio.to_thread(
                self._insert_numbered_json_row_sync,
                "persona_daily_trajectories",
                "trajectory_json",
                data,
            )

    async def recent_trajectories(self, limit: int = 20) -> list[Any]:
        async with self._lock:
            rows = await asyncio.to_thread(
                self._recent_json_rows_sync,
                "persona_daily_trajectories",
                "trajectory_json",
                limit,
            )
        return [_persona_records._adapt_record(row, self._TRAJECTORY_TYPES) for row in rows]

    async def add_arc_event(self, event: Any) -> int:
        data = _persona_records._record_to_dict(event)
        async with self._lock:
            return await asyncio.to_thread(
                self._insert_numbered_json_row_sync,
                "persona_arc",
                "event_json",
                data,
            )

    async def recent_arc_events(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_json_rows_desc_sync,
                "persona_arc",
                "event_json",
                limit,
            )

    async def add_sleep_record(self, record: Any) -> str:
        data = _persona_records._record_to_dict(record)
        record_id = _persona_records._ensure_record_id(data, ("record_id", "sleep_id", "id"), "sleep")
        async with self._lock:
            await asyncio.to_thread(self._upsert_sleep_record_sync, record_id, data)
        return record_id

    async def recent_sleep_records(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_json_rows_desc_sync,
                "persona_sleep_records",
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
            new_data = _persona_records._record_to_dict(record_id_or_record)
            if updates:
                new_data.update(dict(updates))
            new_data.update(fields_to_update)
            record_id = _persona_records._optional_text(new_data, ("record_id", "sleep_id", "id")) or ""
        if not record_id:
            return False
        async with self._lock:
            return await asyncio.to_thread(self._update_sleep_record_sync, record_id, new_data)

    async def add_eat_record(self, record: Any) -> int:
        data = _persona_records._record_to_dict(record)
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
            new_data = _persona_records._record_to_dict(record_id_or_record)
            if updates:
                new_data.update(dict(updates))
            new_data.update(fields_to_update)
            record_id = _persona_records._optional_text(new_data, ("record_id", "eat_id", "id")) or ""
        if not record_id:
            return False
        async with self._lock:
            return await asyncio.to_thread(self._update_eat_record_sync, record_id, new_data)

    async def recent_eat_records(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(
                self._recent_json_rows_desc_sync,
                "persona_eat_records",
                "record_json",
                limit,
            )

    async def recent_state_logs(self, limit: int = 50) -> list[dict[str, Any]]:
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
        db = DianaDB(self._db.path, busy_timeout_ms=self._db.busy_timeout_ms)
        db.load()
        return db.connect()

    def _load_sync(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO persona_schema_version_legacy (
                    persona_id, id, version, updated_at
                )
                VALUES (?, 1, ?, ?)
                ON CONFLICT(persona_id, id) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (self.persona_id, _persona_schema.SCHEMA_VERSION, _persona_records._now_text()),
            )
            conn.commit()

    def _get_state_sync(self, default: Any) -> Any:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT state_json FROM persona_state
                WHERE persona_id = ? AND id = 1
                """,
                (self.persona_id,),
            ).fetchone()
        if row is None:
            return default
        return _persona_records._json_loads(row["state_json"], default={})

    def _save_state_sync(self, data: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO persona_state (persona_id, id, state_json, updated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(persona_id, id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (self.persona_id, _persona_records._json_dumps(data), _persona_records._now_text()),
            )
            conn.commit()

    def _append_update_audit_sync(self, data: dict[str, Any]) -> int:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row_id = self._next_id(conn, "persona_update_audits")
                conn.execute(
                    """
                    INSERT INTO persona_update_audits (
                        persona_id, id, audit_json, "trigger",
                        conversation_id, user_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.persona_id,
                        row_id,
                        _persona_records._json_dumps(data),
                        _persona_records._optional_text(data, ("trigger",)),
                        _persona_records._optional_text(data, ("conversation_id",)),
                        _persona_records._optional_text(data, ("user_id",)),
                        _persona_records._now_text(),
                    ),
                )
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            return row_id

    def _recent_update_audits_sync(
        self,
        limit: int,
        conversation_id: str | None,
        user_id: str | None,
    ) -> list[dict[str, Any]]:
        limit = _persona_records._clamp_int(limit, default=20, minimum=1, maximum=500)
        clauses = ["persona_id = ?"]
        params: list[Any] = [self.persona_id]
        if conversation_id is not None and (text := str(conversation_id).strip()):
            clauses.append("conversation_id = ?")
            params.append(text)
        if user_id is not None and (text := str(user_id).strip()):
            clauses.append("user_id = ?")
            params.append(text)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT audit_json
                FROM persona_update_audits
                WHERE {" AND ".join(clauses)}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [
            record for row in rows
            if isinstance(
                record := _persona_records._json_loads(row["audit_json"], default={}),
                dict,
            )
        ]

    def _upsert_effect_sync(self, effect_id: str, data: dict[str, Any]) -> None:
        now = _persona_records._now_text()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO persona_effects (
                    persona_id, effect_id, effect_json, expires_at,
                    active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, effect_id) DO UPDATE SET
                    effect_json = excluded.effect_json,
                    expires_at = excluded.expires_at,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    self.persona_id,
                    effect_id,
                    _persona_records._json_dumps(data),
                    _persona_records._optional_text(
                        data,
                        ("expires_at", "expire_at", "until", "end_at"),
                    ),
                    1 if _persona_records._record_active(data) else 0,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _get_active_records_sync(
        self,
        table: str,
        json_column: str,
        id_column: str,
        now: Any,
    ) -> list[dict[str, Any]]:
        _persona_schema._validate_table_name(table)
        _persona_schema._validate_table_name(json_column)
        _persona_schema._validate_table_name(id_column)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT {json_column} FROM {table}
                WHERE persona_id = ? AND active = 1
                ORDER BY created_at ASC, {id_column} ASC
                """,
                (self.persona_id,),
            ).fetchall()
        records = [_persona_records._json_loads(row[json_column], default={}) for row in rows]
        return [
            record for record in records
            if (
                isinstance(record, dict)
                and _persona_records._record_active(record)
                and not _persona_records._is_expired(record, now)
            )
        ]

    def _get_todos_sync(self, include_completed: bool) -> list[dict[str, Any]]:
        where = "persona_id = ?"
        if not include_completed:
            where = f"{where} AND completed = 0"
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT todo_json FROM persona_todos
                WHERE {where}
                ORDER BY expires_at IS NULL ASC, expires_at ASC, created_at ASC, todo_id ASC
                """,
                (self.persona_id,),
            ).fetchall()
        records = [
            record for row in rows
            if isinstance(record := _persona_records._json_loads(row["todo_json"], default={}), dict)
        ]
        if include_completed:
            return sorted(records, key=_persona_records._todo_readable_sort_key)
        now = _persona_records._now_value(None)
        return sorted(
            [record for record in records if not _persona_records._is_expired(record, now)],
            key=_persona_records._todo_readable_sort_key,
        )

    def _upsert_todo_sync(self, todo_id: str, data: dict[str, Any]) -> None:
        now = _persona_records._now_text()
        with closing(self._connect()) as conn:
            existing_row = conn.execute(
                """
                SELECT todo_json FROM persona_todos
                WHERE persona_id = ? AND todo_id = ?
                """,
                (self.persona_id, todo_id),
            ).fetchone()
            if existing_row is not None:
                existing = _persona_records._json_loads(existing_row["todo_json"], default={})
                if isinstance(existing, dict):
                    data = {**existing, **data}
                    data["id"] = todo_id
            conn.execute(
                """
                INSERT INTO persona_todos (
                    persona_id, todo_id, todo_json, completed,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, todo_id) DO UPDATE SET
                    todo_json = excluded.todo_json,
                    completed = excluded.completed,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    self.persona_id,
                    todo_id,
                    _persona_records._json_dumps(data),
                    1 if _persona_records._record_completed(data) else 0,
                    _persona_records._optional_text(
                        data,
                        ("expires_at", "expire_at", "until", "end_at"),
                    ),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _mark_expired_todos_missed_sync(self, now: Any) -> int:
        updated = 0
        updated_at = _persona_records._now_text()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT todo_id, todo_json FROM persona_todos
                WHERE persona_id = ? AND completed = 0
                """,
                (self.persona_id,),
            ).fetchall()
            for row in rows:
                record = _persona_records._json_loads(row["todo_json"], default={})
                if not isinstance(record, dict) or not _persona_records._is_expired(record, now):
                    continue
                missed = {
                    **record,
                    "id": str(row["todo_id"]),
                    "status": "missed",
                    "completed": True,
                }
                cur = conn.execute(
                    """
                    UPDATE persona_todos
                    SET todo_json = ?, completed = 1, updated_at = ?
                    WHERE persona_id = ? AND todo_id = ? AND completed = 0
                    """,
                    (
                        _persona_records._json_dumps(missed),
                        updated_at,
                        self.persona_id,
                        str(row["todo_id"]),
                    ),
                )
                updated += int(cur.rowcount)
            if updated:
                conn.commit()
            return updated

    def _upsert_cue_sync(self, cue_id: str, data: dict[str, Any]) -> None:
        now = _persona_records._now_text()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO persona_cues (
                    persona_id, cue_id, cue_json, expires_at,
                    active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, cue_id) DO UPDATE SET
                    cue_json = excluded.cue_json,
                    expires_at = excluded.expires_at,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    self.persona_id,
                    cue_id,
                    _persona_records._json_dumps(data),
                    _persona_records._optional_text(
                        data,
                        ("expires_at", "expire_at", "until", "end_at"),
                    ),
                    1 if _persona_records._record_active(data) else 0,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _get_profile_sync(self, user_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT profile_json FROM persona_user_profiles
                WHERE persona_id = ? AND user_id = ?
                """,
                (self.persona_id, user_id),
            ).fetchone()
        if row is None:
            return None
        data = _persona_records._json_loads(row["profile_json"], default={})
        return data if isinstance(data, dict) else {}

    def _upsert_profile_sync(self, user_id: str, data: dict[str, Any]) -> None:
        now = _persona_records._now_text()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO persona_user_profiles (
                    persona_id, user_id, profile_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, user_id) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                (
                    self.persona_id,
                    user_id,
                    _persona_records._json_dumps(data),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _all_profiles_sync(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT profile_json FROM persona_user_profiles
                WHERE persona_id = ?
                ORDER BY user_id ASC
                """,
                (self.persona_id,),
            ).fetchall()
        return [
            record for row in rows
            if isinstance(
                record := _persona_records._json_loads(row["profile_json"], default={}),
                dict,
            )
        ]

    def _insert_numbered_json_row_sync(
        self,
        table: str,
        json_column: str,
        data: dict[str, Any],
    ) -> int:
        _persona_schema._validate_table_name(table)
        _persona_schema._validate_table_name(json_column)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row_id = self._next_id(conn, table)
                conn.execute(
                    f"""
                    INSERT INTO {table} (persona_id, id, {json_column}, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        self.persona_id,
                        row_id,
                        _persona_records._json_dumps(data),
                        _persona_records._now_text(),
                    ),
                )
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            return row_id

    def _recent_json_rows_sync(
        self,
        table: str,
        json_column: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        _persona_schema._validate_table_name(table)
        _persona_schema._validate_table_name(json_column)
        limit = _persona_records._clamp_int(limit, default=20, minimum=1, maximum=500)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT {json_column}
                FROM (
                    SELECT id, {json_column} FROM {table}
                    WHERE persona_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (self.persona_id, limit),
            ).fetchall()
        return [
            record for row in rows
            if isinstance(record := _persona_records._json_loads(row[json_column], default={}), dict)
        ]

    def _recent_json_rows_desc_sync(
        self,
        table: str,
        json_column: str,
        limit: int,
        order_column: str = "id",
        tie_breaker_column: str = "id",
    ) -> list[dict[str, Any]]:
        _persona_schema._validate_table_name(table)
        _persona_schema._validate_table_name(json_column)
        _persona_schema._validate_table_name(order_column)
        _persona_schema._validate_table_name(tie_breaker_column)
        limit = _persona_records._clamp_int(limit, default=20, minimum=1, maximum=500)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT {json_column}
                FROM {table}
                WHERE persona_id = ?
                ORDER BY {order_column} DESC, {tie_breaker_column} DESC
                LIMIT ?
                """,
                (self.persona_id, limit),
            ).fetchall()
        return [
            record for row in rows
            if isinstance(record := _persona_records._json_loads(row[json_column], default={}), dict)
        ]

    def _upsert_sleep_record_sync(self, record_id: str, data: dict[str, Any]) -> None:
        now = _persona_records._now_text()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO persona_sleep_records (
                    persona_id, record_id, record_json, started_at,
                    ended_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_id, record_id) DO UPDATE SET
                    record_json = excluded.record_json,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    updated_at = excluded.updated_at
                """,
                (
                    self.persona_id,
                    record_id,
                    _persona_records._json_dumps(data),
                    _persona_records._optional_text(data, ("started_at", "start_at", "start")),
                    _persona_records._optional_text(data, ("ended_at", "end_at", "end")),
                    now,
                    now,
                ),
            )
            conn.commit()

    def _update_sleep_record_sync(self, record_id: str, updates: dict[str, Any]) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT record_json FROM persona_sleep_records
                WHERE persona_id = ? AND record_id = ?
                """,
                (self.persona_id, record_id),
            ).fetchone()
            if row is None:
                return False
            data = _persona_records._json_loads(row["record_json"], default={})
            if not isinstance(data, dict):
                data = {}
            data.update(updates)
            data.setdefault("id", record_id)
            data.setdefault("record_id", record_id)
            conn.execute(
                """
                UPDATE persona_sleep_records
                SET record_json = ?,
                    started_at = ?,
                    ended_at = ?,
                    updated_at = ?
                WHERE persona_id = ? AND record_id = ?
                """,
                (
                    _persona_records._json_dumps(data),
                    _persona_records._optional_text(data, ("started_at", "start_at", "start")),
                    _persona_records._optional_text(data, ("ended_at", "end_at", "end")),
                    _persona_records._now_text(),
                    self.persona_id,
                    record_id,
                ),
            )
            conn.commit()
            return True

    def _add_eat_record_sync(self, data: dict[str, Any]) -> int:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row_id = self._next_id(conn, "persona_eat_records")
                conn.execute(
                    """
                    INSERT INTO persona_eat_records (
                        persona_id, id, record_id, record_json, ended_at, status, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.persona_id,
                        row_id,
                        _persona_records._optional_text(data, ("record_id", "eat_id", "id")),
                        _persona_records._json_dumps(data),
                        _persona_records._optional_text(data, ("ended_at", "end_at", "end")),
                        _persona_records._optional_text(data, ("status",)),
                        _persona_records._now_text(),
                    ),
                )
            except Exception:
                conn.rollback()
                raise
            conn.commit()
            return row_id

    def _update_eat_record_sync(self, record_id: str, updates: dict[str, Any]) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT id, record_json FROM persona_eat_records
                WHERE persona_id = ? AND record_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (self.persona_id, record_id),
            ).fetchone()
            if row is None:
                return False
            data = _persona_records._json_loads(row["record_json"], default={})
            if not isinstance(data, dict):
                data = {}
            data.update(updates)
            data.setdefault("id", record_id)
            data.setdefault("record_id", record_id)
            conn.execute(
                """
                UPDATE persona_eat_records
                SET record_json = ?,
                    ended_at = ?,
                    status = ?
                WHERE persona_id = ? AND id = ?
                """,
                (
                    _persona_records._json_dumps(data),
                    _persona_records._optional_text(data, ("ended_at", "end_at", "end")),
                    _persona_records._optional_text(data, ("status",)),
                    self.persona_id,
                    row["id"],
                ),
            )
            conn.commit()
            return True

    def _delete_by_ids_sync(self, table: str, id_column: str, ids: list[str]) -> int:
        _persona_schema._validate_table_name(table)
        _persona_schema._validate_table_name(id_column)
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"""
                DELETE FROM {table}
                WHERE persona_id = ? AND {id_column} IN ({placeholders})
                """,
                [self.persona_id, *ids],
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
        _persona_schema._validate_table_name(table)
        _persona_schema._validate_table_name(id_column)
        _persona_schema._validate_table_name(json_column)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT {id_column}, {json_column}
                FROM {table}
                WHERE persona_id = ?
                """,
                (self.persona_id,),
            ).fetchall()
            expired_ids: list[str] = []
            for row in rows:
                record = _persona_records._json_loads(row[json_column], default={})
                if isinstance(record, dict) and _persona_records._is_expired(record, now):
                    expired_ids.append(str(row[id_column]))
            if not expired_ids:
                return 0
            placeholders = ",".join("?" for _ in expired_ids)
            cur = conn.execute(
                f"""
                DELETE FROM {table}
                WHERE persona_id = ? AND {id_column} IN ({placeholders})
                """,
                [self.persona_id, *expired_ids],
            )
            conn.commit()
            return int(cur.rowcount)

    def _read_important_sync(self, default: Any) -> Any:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT memories_json FROM persona_important_state_legacy
                WHERE persona_id = ? AND id = 1
                """,
                (self.persona_id,),
            ).fetchone()
        if row is None:
            return default if default is not None else []
        return _persona_records._json_loads(
            row["memories_json"],
            default=default if default is not None else [],
        )

    def _write_important_sync(self, data: Any) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO persona_important_state_legacy (
                    persona_id, id, memories_json, updated_at
                )
                VALUES (?, 1, ?, ?)
                ON CONFLICT(persona_id, id) DO UPDATE SET
                    memories_json = excluded.memories_json,
                    updated_at = excluded.updated_at
                """,
                (
                    self.persona_id,
                    _persona_records._json_dumps(data),
                    _persona_records._now_text(),
                ),
            )
            conn.commit()

    def _important_count_sync(self) -> int:
        data = self._read_important_sync(default=[])
        return len(data) if isinstance(data, list) else 0

    def _next_id(self, conn: sqlite3.Connection, table: str) -> int:
        _persona_schema._validate_table_name(table)
        return int(
            conn.execute(
                f"""
                SELECT COALESCE(MAX(id), 0) + 1
                FROM {table}
                WHERE persona_id = ?
                """,
                (self.persona_id,),
            ).fetchone()[0]
        )


class DianaRollingSummaryStore:
    """使用 diana.db 的 rolling_summary 表实现 RollingSummaryStore 等价接口。"""

    def __init__(self, db: DianaDB | str | Path, persona_id: str) -> None:
        self._db = db if isinstance(db, DianaDB) else DianaDB(db)
        self.persona_id = str(persona_id).strip()
        if not self.persona_id:
            raise ValueError("persona_id must not be empty")
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = _default_rolling_summary_data()

    @property
    def db(self) -> DianaDB:
        return self._db

    async def load(self) -> dict[str, Any]:
        async with self._lock:
            self._data = await asyncio.to_thread(self._load_sync)
            return dict(self._data)

    def text(self) -> str:
        return str(self._data.get("summary_text") or "")

    def active_start_index(self) -> int:
        """当前活跃窗口在完整 history 中的起点。"""

        archived_until = self._data.get("archived_until")
        value: Any = None
        if isinstance(archived_until, dict):
            value = archived_until.get("active_start_index")
        if value is None:
            value = self._data.get("active_start_index")
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    async def update(
        self,
        summary_text: str,
        *,
        archived_until: Any = None,
        updated_at: str = "",
        active_start_index: int | None = None,
    ) -> None:
        archived_payload = archived_until
        if active_start_index is not None:
            active_start_index = max(0, int(active_start_index))
            if isinstance(archived_payload, dict):
                archived_payload = dict(archived_payload)
            elif archived_payload is None:
                archived_payload = {}
            else:
                archived_payload = {"legacy_archived_until": archived_payload}
            archived_payload["active_start_index"] = active_start_index
        new_data = {
            "summary_text": summary_text.strip(),
            "archived_until": archived_payload,
            "updated_at": updated_at,
        }
        active_start_column = self._active_start_from_archived_until(archived_payload)
        async with self._lock:
            await asyncio.to_thread(self._update_sync, new_data, active_start_column)
            self._data = new_data

    def _load_sync(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT summary_text, archived_until_json, active_start_index,
                       summary_json, updated_at
                FROM rolling_summary
                WHERE persona_id = ?
                """,
                (self.persona_id,),
            ).fetchone()
        if row is None:
            return _default_rolling_summary_data()

        summary_data = self._fallback_summary_data(row)
        summary_json = self._load_summary_json(row)
        if summary_json is not None:
            summary_data = self._merge_summary_data(summary_data, summary_json)
        return self._normalize_loaded_data(summary_data)

    def _update_sync(
        self,
        new_data: dict[str, Any],
        active_start_index: int | None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO rolling_summary (
                        persona_id, summary_text, archived_until_json,
                        active_start_index, summary_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(persona_id) DO UPDATE SET
                        summary_text = excluded.summary_text,
                        archived_until_json = excluded.archived_until_json,
                        active_start_index = excluded.active_start_index,
                        summary_json = excluded.summary_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self.persona_id,
                        new_data["summary_text"],
                        _json_data(new_data["archived_until"]),
                        active_start_index,
                        _json_data(new_data),
                        new_data["updated_at"],
                    ),
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _connect(self):
        db = DianaDB(self._db.path, busy_timeout_ms=self._db.busy_timeout_ms)
        db.load()
        return db.connect()

    def _load_summary_json(self, row: sqlite3.Row) -> dict[str, Any] | None:
        try:
            data = orjson.loads(row["summary_json"])
        except orjson.JSONDecodeError:
            logger.warning(
                "回退读取损坏的 diana rolling summary summary_json persona_id=%s",
                self.persona_id,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "回退读取非对象 diana rolling summary summary_json persona_id=%s",
                self.persona_id,
            )
            return None
        return data

    def _fallback_summary_data(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            archived_until = orjson.loads(row["archived_until_json"])
        except orjson.JSONDecodeError:
            logger.warning(
                "回退读取损坏的 diana rolling summary archived_until_json persona_id=%s",
                self.persona_id,
            )
            archived_until = None
        return {
            "summary_text": row["summary_text"],
            "archived_until": archived_until,
            "updated_at": row["updated_at"],
            "active_start_index": row["active_start_index"],
        }

    @staticmethod
    def _merge_summary_data(
        fallback_data: dict[str, Any],
        summary_json: dict[str, Any],
    ) -> dict[str, Any]:
        data = dict(fallback_data)
        for key in ("summary_text", "archived_until", "updated_at"):
            if key in summary_json:
                data[key] = summary_json[key]
        if "active_start_index" in summary_json:
            data["active_start_index"] = summary_json["active_start_index"]
        return data

    @classmethod
    def _normalize_loaded_data(cls, data: dict[str, Any]) -> dict[str, Any]:
        archived_until = data.get("archived_until")
        legacy_active_start = cls._coerce_active_start(data.get("active_start_index"))
        if legacy_active_start is not None:
            if isinstance(archived_until, dict):
                archived_until = dict(archived_until)
            elif archived_until is None:
                archived_until = {}
            else:
                archived_until = {"legacy_archived_until": archived_until}
            archived_until.setdefault("active_start_index", legacy_active_start)
        return {
            "summary_text": str(data.get("summary_text") or ""),
            "archived_until": archived_until,
            "updated_at": str(data.get("updated_at") or ""),
        }

    @classmethod
    def _active_start_from_archived_until(cls, archived_until: Any) -> int | None:
        if not isinstance(archived_until, dict):
            return None
        return cls._coerce_active_start(archived_until.get("active_start_index"))

    @staticmethod
    def _coerce_active_start(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None


class DianaUsageStatsStore:
    """使用 diana.db 的 usage_records 表实现 UsageStatsStore 等价接口。"""

    def __init__(
        self,
        db: DianaDB | str | Path,
        persona_id: str | None = None,
    ) -> None:
        self._db = db if isinstance(db, DianaDB) else DianaDB(db)
        persona_text = "" if persona_id is None else str(persona_id).strip()
        self.persona_id = persona_text or None
        self._lock = asyncio.Lock()
        self._records: list[dict[str, Any]] | None = None

    @property
    def db(self) -> DianaDB:
        return self._db

    async def load(self) -> None:
        async with self._lock:
            self._records = await asyncio.to_thread(self._load_sync)

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
                self._records = await asyncio.to_thread(self._load_sync)
            await asyncio.to_thread(self._insert_record_sync, record)
            self._records.append(record)

    def summarize(self, range_name: UsageRange = "today") -> UsageSummary:
        records = self._records or []
        cutoff = cutoff_timestamp(range_name)
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

    def _load_sync(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            if self.persona_id is None:
                rows = conn.execute(
                    """
                    SELECT id, persona_id, record_json
                    FROM usage_records
                    ORDER BY id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, persona_id, record_json
                    FROM usage_records
                    WHERE persona_id = ?
                    ORDER BY id ASC
                    """,
                    (self.persona_id,),
                ).fetchall()

        records: list[dict[str, Any]] = []
        for row in rows:
            try:
                record = orjson.loads(row["record_json"])
            except orjson.JSONDecodeError:
                logger.warning(
                    "跳过损坏的 diana usage record_json persona_id=%s rowid=%s",
                    row["persona_id"],
                    row["id"],
                )
                continue
            if not isinstance(record, dict):
                logger.warning(
                    "跳过非对象 diana usage record_json persona_id=%s rowid=%s",
                    row["persona_id"],
                    row["id"],
                )
                continue
            records.append(record)
        return records

    def _insert_record_sync(self, record: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO usage_records (
                        persona_id, ts, provider, model, agent, operation,
                        prompt_tokens, completion_tokens, reasoning_tokens,
                        cached_tokens, cache_creation_tokens, total_tokens,
                        record_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.persona_id,
                        float(record.get("ts") or 0),
                        _record_text(record.get("provider")),
                        _record_text(record.get("model")),
                        _record_text(record.get("agent")),
                        _record_text(record.get("operation")),
                        int(record.get("prompt_tokens") or 0),
                        int(record.get("completion_tokens") or 0),
                        int(record.get("reasoning_tokens") or 0),
                        int(record.get("cached_tokens") or 0),
                        int(record.get("cache_creation_tokens") or 0),
                        int(record.get("total_tokens") or 0),
                        _record_json(record),
                    ),
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _connect(self):
        db = DianaDB(self._db.path, busy_timeout_ms=self._db.busy_timeout_ms)
        db.load()
        return db.connect()


class DianaEventStore:
    """使用 diana.db 的 event_log 表实现 EventJournal 可用的事件仓储。"""

    _PROJECTION_STATE_NAME = "last_projected_event_id"
    _schema_lock = threading.Lock()
    _schema_ready_paths: set[Path] = set()

    def __init__(self, db: DianaDB | str | Path, persona_id: str) -> None:
        self._db = db if isinstance(db, DianaDB) else DianaDB(db)
        self._schema_path = self._db.path.expanduser().resolve()
        self.persona_id = str(persona_id).strip()
        if not self.persona_id:
            raise ValueError("persona_id must not be empty")
        self._lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()
        self._loaded = False
        self._closed = False
        self._last_appended_event_id = 0
        self._last_projected_event_id = 0

    @property
    def db(self) -> DianaDB:
        return self._db

    async def start_projection(self) -> None:
        """兼容 EventStore 入口：diana.db 写入即投影，无后台 worker。"""

        await self._ensure_loaded()

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

        pending = [_normalize_event(event) for event in events]
        await self._ensure_loaded()
        async with self._lock:
            if self._closed:
                raise RuntimeError("DianaEventStore is closed")
            ids, max_event_id = await asyncio.to_thread(self._append_events_sync, pending)
            self._last_appended_event_id = max_event_id
            self._last_projected_event_id = max_event_id
            return ids

    async def wait_projected(
        self,
        event_id: int | None = None,
        *,
        timeout: float | None = None,
    ) -> bool:
        """等待当前 persona 至少出现指定 event_id。"""

        await self._ensure_loaded()
        if event_id is None:
            await self._refresh_event_ids()
            return True

        target_event_id = _positive_int_or_none(event_id)
        if target_event_id is None:
            return True

        if await self._refresh_event_ids() >= target_event_id:
            return True

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, float(timeout))
        poll_interval = self._projection_poll_interval(timeout)
        while True:
            if deadline is None:
                await asyncio.sleep(poll_interval)
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(poll_interval, remaining))
            if await self._refresh_event_ids() >= target_event_id:
                return True

    async def stats(self) -> dict[str, Any]:
        await self._ensure_loaded()
        max_event_id = await asyncio.to_thread(self._max_event_id_sync)
        self._last_appended_event_id = max_event_id
        self._last_projected_event_id = max_event_id
        return {
            "last_appended_event_id": max_event_id,
            "last_projected_event_id": max_event_id,
            "projection_lag": 0,
            "pending_count": 0,
            "projection_error_count": 0,
            "last_projection_error": None,
            "last_projection_error_event_id": None,
            "projection_running": False,
            "closed": self._closed,
        }

    async def shutdown(self, timeout: float | None = 5.0) -> bool:
        del timeout
        await self._ensure_loaded()
        async with self._lock:
            self._closed = True
        return True

    async def close(self, timeout: float | None = 5.0) -> bool:
        return await self.shutdown(timeout=timeout)

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        event_id = _positive_int_or_none(event_id)
        if event_id is None:
            return None
        await self._ensure_loaded()
        return await asyncio.to_thread(self._get_event_sync, event_id)

    async def get_events(self, event_ids: list[int]) -> list[dict[str, Any] | None]:
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
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]:
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
            await asyncio.to_thread(self._ensure_schema_sync)
            max_event_id = await asyncio.to_thread(self._max_event_id_sync)
            self._last_appended_event_id = max_event_id
            self._last_projected_event_id = max_event_id
            self._loaded = True

    async def _refresh_event_ids(self) -> int:
        max_event_id = await asyncio.to_thread(self._max_event_id_sync)
        self._last_appended_event_id = max_event_id
        self._last_projected_event_id = max_event_id
        return max_event_id

    @staticmethod
    def _projection_poll_interval(timeout: float | None) -> float:
        if timeout is None:
            return 0.01
        timeout_seconds = max(0.0, float(timeout))
        if timeout_seconds <= 0:
            return 0.001
        return min(0.01, max(0.001, timeout_seconds / 10))

    def _append_events_sync(self, events: list[_PendingEvent]) -> tuple[list[int], int]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                next_event_id = self._max_event_id(conn) + 1
                ids: list[int] = []
                batch_idempotency: dict[str, int] = {}

                for event in events:
                    existing_id = self._existing_event_id(
                        conn,
                        event,
                        batch_idempotency,
                    )
                    if existing_id is not None:
                        ids.append(existing_id)
                        continue

                    while True:
                        try:
                            self._insert_event(conn, next_event_id, event)
                        except sqlite3.IntegrityError:
                            existing_id = self._idempotency_event_id(
                                conn,
                                event.idempotency_key,
                            )
                            if existing_id is not None:
                                ids.append(existing_id)
                                batch_idempotency[event.idempotency_key or ""] = existing_id
                                break
                            latest_event_id = self._max_event_id(conn)
                            if latest_event_id >= next_event_id:
                                next_event_id = latest_event_id + 1
                                continue
                            raise
                        ids.append(next_event_id)
                        if event.idempotency_key is not None:
                            batch_idempotency[event.idempotency_key] = next_event_id
                        next_event_id += 1
                        break

                max_event_id = self._max_event_id(conn)
                self._set_projection_progress(conn, max_event_id)
            except Exception:
                conn.rollback()
                raise
            conn.commit()
        return ids, max_event_id

    def _existing_event_id(
        self,
        conn: sqlite3.Connection,
        event: _PendingEvent,
        batch_idempotency: dict[str, int],
    ) -> int | None:
        key = event.idempotency_key
        if key is None:
            return None
        existing_id = batch_idempotency.get(key)
        if existing_id is not None:
            return existing_id
        existing_id = self._idempotency_event_id(conn, key)
        if existing_id is not None:
            batch_idempotency[key] = existing_id
        return existing_id

    def _idempotency_event_id(
        self,
        conn: sqlite3.Connection,
        idempotency_key: str | None,
    ) -> int | None:
        if idempotency_key is None:
            return None
        row = conn.execute(
            """
            SELECT event_id
            FROM event_log
            WHERE persona_id = ? AND idempotency_key = ?
            """,
            (self.persona_id, idempotency_key),
        ).fetchone()
        return None if row is None else int(row["event_id"])

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        event_id: int,
        event: _PendingEvent,
    ) -> None:
        conn.execute(
            """
            INSERT INTO event_log (
                persona_id, event_id, event_type, event_uuid, conversation_id,
                session_id, turn_id, source, external_id, tool_call_id,
                parent_event_id, idempotency_key, timestamp_unix,
                created_at_unix, payload_json, payload_hash, schema_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.persona_id,
                event_id,
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

    def _set_projection_progress(
        self,
        conn: sqlite3.Connection,
        event_id: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO event_projection_state(persona_id, name, value)
            VALUES (?, ?, ?)
            ON CONFLICT(persona_id, name) DO UPDATE SET value = excluded.value
            """,
            (self.persona_id, self._PROJECTION_STATE_NAME, str(event_id)),
        )

    def _get_event_sync(self, event_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM event_log
                WHERE persona_id = ? AND event_id = ?
                """,
                (self.persona_id, event_id),
            ).fetchone()
        return _row_to_event(row) if row is not None else None

    def _get_events_sync(self, event_ids: list[int]) -> dict[int, dict[str, Any]]:
        placeholders = ", ".join("?" for _ in event_ids)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM event_log
                WHERE persona_id = ? AND event_id IN ({placeholders})
                """,
                [self.persona_id, *event_ids],
            ).fetchall()
        return {int(row["event_id"]): _row_to_event(row) for row in rows}

    def _iter_events_sync(
        self,
        limit: int,
        after_event_id: int | None,
        before_event_id: int | None,
        order: str,
    ) -> list[dict[str, Any]]:
        cursor_where, cursor_params = _cursor_where(after_event_id, before_event_id)
        where = ["persona_id = ?", *cursor_where]
        params: list[Any] = [self.persona_id, *cursor_params, limit]
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM event_log
                WHERE {" AND ".join(where)}
                ORDER BY event_id {order.upper()}
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def _events_for_conversation_sync(
        self,
        conversation_id: str,
        limit: int,
        before_event_id: int | None,
    ) -> list[dict[str, Any]]:
        where = ["persona_id = ?", "conversation_id = ?"]
        params: list[Any] = [self.persona_id, conversation_id]
        if before_event_id is not None:
            where.append("event_id < ?")
            params.append(before_event_id)
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM event_log
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
        cursor_where, cursor_params = _cursor_where(after_event_id, before_event_id)
        where = ["persona_id = ?", "event_type = ?", *cursor_where]
        params: list[Any] = [self.persona_id, event_type, *cursor_params, limit]
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM event_log
                WHERE {" AND ".join(where)}
                ORDER BY event_id {order.upper()}
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def _max_event_id_sync(self) -> int:
        with closing(self._connect()) as conn:
            return self._max_event_id(conn)

    def _max_event_id(self, conn: sqlite3.Connection) -> int:
        return int(
            conn.execute(
                """
                SELECT COALESCE(MAX(event_id), 0)
                FROM event_log
                WHERE persona_id = ?
                """,
                (self.persona_id,),
            ).fetchone()[0]
        )

    def _ensure_schema_sync(self) -> None:
        if self._schema_path in self._schema_ready_paths and self._schema_path.exists():
            return
        with self._schema_lock:
            if self._schema_path in self._schema_ready_paths and self._schema_path.exists():
                return
            db = DianaDB(self._db.path, busy_timeout_ms=self._db.busy_timeout_ms)
            try:
                db.load()
            finally:
                db.close()
            self._schema_ready_paths.add(self._schema_path)

    def _connect(self) -> sqlite3.Connection:
        self._db.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db.path, timeout=self._db.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self._db.busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


_LEGACY_RAW_IMPORTANT_MEMORY_ID = "__diana_important_raw__"
_LEGACY_RAW_IMPORTANT_SCOPE = "__legacy_raw__"


def _is_legacy_raw_important_row(row: sqlite3.Row) -> bool:
    return (
        row["memory_id"] == _LEGACY_RAW_IMPORTANT_MEMORY_ID
        and row["scope"] == _LEGACY_RAW_IMPORTANT_SCOPE
    )


def _important_memory_id(item: Any) -> str:
    if isinstance(item, dict):
        item_id = _optional_text(item.get("id"))
        if item_id:
            return item_id
    digest = hashlib.sha256(_canonical_json_data(item).encode("utf-8")).hexdigest()[:32]
    return f"fallback:{digest}"


def _unique_memory_id(memory_id: str, used_memory_ids: set[str]) -> str:
    candidate = memory_id
    suffix = 2
    while candidate in used_memory_ids:
        candidate = f"{memory_id}#{suffix}"
        suffix += 1
    used_memory_ids.add(candidate)
    return candidate


def _important_item_text(item: Any, key: str) -> str | None:
    if not isinstance(item, dict):
        return None
    return _optional_text(item.get(key))


def _important_pinned_value(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    value = item.get("pinned", False)
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
    return 1 if bool(value) else 0


def _record_json(record: dict) -> str:
    return orjson.dumps(record).decode("utf-8")


def _record_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _json_data(data: Any) -> str:
    return orjson.dumps(data).decode("utf-8")


def _canonical_json_data(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _history_record_conversation_id(record: dict) -> str | None:
    return _optional_text(record.get("conversation_id")) or _infer_conversation_id(record)


def _infer_conversation_id(record: dict) -> str | None:
    meta = record.get("metadata")
    if not isinstance(meta, dict):
        return None

    messages = meta.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            scope = last.get("scope")
            target_id = last.get("target_id")
            group_id = last.get("group_id")
            user_id = last.get("user_id")
            if scope == "group" and (group_id or target_id):
                return f"group:{group_id or target_id}"
            if scope == "private" and (target_id or user_id):
                return f"private:{target_id or user_id}"
            if group_id:
                return f"group:{group_id}"
            if user_id:
                return f"private:{user_id}"

    scope = meta.get("scope")
    target_id = meta.get("target_id")
    group_id = meta.get("group_id")
    user_id = meta.get("user_id")
    if scope == "group" and (group_id or target_id):
        return f"group:{group_id or target_id}"
    if scope == "private" and (target_id or user_id):
        return f"private:{target_id or user_id}"
    if group_id:
        return f"group:{group_id}"
    if user_id:
        return f"private:{user_id}"
    return None


def _content_fingerprint(content: object) -> tuple[int, str]:
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        text = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _default_rolling_summary_data() -> dict[str, Any]:
    return {
        "summary_text": "",
        "archived_until": None,
        "updated_at": "",
    }


__all__ = [
    "DianaArchiveStore",
    "DianaEventStore",
    "DianaHistoryStore",
    "DianaImportantStore",
    "DianaPersonaDB",
    "DianaRollingSummaryStore",
    "DianaUsageStatsStore",
]
