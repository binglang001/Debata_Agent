"""旧 memory/logs 文件到 diana.db 的同步导入入口。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from .diana_db import DianaDB, backup_existing_database
from .diana_stores import DianaHistoryStore, DianaImportantStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LegacyImportDomainResult:
    """单个旧文件域的导入统计。"""

    imported: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class LegacyMemoryImportResult:
    """旧文件导入的汇总结果，供 runtime 后续展示。"""

    backup_path: Path | None
    history: LegacyImportDomainResult
    important: LegacyImportDomainResult
    rolling_summary: LegacyImportDomainResult
    usage: LegacyImportDomainResult
    events: LegacyImportDomainResult = LegacyImportDomainResult()


@dataclass(frozen=True, slots=True)
class _LegacyEventRow:
    event_id: int
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


def import_legacy_memory_files(
    db: DianaDB | str | Path,
    source_dir: str | Path,
    persona_id: str,
    *,
    backup: bool = True,
    usage_persona_id: str | None = None,
) -> LegacyMemoryImportResult:
    """同步导入第一批旧 memory/logs 文件到 diana.db。

    `usage_persona_id=None` 表示跟随 `persona_id`；传入空字符串表示全局 usage。
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _import_legacy_memory_files_async(
                db,
                source_dir,
                persona_id,
                backup=backup,
                usage_persona_id=usage_persona_id,
            )
        )
    raise RuntimeError("import_legacy_memory_files cannot run inside a running event loop")


async def _import_legacy_memory_files_async(
    db: DianaDB | str | Path,
    source_dir: str | Path,
    persona_id: str,
    *,
    backup: bool,
    usage_persona_id: str | None,
) -> LegacyMemoryImportResult:
    target_db = db if isinstance(db, DianaDB) else DianaDB(db)
    should_close_db = not isinstance(db, DianaDB)
    source_path = Path(source_dir)
    normalized_persona = _normalize_persona_id(persona_id)
    normalized_usage_persona = _normalize_usage_persona_id(
        normalized_persona,
        usage_persona_id,
    )

    backup_path = None
    if backup and target_db.path.exists():
        backup_path = backup_existing_database(target_db.path)

    try:
        target_db.load()
        history_result = await _import_history(target_db, source_path, normalized_persona)
        important_result = await _import_important(target_db, source_path, normalized_persona)
        rolling_result = await _import_rolling_summary(
            target_db,
            source_path,
            normalized_persona,
        )
        usage_result = _import_usage(target_db, source_path, normalized_usage_persona)
        events_result = _import_events(target_db, source_path, normalized_persona)
    finally:
        if should_close_db:
            target_db.close()

    return LegacyMemoryImportResult(
        backup_path=backup_path,
        history=history_result,
        important=important_result,
        rolling_summary=rolling_result,
        usage=usage_result,
        events=events_result,
    )


async def _import_history(
    db: DianaDB,
    source_dir: Path,
    persona_id: str,
) -> LegacyImportDomainResult:
    path = source_dir / "history.jsonl"
    records, skipped, exists = _read_jsonl_objects(path)
    if not exists:
        return LegacyImportDomainResult()
    store = DianaHistoryStore(db, persona_id)
    await store.replace_all(records)
    return LegacyImportDomainResult(imported=len(records), skipped=skipped)


async def _import_important(
    db: DianaDB,
    source_dir: Path,
    persona_id: str,
) -> LegacyImportDomainResult:
    path = source_dir / "important.json"
    data, skipped, exists = _read_json_file(path)
    if not exists or skipped:
        return LegacyImportDomainResult(skipped=skipped)
    store = DianaImportantStore(db, persona_id)
    await store.write(data)
    return LegacyImportDomainResult(imported=_important_import_count(data))


async def _import_rolling_summary(
    db: DianaDB,
    source_dir: Path,
    persona_id: str,
) -> LegacyImportDomainResult:
    path = source_dir / "rolling_summary.json"
    data, skipped, exists = _read_json_file(path)
    if not exists or skipped:
        return LegacyImportDomainResult(skipped=skipped)
    if not isinstance(data, dict):
        logger.warning("跳过非对象旧 rolling_summary JSON: %s", path)
        return LegacyImportDomainResult(skipped=1)

    _upsert_rolling_summary(db, persona_id, data)
    return LegacyImportDomainResult(imported=1)


def _upsert_rolling_summary(
    db: DianaDB,
    persona_id: str,
    data: dict[str, Any],
) -> None:
    with closing(_connect_for_import(db)) as conn:
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
                    persona_id,
                    str(data.get("summary_text") or "").strip(),
                    _json_data(data.get("archived_until")),
                    _coerce_non_negative_int(data.get("active_start_index")),
                    _json_data(data),
                    str(data.get("updated_at") or ""),
                ),
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def _import_usage(
    db: DianaDB,
    source_dir: Path,
    persona_id: str | None,
) -> LegacyImportDomainResult:
    path = source_dir / "model_usage.jsonl"
    records, skipped, exists = _read_jsonl_objects(path)
    if not exists:
        return LegacyImportDomainResult()
    imported, duplicate_count = _insert_usage_records(db, records, persona_id)
    return LegacyImportDomainResult(
        imported=imported,
        skipped=skipped + duplicate_count,
    )


def _import_events(
    db: DianaDB,
    source_dir: Path,
    persona_id: str,
) -> LegacyImportDomainResult:
    sqlite_rows, sqlite_skipped, sqlite_exists = _read_legacy_event_sqlite_rows(
        source_dir / "events.sqlite3"
    )
    append_rows, append_skipped, append_exists = _read_legacy_event_append_log_rows(
        source_dir / "events.sqlite3.append.jsonl"
    )
    if not sqlite_exists and not append_exists:
        return LegacyImportDomainResult()

    imported, duplicate_or_conflict_skipped = _insert_event_rows(
        db,
        persona_id,
        [*sqlite_rows, *append_rows],
    )
    return LegacyImportDomainResult(
        imported=imported,
        skipped=sqlite_skipped + append_skipped + duplicate_or_conflict_skipped,
    )


def _read_legacy_event_sqlite_rows(
    path: Path,
) -> tuple[list[_LegacyEventRow], int, bool]:
    if not path.exists():
        return [], 0, False

    rows: list[_LegacyEventRow] = []
    skipped = 0
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            source_rows = conn.execute(
                """
                SELECT
                    event_id, event_type, event_uuid, conversation_id, session_id,
                    turn_id, source, external_id, tool_call_id, parent_event_id,
                    idempotency_key, timestamp_unix, created_at_unix, payload_json,
                    payload_hash, schema_version
                FROM event_log
                ORDER BY event_id ASC
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            logger.warning("旧 events.sqlite3 缺少 event_log 表，跳过事件导入: %s", path)
            return [], 0, False
        logger.warning("读取旧 events.sqlite3 失败，跳过事件导入: %s error=%s", path, exc)
        return [], 0, False
    except sqlite3.Error as exc:
        logger.warning("读取旧 events.sqlite3 失败，跳过事件导入: %s error=%s", path, exc)
        return [], 0, False

    for index, source_row in enumerate(source_rows, start=1):
        event_row = _legacy_event_row_from_mapping(source_row, f"{path}:row {index}")
        if event_row is None:
            skipped += 1
            continue
        rows.append(event_row)
    return rows, skipped, True


def _read_legacy_event_append_log_rows(
    path: Path,
) -> tuple[list[_LegacyEventRow], int, bool]:
    if not path.exists():
        return [], 0, False

    rows: list[_LegacyEventRow] = []
    skipped = 0
    for line_number, raw_line in enumerate(path.read_bytes().splitlines(), start=1):
        line = raw_line.strip()
        location = f"{path}:{line_number}"
        if not line:
            skipped += 1
            logger.warning("跳过空的旧事件 append log 行: %s", location)
            continue
        try:
            data = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            skipped += 1
            logger.warning("跳过损坏的旧事件 append log 行: %s error=%s", location, exc)
            continue
        if not isinstance(data, dict):
            skipped += 1
            logger.warning("跳过非对象旧事件 append log 行: %s", location)
            continue
        event_row = _legacy_event_row_from_mapping(data, location)
        if event_row is None:
            skipped += 1
            continue
        rows.append(event_row)
    return rows, skipped, True


def _legacy_event_row_from_mapping(
    record: sqlite3.Row | dict[str, Any],
    location: str,
) -> _LegacyEventRow | None:
    event_id = _positive_int(_mapping_value(record, "event_id"))
    event_type = _required_text(_mapping_value(record, "event_type"))
    event_uuid = _required_text(_mapping_value(record, "event_uuid"))
    timestamp_unix = _required_float(_mapping_value(record, "timestamp_unix"))
    created_at_unix = _required_float(_mapping_value(record, "created_at_unix"))
    payload_json = _required_raw_json_text(_mapping_value(record, "payload_json"))
    payload_hash = _required_text(_mapping_value(record, "payload_hash"))
    schema_version = _required_int(_mapping_value(record, "schema_version"))
    if (
        event_id is None
        or event_type is None
        or event_uuid is None
        or timestamp_unix is None
        or created_at_unix is None
        or payload_json is None
        or payload_hash is None
        or schema_version is None
    ):
        logger.warning("跳过缺少关键字段的旧事件行: %s", location)
        return None
    try:
        orjson.loads(payload_json)
    except orjson.JSONDecodeError as exc:
        logger.warning("跳过 payload_json 损坏的旧事件行: %s error=%s", location, exc)
        return None

    return _LegacyEventRow(
        event_id=event_id,
        event_type=event_type,
        event_uuid=event_uuid,
        conversation_id=_optional_text(_mapping_value(record, "conversation_id")),
        session_id=_optional_text(_mapping_value(record, "session_id")),
        turn_id=_optional_text(_mapping_value(record, "turn_id")),
        source=_optional_text(_mapping_value(record, "source")),
        external_id=_optional_text(_mapping_value(record, "external_id")),
        tool_call_id=_optional_text(_mapping_value(record, "tool_call_id")),
        parent_event_id=_optional_int(_mapping_value(record, "parent_event_id")),
        idempotency_key=_optional_text(_mapping_value(record, "idempotency_key")),
        timestamp_unix=timestamp_unix,
        created_at_unix=created_at_unix,
        payload_json=payload_json,
        payload_hash=payload_hash,
        schema_version=schema_version,
    )


def _insert_event_rows(
    db: DianaDB,
    persona_id: str,
    rows: Sequence[_LegacyEventRow],
) -> tuple[int, int]:
    imported = 0
    skipped = 0
    with closing(_connect_for_import(db)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            events_by_id = _existing_events_by_id(conn, persona_id)
            ids_by_key = _existing_event_ids_by_idempotency_key(conn, persona_id)
            for row in rows:
                existing = events_by_id.get(row.event_id)
                if existing is not None:
                    skipped += 1
                    if not _legacy_events_equivalent(existing, row):
                        logger.warning(
                            "跳过冲突的旧事件 event_id=%s persona_id=%s",
                            row.event_id,
                            persona_id,
                        )
                    continue

                if row.idempotency_key is not None:
                    idempotency_event_id = ids_by_key.get(row.idempotency_key)
                    if idempotency_event_id is not None:
                        skipped += 1
                        if idempotency_event_id != row.event_id:
                            logger.warning(
                                "跳过冲突的旧事件 idempotency_key=%s persona_id=%s "
                                "existing_event_id=%s incoming_event_id=%s",
                                row.idempotency_key,
                                persona_id,
                                idempotency_event_id,
                                row.event_id,
                            )
                        continue

                _insert_event_row(conn, persona_id, row)
                events_by_id[row.event_id] = row
                if row.idempotency_key is not None:
                    ids_by_key[row.idempotency_key] = row.event_id
                imported += 1

            _set_event_projection_progress(conn, persona_id, _max_event_id(conn, persona_id))
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    return imported, skipped


def _existing_events_by_id(
    conn: sqlite3.Connection,
    persona_id: str,
) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM event_log
        WHERE persona_id = ?
        """,
        (persona_id,),
    ).fetchall()
    return {int(row["event_id"]): row for row in rows}


def _existing_event_ids_by_idempotency_key(
    conn: sqlite3.Connection,
    persona_id: str,
) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT event_id, idempotency_key
        FROM event_log
        WHERE persona_id = ? AND idempotency_key IS NOT NULL
        """,
        (persona_id,),
    ).fetchall()
    return {str(row["idempotency_key"]): int(row["event_id"]) for row in rows}


def _insert_event_row(
    conn: sqlite3.Connection,
    persona_id: str,
    row: _LegacyEventRow,
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
            persona_id,
            row.event_id,
            row.event_type,
            row.event_uuid,
            row.conversation_id,
            row.session_id,
            row.turn_id,
            row.source,
            row.external_id,
            row.tool_call_id,
            row.parent_event_id,
            row.idempotency_key,
            row.timestamp_unix,
            row.created_at_unix,
            row.payload_json,
            row.payload_hash,
            row.schema_version,
        ),
    )


def _set_event_projection_progress(
    conn: sqlite3.Connection,
    persona_id: str,
    event_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO event_projection_state(persona_id, name, value)
        VALUES (?, ?, ?)
        ON CONFLICT(persona_id, name) DO UPDATE SET value = excluded.value
        """,
        (persona_id, "last_projected_event_id", str(event_id)),
    )


def _max_event_id(conn: sqlite3.Connection, persona_id: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COALESCE(MAX(event_id), 0)
            FROM event_log
            WHERE persona_id = ?
            """,
            (persona_id,),
        ).fetchone()[0]
    )


def _legacy_events_equivalent(
    existing: sqlite3.Row | _LegacyEventRow,
    incoming: _LegacyEventRow,
) -> bool:
    return all(
        _event_value(existing, column) == getattr(incoming, column)
        for column in _LEGACY_EVENT_EQUIVALENCE_COLUMNS
    )


_LEGACY_EVENT_EQUIVALENCE_COLUMNS = (
    "event_id",
    "event_type",
    "event_uuid",
    "conversation_id",
    "session_id",
    "turn_id",
    "source",
    "external_id",
    "tool_call_id",
    "parent_event_id",
    "idempotency_key",
    "timestamp_unix",
    "created_at_unix",
    "payload_json",
    "payload_hash",
    "schema_version",
)


def _insert_usage_records(
    db: DianaDB,
    records: Sequence[dict[str, Any]],
    persona_id: str | None,
) -> tuple[int, int]:
    if not records:
        return 0, 0

    record_json_values = [_json_data(record) for record in records]
    with closing(_connect_for_import(db)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _existing_usage_record_json(conn, persona_id, record_json_values)
            seen: set[str] = set()
            rows: list[tuple[Any, ...]] = []
            duplicate_count = 0
            for record, record_json in zip(records, record_json_values, strict=True):
                if record_json in existing or record_json in seen:
                    duplicate_count += 1
                    continue
                seen.add(record_json)
                rows.append(_usage_record_row(persona_id, record, record_json))

            if rows:
                conn.executemany(
                    """
                    INSERT INTO usage_records (
                        persona_id, ts, provider, model, agent, operation,
                        prompt_tokens, completion_tokens, reasoning_tokens,
                        cached_tokens, cache_creation_tokens, total_tokens,
                        record_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    return len(rows), duplicate_count


def _existing_usage_record_json(
    conn: sqlite3.Connection,
    persona_id: str | None,
    record_json_values: Sequence[str],
) -> set[str]:
    existing: set[str] = set()
    unique_values = list(dict.fromkeys(record_json_values))
    for start in range(0, len(unique_values), 500):
        chunk = unique_values[start:start + 500]
        if not chunk:
            continue
        placeholders = ", ".join("?" for _ in chunk)
        if persona_id is None:
            rows = conn.execute(
                f"""
                SELECT record_json
                FROM usage_records
                WHERE persona_id IS NULL AND record_json IN ({placeholders})
                """,
                chunk,
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT record_json
                FROM usage_records
                WHERE persona_id = ? AND record_json IN ({placeholders})
                """,
                [persona_id, *chunk],
            ).fetchall()
        existing.update(str(row["record_json"]) for row in rows)
    return existing


def _usage_record_row(
    persona_id: str | None,
    record: dict[str, Any],
    record_json: str,
) -> tuple[Any, ...]:
    return (
        persona_id,
        _coerce_float(record.get("ts")),
        _record_text(record.get("provider")),
        _record_text(record.get("model")),
        _record_text(record.get("agent")),
        _record_text(record.get("operation")),
        _coerce_int(record.get("prompt_tokens")),
        _coerce_int(record.get("completion_tokens")),
        _coerce_int(record.get("reasoning_tokens")),
        _coerce_int(record.get("cached_tokens")),
        _coerce_int(record.get("cache_creation_tokens")),
        _coerce_int(record.get("total_tokens")),
        record_json,
    )


def _read_jsonl_objects(path: Path) -> tuple[list[dict[str, Any]], int, bool]:
    if not path.exists():
        return [], 0, False

    records: list[dict[str, Any]] = []
    skipped = 0
    for line_number, raw_line in enumerate(path.read_bytes().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            skipped += 1
            logger.warning("跳过空的旧 JSONL 行: %s:%d", path, line_number)
            continue
        try:
            data = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            skipped += 1
            logger.warning(
                "跳过损坏的旧 JSONL 行: %s:%d error=%s",
                path,
                line_number,
                exc,
            )
            continue
        if not isinstance(data, dict):
            skipped += 1
            logger.warning("跳过非对象旧 JSONL 行: %s:%d", path, line_number)
            continue
        records.append(data)
    return records, skipped, True


def _read_json_file(path: Path) -> tuple[Any, int, bool]:
    if not path.exists():
        return None, 0, False

    content = path.read_bytes()
    if not content.strip():
        logger.warning("跳过空的旧 JSON 文件: %s", path)
        return None, 1, True
    try:
        return orjson.loads(content), 0, True
    except orjson.JSONDecodeError as exc:
        logger.warning("跳过损坏的旧 JSON 文件: %s error=%s", path, exc)
        return None, 1, True


def _connect_for_import(db: DianaDB) -> sqlite3.Connection:
    conn = sqlite3.connect(db.path, timeout=db.busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(db.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _mapping_value(record: sqlite3.Row | dict[str, Any], key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    return record[key] if key in record.keys() else None


def _event_value(record: sqlite3.Row | _LegacyEventRow, key: str) -> Any:
    if isinstance(record, _LegacyEventRow):
        return getattr(record, key)
    return record[key]


def _normalize_persona_id(persona_id: str) -> str:
    text = str(persona_id).strip()
    if not text:
        raise ValueError("persona_id must not be empty")
    return text


def _normalize_usage_persona_id(
    persona_id: str,
    usage_persona_id: str | None,
) -> str | None:
    if usage_persona_id is None:
        return persona_id
    text = str(usage_persona_id).strip()
    return text or None


def _important_import_count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    return 1


def _json_data(data: Any) -> str:
    return orjson.dumps(data).decode("utf-8")


def _record_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _required_text(value: Any) -> str | None:
    text = _optional_text(value)
    return text


def _required_raw_json_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value.strip() else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _required_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int | None:
    number = _required_int(value)
    if number is None or number <= 0:
        return None
    return number


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _required_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "LegacyImportDomainResult",
    "LegacyMemoryImportResult",
    "import_legacy_memory_files",
]
