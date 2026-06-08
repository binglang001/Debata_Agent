"""sqlite3 归档存储。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class _NormalizedRecord:
    role: str
    timestamp: str | None
    timestamp_unix: int | None
    date_key: str | None
    month_key: str | None
    conversation_id: str | None
    conversation_type: str
    target_id: str | None
    sender_id: str | None
    sender_name: str | None
    sender_role: str
    direction: str
    message_kind: str
    content: str
    content_search: str
    original_msg_id: str | None
    reply_to_msg_id: str | None
    metadata: dict[str, Any]
    record: dict[str, Any]


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
        payload = [dict(record) for record in records if isinstance(record, dict)]
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
        async with self._lock:
            return await asyncio.to_thread(self._filter_records_sync, query_dict)

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
        conn.commit()

    def _append_many_sync(self, records: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            now = _now_text()
            next_rowid = int(
                conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) + 1 FROM archive_messages"
                ).fetchone()[0]
            )
            message_rows: list[tuple[Any, ...]] = []
            media_rows: list[tuple[Any, ...]] = []
            for offset, record in enumerate(records):
                rowid = next_rowid + offset
                archive_id = "a" + _base36(rowid)
                normalized = _normalize_record(record)
                record_json = _json_dumps(normalized.record)
                metadata_json = _json_dumps(normalized.metadata)
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

    def _records_sync(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM archive_messages ORDER BY rowid ASC"
            ).fetchall()
        return [_row_to_record(row) for row in rows]

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
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM archive_messages").fetchall()
        matched = [row for row in rows if _row_matches_filter(row, query)]
        matched.sort(
            key=lambda row: (
                row["timestamp_unix"] if row["timestamp_unix"] is not None else -1,
                row["rowid"],
            ),
            reverse=reverse,
        )
        total = len(matched)
        selected = matched[offset:offset + limit]
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
        by_id = {row["archive_id"]: _row_to_record(row) for row in rows}
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
            if target is None:
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
                    ORDER BY rowid DESC LIMIT ?
                    """,
                    (conversation_id, target["rowid"], before),
                ).fetchall()
                next_rows = conn.execute(
                    """
                    SELECT * FROM archive_messages
                    WHERE conversation_id = ? AND rowid > ?
                    ORDER BY rowid ASC LIMIT ?
                    """,
                    (conversation_id, target["rowid"], after),
                ).fetchall()
        rows = list(reversed(prev_rows)) + [target] + list(next_rows)
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


def _normalize_archive_path(path: Path) -> Path:
    if path.name == "archive.jsonl":
        return path.with_name("archive.sqlite3")
    if path.suffix.lower() == ".jsonl":
        return path.with_suffix(".sqlite3")
    return path


def _normalize_record(record: dict[str, Any]) -> _NormalizedRecord:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    metadata = dict(metadata or {})
    role = str(record.get("role") or "").strip() or "user"
    content = _clean_display_content(_record_content_text(record.get("content")))
    content_search = _clean_search_content(content)
    timestamp = _extract_timestamp(record, metadata)
    timestamp_unix, date_key, month_key = _timestamp_parts(timestamp)
    conversation_id = _extract_conversation_id(record, metadata)
    conversation_type, target_id = _conversation_parts(conversation_id, metadata)
    sender_id, sender_name = _sender_parts(record, metadata, role)
    direction = _direction_for(record, metadata, role)
    message_kind = _message_kind_for(content, record, metadata, role)
    if direction == "runtime":
        message_kind = "runtime"
    normalized_record = dict(record)
    normalized_record["content"] = content
    if conversation_id:
        normalized_record["conversation_id"] = conversation_id
    normalized_record["metadata"] = metadata
    return _NormalizedRecord(
        role=role,
        timestamp=timestamp,
        timestamp_unix=timestamp_unix,
        date_key=date_key,
        month_key=month_key,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        target_id=target_id,
        sender_id=sender_id,
        sender_name=sender_name,
        sender_role=role,
        direction=direction,
        message_kind=message_kind,
        content=content,
        content_search=content_search,
        original_msg_id=_extract_original_msg_id(record, metadata),
        reply_to_msg_id=_clean_optional(record.get("reply_to_msg_id"))
        or _clean_optional(metadata.get("reply_to_message_id")),
        metadata=metadata,
        record=normalized_record,
    )


def _row_to_record(row: sqlite3.Row) -> dict:
    record = _json_loads(row["record_json"], default={})
    if not isinstance(record, dict):
        record = {}
    metadata = _json_loads(row["metadata_json"], default={})
    if not isinstance(metadata, dict):
        metadata = {}
    record["role"] = record.get("role") or row["sender_role"] or "user"
    record["content"] = row["content"] or ""
    record["conversation_id"] = row["conversation_id"]
    record["metadata"] = metadata
    record["archive_id"] = row["archive_id"]
    return record


def _row_to_light_result(row: sqlite3.Row) -> dict[str, Any]:
    sender = str(row["sender_name"] or row["sender_id"] or row["sender_role"] or "-")
    if row["sender_name"] and row["sender_id"]:
        sender = f"{row['sender_name']}({row['sender_id']})"
    return {
        "id": row["archive_id"],
        "time": row["timestamp"],
        "conversation_id": row["conversation_id"],
        "sender": sender,
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "direction": row["direction"],
        "kind": row["message_kind"],
        "content": row["content"],
        "metadata": {
            "date": row["date_key"],
            "month": row["month_key"],
            "conversation_type": row["conversation_type"],
            "target_id": row["target_id"],
            "original_msg_id": row["original_msg_id"],
        },
    }


def _row_matches_filter(row: sqlite3.Row, query: dict[str, Any]) -> bool:
    archive_ids = _string_list(query.get("archive_ids"))
    if archive_ids and row["archive_id"] not in archive_ids:
        return False
    conversation_ids = _string_list(query.get("conversation_ids"))
    if conversation_ids and not _matches_values(
        str(row["conversation_id"] or ""),
        conversation_ids,
        str(query.get("conversation_match") or "exact"),
    ):
        return False
    sender_ids = _string_list(query.get("sender_ids"))
    if sender_ids and str(row["sender_id"] or "") not in sender_ids:
        return False
    sender_names = _string_list(query.get("sender_names"))
    if sender_names and not _matches_values(
        str(row["sender_name"] or ""),
        sender_names,
        str(query.get("sender_match") or "exact"),
    ):
        return False
    message_kinds = _string_list(query.get("message_kinds"))
    if message_kinds and row["message_kind"] not in message_kinds:
        return False
    if not _matches_time_ranges(row, query.get("time_ranges")):
        return False
    keywords = _string_list(query.get("keywords"))
    if keywords and not _matches_keywords(
        str(row["content_search"] or ""),
        keywords,
        str(query.get("keyword_match") or "contains"),
        str(query.get("keyword_operator") or "all"),
    ):
        return False
    return True


def _matches_values(text: str, values: list[str], mode: str) -> bool:
    if not values:
        return True
    text_norm = text.lower()
    for value in values:
        value_norm = value.lower()
        if mode == "exact" and text_norm == value_norm:
            return True
        if mode == "contains" and value_norm in text_norm:
            return True
        if mode == "fuzzy" and _fuzzy_match(text_norm, value_norm):
            return True
    return False


def _matches_keywords(text: str, keywords: list[str], mode: str, operator: str) -> bool:
    checks = [_matches_values(text, [keyword], mode) for keyword in keywords]
    if operator == "any":
        return any(checks)
    return all(checks)


def _matches_time_ranges(row: sqlite3.Row, raw_ranges: Any) -> bool:
    ranges = _time_range_list(raw_ranges)
    if not ranges:
        return True
    ts = row["timestamp_unix"]
    text = _legacy_search_text(row)
    for item in ranges:
        start_raw = item.get("start")
        end_raw = item.get("end")
        start_ts = _timestamp_parts(_clean_optional(start_raw))[0] if start_raw else None
        end_ts = _timestamp_parts(_clean_optional(end_raw))[0] if end_raw else None
        if ts is not None and (start_ts is not None or end_ts is not None):
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts > end_ts:
                continue
            return True
        needle = " ".join(
            value for value in (_clean_optional(start_raw), _clean_optional(end_raw)) if value
        )
        if needle and needle in text:
            return True
    return False


def _fuzzy_match(text: str, needle: str) -> bool:
    if not needle:
        return True
    if needle in text:
        return True
    if not text:
        return False
    if SequenceMatcher(None, text, needle).ratio() >= 0.62:
        return True
    size = len(needle)
    if size <= 1:
        return False
    for width in range(max(1, size - 2), min(len(text), size + 6) + 1):
        for start in range(0, max(1, len(text) - width + 1)):
            if SequenceMatcher(None, text[start:start + width], needle).ratio() >= 0.72:
                return True
    return False


def _legacy_search_text(row: sqlite3.Row) -> str:
    return "\n".join(
        str(value or "")
        for value in (
            row["timestamp"],
            row["date_key"],
            row["month_key"],
            row["conversation_id"],
            row["sender_id"],
            row["sender_name"],
            row["content_search"],
            row["original_msg_id"],
            row["reply_to_msg_id"],
        )
    )


def _record_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False, default=str).strip()
    except TypeError:
        return str(content).strip()


def _clean_display_content(text: str) -> str:
    text = _normalize_media_placeholders(text)
    text = _MEDIA_URL_ATTR_PATTERN.sub("", text)
    text = re.sub(r"\s+\]", "]", text)
    return text.strip()


def _clean_search_content(text: str) -> str:
    text = _URL_PATTERN.sub("[链接]", text)
    text = _WINDOWS_PATH_PATTERN.sub("[本地路径]", text)
    text = _SECRET_QUERY_PATTERN.sub("", text)
    text = re.sub(r"\[链接\](?:\s*\[链接\])+", "[链接]", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _normalize_media_placeholders(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        kind = match.group(1)
        attrs = match.group(2) or ""
        workspace = _extract_attr(attrs, "workspace")
        if kind == "音频消息":
            transcript = _MEDIA_RUNTIME_ATTR_PATTERN.sub("", attrs).strip()
            transcript = re.sub(r"^[:：]\s*", "", transcript).strip()
            if transcript:
                return (
                    f"[音频消息: {transcript} workspace={workspace}]"
                    if workspace
                    else f"[音频消息: {transcript}]"
                )
            return f"[音频消息 workspace={workspace}]" if workspace else "[音频消息]"
        return f"[{kind} workspace={workspace}]" if workspace else f"[{kind}]"

    return _MEDIA_PLACEHOLDER_PATTERN.sub(repl, text)


def _extract_media(content: Any) -> list[dict[str, Any]]:
    text = _record_content_text(content)
    result: list[dict[str, Any]] = []
    for match in _MEDIA_PLACEHOLDER_PATTERN.finditer(text):
        attrs = match.group(2) or ""
        workspace = _extract_attr(attrs, "workspace")
        if not workspace:
            continue
        media_type = {
            "图片": "image",
            "文件": "file",
            "音频消息": "audio",
        }.get(match.group(1), "media")
        result.append(
            {
                "media_type": media_type,
                "workspace_path": workspace,
                "original_name": _extract_attr(attrs, "name")
                or _extract_attr(attrs, "file_name"),
                "metadata": {"raw": match.group(0)},
            }
        )
    return result


def _extract_attr(attrs: str, name: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(name)}=([^\]\s]+)", attrs or "")
    return match.group(1).strip() if match else ""


def _extract_timestamp(record: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    for value in (record.get("timestamp"), metadata.get("timestamp")):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned
    messages = metadata.get("messages")
    if isinstance(messages, list) and messages:
        for item in messages:
            if isinstance(item, dict):
                cleaned = _clean_optional(item.get("timestamp"))
                if cleaned:
                    return cleaned
    return None


def _timestamp_parts(timestamp: str | None) -> tuple[int | None, str | None, str | None]:
    if not timestamp:
        return None, None, None
    dt = _parse_datetime(timestamp)
    if dt is None:
        return None, None, None
    return int(dt.timestamp()), dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m")


def _parse_datetime(value: str) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError):
            return None
    normalized = text.replace("T", " ").replace("Z", "+00:00")
    candidates = [
        normalized,
        normalized[:19],
        normalized[:16],
        normalized[:10],
    ]
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ]
    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                pass
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _extract_conversation_id(record: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    direct = _clean_optional(record.get("conversation_id"))
    if direct:
        return direct
    message = _first_meta_message(metadata)
    if message:
        scope = _clean_optional(message.get("scope"))
        target_id = _clean_optional(message.get("target_id"))
        group_id = _clean_optional(message.get("group_id"))
        user_id = _clean_optional(message.get("user_id"))
        if scope == "group" and (group_id or target_id):
            return f"group:{group_id or target_id}"
        if scope == "private" and (target_id or user_id):
            return f"private:{target_id or user_id}"
        if group_id:
            return f"group:{group_id}"
        if user_id:
            return f"private:{user_id}"
    scope = _clean_optional(metadata.get("scope"))
    target_id = _clean_optional(metadata.get("target_id"))
    group_id = _clean_optional(metadata.get("group_id"))
    user_id = _clean_optional(metadata.get("user_id"))
    if scope == "group" and (group_id or target_id):
        return f"group:{group_id or target_id}"
    if scope == "private" and (target_id or user_id):
        return f"private:{target_id or user_id}"
    if group_id:
        return f"group:{group_id}"
    if user_id:
        return f"private:{user_id}"
    return None


def _conversation_parts(
    conversation_id: str | None,
    metadata: dict[str, Any],
) -> tuple[str, str | None]:
    if conversation_id and ":" in conversation_id:
        conversation_type, target_id = conversation_id.split(":", 1)
        return conversation_type or "unknown", target_id or None
    message = _first_meta_message(metadata)
    if message:
        scope = _clean_optional(message.get("scope"))
        target_id = (
            _clean_optional(message.get("target_id"))
            or _clean_optional(message.get("group_id"))
            or _clean_optional(message.get("user_id"))
        )
        return scope or "unknown", target_id
    return "unknown", None


def _sender_parts(
    record: dict[str, Any],
    metadata: dict[str, Any],
    role: str,
) -> tuple[str | None, str | None]:
    message = _first_meta_message(metadata)
    if role == "user" and message:
        return (
            _clean_optional(message.get("user_id")),
            _clean_optional(message.get("nickname")),
        )
    if role == "assistant":
        return _clean_optional(record.get("sender_id")) or "assistant", "assistant"
    if role in {"system", "tool"}:
        return role, role
    return (
        _clean_optional(record.get("sender_id")) or _clean_optional(metadata.get("sender_id")),
        _clean_optional(record.get("sender_name")) or _clean_optional(metadata.get("sender_name")),
    )


def _direction_for(record: dict[str, Any], metadata: dict[str, Any], role: str) -> str:
    if _metadata_is_runtime(metadata) or _text_is_runtime(_record_content_text(record.get("content"))):
        return "runtime"
    if role == "user":
        return "inbound"
    if role == "assistant" and not record.get("tool_calls"):
        return "outbound"
    if role in {"assistant", "system", "tool"}:
        return "runtime"
    return "unknown"


def _message_kind_for(
    content: str,
    record: dict[str, Any],
    metadata: dict[str, Any],
    role: str,
) -> str:
    if role in {"system", "tool"} or record.get("tool_calls") or _metadata_is_runtime(metadata):
        return "runtime"
    kinds: set[str] = set()
    if "[图片" in content:
        kinds.add("image")
    if "[文件" in content:
        kinds.add("file")
    if "[音频消息" in content:
        kinds.add("audio")
    if "[合并转发" in content:
        kinds.add("forward")
    if not kinds:
        return "text"
    return next(iter(kinds)) if len(kinds) == 1 else "mixed"


def _extract_original_msg_id(record: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    for value in (record.get("original_msg_id"), record.get("message_id"), record.get("msg_id")):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned
    message = _first_meta_message(metadata)
    if message:
        return _clean_optional(message.get("message_id"))
    return None


def _first_meta_message(metadata: dict[str, Any]) -> dict[str, Any] | None:
    messages = metadata.get("messages")
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict):
                return item
    return None


def _metadata_is_runtime(metadata: dict[str, Any]) -> bool:
    return metadata.get("kind") in {"task_context_snapshot", "send_done_snapshot"}


def _text_is_runtime(text: str) -> bool:
    return any(marker in text for marker in _RUNTIME_CONTEXT_MARKERS)


def _query_to_dict(query: Any) -> dict[str, Any]:
    if query is None:
        return {}
    if isinstance(query, dict):
        return dict(query)
    model_dump = getattr(query, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True))
    return dict(getattr(query, "__dict__", {}) or {})


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        cleaned = _clean_optional(item)
        if cleaned:
            result.append(cleaned)
    return result


def _time_range_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append(dict(item))
            continue
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            result.append(dict(model_dump(exclude_none=True)))
    return result


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _clean_id(value: Any) -> str:
    return _clean_optional(value).lower()


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: Any, *, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value <= 0:
        return "0"
    digits: list[str] = []
    while value:
        value, rest = divmod(value, 36)
        digits.append(alphabet[rest])
    return "".join(reversed(digits))


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_MEDIA_PLACEHOLDER_PATTERN = re.compile(
    r"\[(图片|文件|音频消息)([^\]]*(?:url=|workspace=)[^\]]*)\]"
)
_MEDIA_URL_ATTR_PATTERN = re.compile(r"\surl=(?:[^\]\s]+)")
_MEDIA_RUNTIME_ATTR_PATTERN = re.compile(r"\s(?:url|workspace)=(?:[^\]\s]+)")
_URL_PATTERN = re.compile(r"https?://[^\s\]）)>\"']+")
_WINDOWS_PATH_PATTERN = re.compile(r"(?<![\w/\\])[A-Za-z]:[\\/][^\s\]）)>\"']+")
_SECRET_QUERY_PATTERN = re.compile(r"(?i)(?:rkey|clientkey|skey|token)=[^&\s\]]+")
_RUNTIME_CONTEXT_MARKERS = (
    "<task_context",
    "</task_context>",
    "<send_status",
    "</send_status>",
    "<send_receipt",
    "</send_receipt>",
    "<send_receipt_task",
    "</send_receipt_task>",
)
