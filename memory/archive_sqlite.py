"""sqlite3 归档存储。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .archive_sqlite_filters import (
    _filter_order_sql,
    _filter_row_sort_key,
    _filter_sql_plan,
    _fuzzy_match,
    _join_filter_clauses,
    _json_extract_sql,
    _json_root_type_sql,
    _json_truthy_sql,
    _json_type_sql,
    _legacy_search_text_sql,
    _matches_keywords,
    _matches_time_ranges,
    _matches_values,
    _placeholders,
    _real_chat_sql_conditions,
    _row_matches_filter,
    _sql_time_ranges,
    _time_ranges_sql,
    _unique_rows_by_rowid,
)
from .archive_sqlite_models import _FilterSqlPlan, _NormalizedRecord, _SqlTimeRange
from .archive_sqlite_records import (
    _MEDIA_PLACEHOLDER_PATTERN,
    _MEDIA_RUNTIME_ATTR_PATTERN,
    _MEDIA_URL_ATTR_PATTERN,
    _REAL_CHAT_DIRECTIONS,
    _REAL_CHAT_MESSAGE_KINDS,
    _REAL_CHAT_ROLES,
    _RUNTIME_CONTEXT_MARKERS,
    _RUNTIME_METADATA_KINDS,
    _SECRET_QUERY_PATTERN,
    _URL_PATTERN,
    _WINDOWS_PATH_PATTERN,
    _assistant_has_outbound_proof,
    _base36,
    _clamp_int,
    _clean_display_content,
    _clean_id,
    _clean_optional,
    _clean_search_content,
    _conversation_is_runtime,
    _conversation_parts,
    _direction_for,
    _extract_attr,
    _extract_conversation_id,
    _extract_media,
    _extract_original_msg_id,
    _extract_timestamp,
    _first_meta_message,
    _json_dumps,
    _json_loads,
    _legacy_search_text,
    _message_kind_for,
    _metadata_is_runtime,
    _normalize_archive_path,
    _normalize_media_placeholders,
    _normalize_record,
    _normalized_is_real_chat,
    _now_text,
    _outbound_records_from_tool_result,
    _parse_datetime,
    _query_to_dict,
    _record_content_text,
    _row_is_real_chat,
    _row_to_light_result,
    _row_to_record,
    _sender_parts,
    _string_list,
    _text_is_runtime,
    _time_range_list,
    _timestamp_parts,
    is_real_chat_record,
    real_chat_archive_records,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SqliteArchiveStore",
    "_FilterSqlPlan",
    "_NormalizedRecord",
    "_SqlTimeRange",
    "_assistant_has_outbound_proof",
    "_base36",
    "_MEDIA_PLACEHOLDER_PATTERN",
    "_MEDIA_RUNTIME_ATTR_PATTERN",
    "_MEDIA_URL_ATTR_PATTERN",
    "_REAL_CHAT_DIRECTIONS",
    "_REAL_CHAT_MESSAGE_KINDS",
    "_REAL_CHAT_ROLES",
    "_RUNTIME_CONTEXT_MARKERS",
    "_RUNTIME_METADATA_KINDS",
    "_SECRET_QUERY_PATTERN",
    "_URL_PATTERN",
    "_WINDOWS_PATH_PATTERN",
    "_clean_display_content",
    "_clean_id",
    "_clamp_int",
    "_clean_optional",
    "_clean_search_content",
    "_conversation_is_runtime",
    "_conversation_parts",
    "_direction_for",
    "_extract_attr",
    "_extract_conversation_id",
    "_extract_media",
    "_extract_original_msg_id",
    "_extract_timestamp",
    "_filter_order_sql",
    "_filter_row_sort_key",
    "_filter_sql_plan",
    "_first_meta_message",
    "_fuzzy_match",
    "_join_filter_clauses",
    "_json_dumps",
    "_json_extract_sql",
    "_json_loads",
    "_json_root_type_sql",
    "_json_truthy_sql",
    "_json_type_sql",
    "_legacy_search_text",
    "_legacy_search_text_sql",
    "_matches_keywords",
    "_matches_time_ranges",
    "_matches_values",
    "_message_kind_for",
    "_metadata_is_runtime",
    "_normalize_archive_path",
    "_normalize_media_placeholders",
    "_normalize_record",
    "_normalized_is_real_chat",
    "_now_text",
    "_outbound_records_from_tool_result",
    "_parse_datetime",
    "_placeholders",
    "_query_to_dict",
    "_real_chat_sql_conditions",
    "_record_content_text",
    "_row_is_real_chat",
    "_row_matches_filter",
    "_row_to_light_result",
    "_row_to_record",
    "_sender_parts",
    "_sql_time_ranges",
    "_string_list",
    "_text_is_runtime",
    "_time_range_list",
    "_time_ranges_sql",
    "_timestamp_parts",
    "_unique_rows_by_rowid",
    "is_real_chat_record",
    "real_chat_archive_records",
]


class SqliteArchiveStore:
    """永久归档 sqlite 后端。"""

    def __init__(self, path: Path) -> None:
        self.path = _normalize_archive_path(path)
        self._lock = asyncio.Lock()

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
                "Archive filter_records 指标 limit=%s offset=%s returned=%s total=%s "
                "elapsed_ms=%.3f",
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
        """返回只适合 RAG bootstrap 的真实聊天记录。"""
        async with self._lock:
            return await asyncio.to_thread(self._rag_records_sync)

    async def media_records(self, archive_id: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._media_records_sync, archive_id)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_messages (
                rowid INTEGER PRIMARY KEY,
                archive_id TEXT UNIQUE NOT NULL,
                timestamp TEXT,
                timestamp_unix INTEGER,
                date_key TEXT,
                month_key TEXT,
                conversation_id TEXT,
                conversation_type TEXT,
                target_id TEXT,
                sender_id TEXT,
                sender_name TEXT,
                sender_role TEXT,
                direction TEXT,
                message_kind TEXT,
                content TEXT,
                content_search TEXT,
                original_msg_id TEXT,
                reply_to_msg_id TEXT,
                metadata_json TEXT,
                record_json TEXT,
                created_at TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(archive_messages)").fetchall()
        }
        if "record_json" not in columns:
            conn.execute("ALTER TABLE archive_messages ADD COLUMN record_json TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_message_media (
                id INTEGER PRIMARY KEY,
                archive_id TEXT NOT NULL,
                media_type TEXT,
                workspace_path TEXT,
                original_name TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_time "
            "ON archive_messages(timestamp_unix)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_conversation_time "
            "ON archive_messages(conversation_id, timestamp_unix)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_sender_time "
            "ON archive_messages(sender_id, timestamp_unix)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_original_msg "
            "ON archive_messages(original_msg_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_date "
            "ON archive_messages(date_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_record_json "
            "ON archive_messages(record_json)"
        )
        conn.commit()

    def _append_many_sync(self, records: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now_text()
            existing_record_json = self._existing_record_json(conn, records)
            seen_record_json: set[str] = set()
            pending: list[tuple[_NormalizedRecord, str, str]] = []
            for record in records:
                normalized = _normalize_record(record)
                record_json = _json_dumps(normalized.record)
                if record_json in existing_record_json or record_json in seen_record_json:
                    continue
                seen_record_json.add(record_json)
                pending.append((normalized, record_json, _json_dumps(normalized.metadata)))
            if not pending:
                conn.commit()
                return
            next_rowid = int(
                conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) + 1 FROM archive_messages"
                ).fetchone()[0]
            )
            message_rows: list[tuple[Any, ...]] = []
            media_rows: list[tuple[Any, ...]] = []
            for offset, (normalized, record_json, metadata_json) in enumerate(pending):
                rowid = next_rowid + offset
                archive_id = "a" + _base36(rowid)
                message_rows.append(
                    (
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
                media_rows.extend(
                    (
                        archive_id,
                        item["media_type"],
                        item.get("workspace_path"),
                        item.get("original_name"),
                        _json_dumps(item.get("metadata") or {}),
                    )
                    for item in _extract_media(normalized.record.get("content"))
                )
            conn.executemany(
                """
                INSERT INTO archive_messages (
                    rowid, archive_id, timestamp, timestamp_unix, date_key, month_key,
                    conversation_id, conversation_type, target_id, sender_id,
                    sender_name, sender_role, direction, message_kind, content,
                    content_search, original_msg_id, reply_to_msg_id, metadata_json,
                    record_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                message_rows,
            )
            if media_rows:
                conn.executemany(
                    """
                    INSERT INTO archive_message_media (
                        archive_id, media_type, workspace_path, original_name, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    media_rows,
                )
            conn.commit()

    @staticmethod
    def _existing_record_json(
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
                WHERE record_json IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            existing.update(str(row["record_json"]) for row in rows if row["record_json"])
        return existing

    def _records_sync(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM archive_messages ORDER BY rowid ASC"
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM archive_messages ORDER BY rowid ASC"
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
            WHERE {plan.where_sql}
            """
        with self._connect() as conn:
            if plan.has_python_residual_filter:
                rows = conn.execute(
                    f"SELECT * {base_sql} ORDER BY {order_sql}",
                    plan.params,
                ).fetchall()
                if plan.fallback_where_sql is not None:
                    fallback_base_sql = f"""
                        FROM archive_messages
                        WHERE {plan.fallback_where_sql}
                        """
                    fallback_rows = conn.execute(
                        f"SELECT * {fallback_base_sql} ORDER BY {order_sql}",
                        plan.fallback_params or [],
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
                        plan.params,
                    ).fetchone()[0]
                )
                selected = conn.execute(
                    f"SELECT * {base_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                    [*plan.params, limit, offset],
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
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM archive_messages WHERE archive_id IN ({placeholders})",
                archive_ids,
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
        with self._connect() as conn:
            target = conn.execute(
                "SELECT * FROM archive_messages WHERE archive_id = ?",
                (archive_id,),
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
                    WHERE conversation_id = ? AND rowid < ?
                    ORDER BY rowid DESC
                    """,
                    (conversation_id, target["rowid"]),
                ).fetchall()
                next_rows = conn.execute(
                    """
                    SELECT * FROM archive_messages
                    WHERE conversation_id = ? AND rowid > ?
                    ORDER BY rowid ASC
                    """,
                    (conversation_id, target["rowid"]),
                ).fetchall()
        prev_real = [row for row in prev_rows if _row_is_real_chat(row)][:before]
        next_real = [row for row in next_rows if _row_is_real_chat(row)][:after]
        rows = list(reversed(prev_real)) + [target] + next_real
        return [_row_to_record(row) for row in rows]

    def _rag_records_sync(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM archive_messages
                WHERE direction IN ('inbound', 'outbound')
                  AND message_kind IN ('text', 'image', 'file', 'audio', 'forward', 'mixed')
                ORDER BY rowid ASC
                """
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
        with self._connect() as conn:
            if archive_id:
                rows = conn.execute(
                    """
                    SELECT * FROM archive_message_media
                    WHERE archive_id = ?
                    ORDER BY id ASC
                    """,
                    (archive_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM archive_message_media ORDER BY id ASC"
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
