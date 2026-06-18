"""diana.db 轻量仓储适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

from .diana_db import DianaDB

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


__all__ = ["DianaHistoryStore", "DianaImportantStore"]
