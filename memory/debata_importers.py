"""旧 memory/logs 文件到 debata.db 的同步导入入口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from .debata_db import DebataDB, backup_existing_database
from .debata_stores import DebataHistoryStore, DebataImportantStore

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
    archive: LegacyImportDomainResult = LegacyImportDomainResult()
    persona: LegacyImportDomainResult = LegacyImportDomainResult()


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


@dataclass(frozen=True, slots=True)
class _LegacyArchiveMessageRow:
    rowid: int
    archive_id: str
    timestamp: Any
    timestamp_unix: Any
    date_key: Any
    month_key: Any
    conversation_id: Any
    conversation_type: Any
    target_id: Any
    sender_id: Any
    sender_name: Any
    sender_role: Any
    direction: Any
    message_kind: Any
    content: Any
    content_search: Any
    original_msg_id: Any
    reply_to_msg_id: Any
    metadata_json: Any
    record_json: Any
    created_at: Any


@dataclass(frozen=True, slots=True)
class _LegacyArchiveMediaRow:
    id: int
    archive_id: str
    media_type: Any
    workspace_path: Any
    original_name: Any
    metadata_json: Any


@dataclass(frozen=True, slots=True)
class _LegacyPersonaTableSpec:
    source_table: str
    target_table: str
    target_columns: tuple[str, ...]
    key_columns: tuple[str, ...]
    optional_target_columns: tuple[str, ...] = ()
    required_source_columns: tuple[str, ...] = ()
    id_fixed_to_one: bool = False
    id_must_be_one: bool = False
    derive_eat_record_id: bool = False


@dataclass(frozen=True, slots=True)
class _LegacyImportantMergeSource:
    items: list[Any]
    skipped: int = 0
    valid: bool = False


def import_legacy_memory_files(
    db: DebataDB | str | Path,
    source_dir: str | Path,
    persona_id: str,
    *,
    backup: bool = True,
    usage_persona_id: str | None = None,
    usage_source_path: str | Path | None = None,
    skip_existing_domains: bool = False,
) -> LegacyMemoryImportResult:
    """同步导入第一批旧 memory/logs 文件到 debata.db。

    `usage_persona_id=None` 表示跟随 `persona_id`；传入空字符串表示全局 usage。
    `usage_source_path=None` 保持旧行为，从 `source_dir / "model_usage.jsonl"` 导入。
    `skip_existing_domains=True` 时，history/important/rolling 等已有数据的域不会被旧文件覆盖。
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
                usage_source_path=usage_source_path,
                skip_existing_domains=skip_existing_domains,
            )
        )
    raise RuntimeError("import_legacy_memory_files cannot run inside a running event loop")


async def import_legacy_memory_files_async(
    db: DebataDB | str | Path,
    source_dir: str | Path,
    persona_id: str,
    *,
    backup: bool = True,
    usage_persona_id: str | None = None,
    usage_source_path: str | Path | None = None,
    skip_existing_domains: bool = False,
) -> LegacyMemoryImportResult:
    """异步导入第一批旧 memory/logs 文件到 debata.db。"""

    return await _import_legacy_memory_files_async(
        db,
        source_dir,
        persona_id,
        backup=backup,
        usage_persona_id=usage_persona_id,
        usage_source_path=usage_source_path,
        skip_existing_domains=skip_existing_domains,
    )


async def _import_legacy_memory_files_async(
    db: DebataDB | str | Path,
    source_dir: str | Path,
    persona_id: str,
    *,
    backup: bool,
    usage_persona_id: str | None,
    usage_source_path: str | Path | None,
    skip_existing_domains: bool,
) -> LegacyMemoryImportResult:
    target_db = db if isinstance(db, DebataDB) else DebataDB(db)
    should_close_db = not isinstance(db, DebataDB)
    source_path = Path(source_dir)
    usage_path = (
        Path(usage_source_path)
        if usage_source_path is not None
        else source_path / "model_usage.jsonl"
    )
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
        persona_important_source = _read_legacy_persona_important_source(
            source_path / "persona.db"
        )
        history_result = LegacyImportDomainResult()
        if not skip_existing_domains or _domain_row_count(
            target_db,
            "history_records",
            normalized_persona,
        ) == 0:
            history_result = await _import_history(target_db, source_path, normalized_persona)

        important_result = LegacyImportDomainResult()
        if not skip_existing_domains or _domain_row_count(
            target_db,
            "important_memories",
            normalized_persona,
        ) == 0:
            important_result = await _import_important(
                target_db,
                source_path,
                normalized_persona,
                persona_important_source,
            )

        rolling_result = LegacyImportDomainResult()
        if not skip_existing_domains or _domain_row_count(
            target_db,
            "rolling_summary",
            normalized_persona,
        ) == 0:
            rolling_result = await _import_rolling_summary(
                target_db,
                source_path,
                normalized_persona,
            )

        usage_result = LegacyImportDomainResult()
        if not skip_existing_domains or _domain_row_count(
            target_db,
            "usage_records",
            normalized_usage_persona,
        ) == 0:
            usage_result = _import_usage(target_db, usage_path, normalized_usage_persona)

        events_result = LegacyImportDomainResult()
        if not skip_existing_domains or _domain_row_count(
            target_db,
            "event_log",
            normalized_persona,
        ) == 0:
            events_result = _import_events(target_db, source_path, normalized_persona)

        archive_result = LegacyImportDomainResult()
        if not skip_existing_domains or _domain_row_count(
            target_db,
            "archive_messages",
            normalized_persona,
        ) == 0:
            archive_result = _import_archive(target_db, source_path, normalized_persona)

        persona_result = LegacyImportDomainResult()
        if not skip_existing_domains or _persona_domain_row_count(
            target_db,
            normalized_persona,
        ) == 0:
            persona_result = _import_persona(target_db, source_path, normalized_persona)
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
        archive=archive_result,
        persona=persona_result,
    )


async def _import_history(
    db: DebataDB,
    source_dir: Path,
    persona_id: str,
) -> LegacyImportDomainResult:
    path = source_dir / "history.jsonl"
    records, skipped, exists = _read_jsonl_objects(path)
    if not exists:
        return LegacyImportDomainResult()
    store = DebataHistoryStore(db, persona_id)
    await store.replace_all(records)
    return LegacyImportDomainResult(imported=len(records), skipped=skipped)


async def _import_important(
    db: DebataDB,
    source_dir: Path,
    persona_id: str,
    persona_important_source: _LegacyImportantMergeSource | None = None,
) -> LegacyImportDomainResult:
    persona_source = persona_important_source or _LegacyImportantMergeSource([])
    path = source_dir / "important.json"
    data, skipped, exists = _read_json_file(path)
    total_skipped = persona_source.skipped + skipped

    store = DebataImportantStore(db, persona_id)
    important_items: list[Any] = []
    important_is_list = False
    if exists and not skipped:
        if isinstance(data, list):
            important_items = data
            important_is_list = True
        elif persona_source.valid:
            total_skipped += 1
            logger.warning("跳过非列表旧 important JSON，无法合并重要记忆: %s", path)
        else:
            await store.write(data)
            return LegacyImportDomainResult(
                imported=_important_import_count(data),
                skipped=total_skipped,
            )

    if persona_source.valid:
        merged_items, duplicate_skipped = _merge_important_items(
            persona_source.items,
            important_items,
        )
        await store.write(merged_items)
        return LegacyImportDomainResult(
            imported=len(merged_items),
            skipped=total_skipped + duplicate_skipped,
        )

    if important_is_list:
        await store.write(data)
        return LegacyImportDomainResult(
            imported=_important_import_count(data),
            skipped=total_skipped,
        )

    return LegacyImportDomainResult(skipped=total_skipped)


async def _import_rolling_summary(
    db: DebataDB,
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
    db: DebataDB,
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
    db: DebataDB,
    path: Path,
    persona_id: str | None,
) -> LegacyImportDomainResult:
    records, skipped, exists = _read_jsonl_objects(path)
    if not exists:
        return LegacyImportDomainResult()
    imported, duplicate_count = _insert_usage_records(db, records, persona_id)
    return LegacyImportDomainResult(
        imported=imported,
        skipped=skipped + duplicate_count,
    )


def _import_events(
    db: DebataDB,
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


def _import_archive(
    db: DebataDB,
    source_dir: Path,
    persona_id: str,
) -> LegacyImportDomainResult:
    path = source_dir / "archive.sqlite3"
    if not path.exists():
        return LegacyImportDomainResult()

    message_rows, message_skipped, messages_exists = _read_legacy_archive_message_rows(path)
    if not messages_exists:
        return LegacyImportDomainResult(skipped=message_skipped)

    media_rows, media_skipped, _ = _read_legacy_archive_media_rows(path)
    imported, duplicate_or_conflict_skipped = _insert_archive_rows(
        db,
        persona_id,
        message_rows,
        media_rows,
    )
    return LegacyImportDomainResult(
        imported=imported,
        skipped=message_skipped + media_skipped + duplicate_or_conflict_skipped,
    )


def _import_persona(
    db: DebataDB,
    source_dir: Path,
    persona_id: str,
) -> LegacyImportDomainResult:
    path = source_dir / "persona.db"
    if not path.exists():
        return LegacyImportDomainResult()

    rows, read_skipped = _read_legacy_persona_rows(path)
    imported, duplicate_or_conflict_skipped = _insert_persona_rows(
        db,
        persona_id,
        rows,
    )
    return LegacyImportDomainResult(
        imported=imported,
        skipped=read_skipped + duplicate_or_conflict_skipped,
    )


def _read_legacy_persona_important_source(path: Path) -> _LegacyImportantMergeSource:
    if not path.exists():
        return _LegacyImportantMergeSource([])

    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            if not _legacy_sqlite_table_exists(conn, "important_memories"):
                return _LegacyImportantMergeSource([])

            source_columns = _legacy_sqlite_table_columns(conn, "important_memories")
            if "memories_json" not in source_columns:
                skipped = _legacy_sqlite_row_count(conn, "important_memories")
                logger.warning(
                    "旧 persona.db important_memories 缺少 memories_json，跳过合并源: %s",
                    path,
                )
                return _LegacyImportantMergeSource([], skipped=skipped)

            order_by = "id ASC" if "id" in source_columns else "rowid ASC"
            rows = conn.execute(
                f"""
                SELECT memories_json
                FROM important_memories
                ORDER BY {order_by}
                """
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(
            "读取旧 persona.db important_memories 失败，跳过重要记忆合并源: %s error=%s",
            path,
            exc,
        )
        return _LegacyImportantMergeSource([])

    items: list[Any] = []
    skipped = 0
    valid = False
    for row in rows:
        raw_json = row["memories_json"]
        if not isinstance(raw_json, str) or not raw_json.strip():
            skipped += 1
            logger.warning("跳过空的旧 persona important_memories memories_json: %s", path)
            continue
        try:
            data = orjson.loads(raw_json)
        except orjson.JSONDecodeError as exc:
            skipped += 1
            logger.warning(
                "跳过损坏的旧 persona important_memories memories_json: %s error=%s",
                path,
                exc,
            )
            continue
        if not isinstance(data, list):
            skipped += 1
            logger.warning("跳过非列表旧 persona important_memories memories_json: %s", path)
            continue
        valid = True
        items.extend(data)
    return _LegacyImportantMergeSource(items, skipped=skipped, valid=valid)


def _merge_important_items(
    persona_items: Sequence[Any],
    important_items: Sequence[Any],
) -> tuple[list[Any], int]:
    merged: list[Any] = list(persona_items)
    persona_ids = {_important_memory_id(item) for item in persona_items}
    skipped = 0

    for item in important_items:
        memory_id = _important_memory_id(item)
        if memory_id in persona_ids:
            skipped += 1
            continue
        merged.append(item)

    return merged, skipped


def _read_legacy_persona_rows(
    path: Path,
) -> tuple[list[tuple[_LegacyPersonaTableSpec, tuple[Any, ...]]], int]:
    rows: list[tuple[_LegacyPersonaTableSpec, tuple[Any, ...]]] = []
    skipped = 0
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            for spec in _LEGACY_PERSONA_TABLE_SPECS:
                table_rows, table_skipped = _read_legacy_persona_table_rows(
                    conn,
                    path,
                    spec,
                )
                rows.extend((spec, row) for row in table_rows)
                skipped += table_skipped
    except sqlite3.Error as exc:
        logger.warning("读取旧 persona.db 失败，跳过 persona 导入: %s error=%s", path, exc)
        return [], skipped
    return rows, skipped


def _read_legacy_persona_table_rows(
    conn: sqlite3.Connection,
    path: Path,
    spec: _LegacyPersonaTableSpec,
) -> tuple[list[tuple[Any, ...]], int]:
    if not _legacy_sqlite_table_exists(conn, spec.source_table):
        logger.warning("旧 persona.db 缺少 %s 表，跳过该表: %s", spec.source_table, path)
        return [], 0

    source_columns = _legacy_sqlite_table_columns(conn, spec.source_table)
    missing_required = sorted(set(spec.required_source_columns) - source_columns)
    if missing_required:
        skipped = _legacy_sqlite_row_count(conn, spec.source_table)
        logger.warning(
            "旧 persona.db %s 缺少必要列，跳过该表: %s missing=%s",
            spec.source_table,
            path,
            ",".join(missing_required),
        )
        return [], skipped

    select_columns = _legacy_persona_select_columns(spec, source_columns)
    if not select_columns:
        skipped = _legacy_sqlite_row_count(conn, spec.source_table)
        logger.warning(
            "旧 persona.db %s 没有可导入列，跳过该表: %s",
            spec.source_table,
            path,
        )
        return [], skipped

    source_rows = conn.execute(
        f"""
        SELECT {_sql_identifier_list(select_columns)}
        FROM {_quote_sql_identifier(spec.source_table)}
        ORDER BY {_legacy_persona_order_by(spec, source_columns)}
        """
    ).fetchall()

    rows: list[tuple[Any, ...]] = []
    skipped = 0
    for index, source_row in enumerate(source_rows, start=1):
        target_row = _legacy_persona_target_row_from_mapping(
            source_row,
            spec,
            f"{path}:{spec.source_table} row {index}",
        )
        if target_row is None:
            skipped += 1
            continue
        rows.append(target_row)
    return rows, skipped


def _legacy_persona_select_columns(
    spec: _LegacyPersonaTableSpec,
    source_columns: set[str],
) -> list[str]:
    columns = [
        column
        for column in spec.target_columns
        if column in source_columns
    ]
    if spec.derive_eat_record_id and "record_json" in source_columns and "record_json" not in columns:
        columns.append("record_json")
    if spec.derive_eat_record_id and "rowid" not in columns:
        columns.append("rowid")
    return columns


def _legacy_persona_order_by(
    spec: _LegacyPersonaTableSpec,
    source_columns: set[str],
) -> str:
    if "id" in source_columns:
        return _quote_sql_identifier("id") + " ASC"
    if "record_id" in source_columns:
        return _quote_sql_identifier("record_id") + " ASC"
    if spec.source_table in {"effects", "todos", "cues", "user_profiles"}:
        key_column = spec.key_columns[0]
        if key_column in source_columns:
            return _quote_sql_identifier(key_column) + " ASC"
    return "rowid ASC"


def _legacy_persona_target_row_from_mapping(
    record: sqlite3.Row,
    spec: _LegacyPersonaTableSpec,
    location: str,
) -> tuple[Any, ...] | None:
    values: list[Any] = []
    row_mapping = {key: record[key] for key in record.keys()}
    for column in spec.target_columns:
        if spec.id_fixed_to_one and column == "id":
            value = 1
        elif spec.derive_eat_record_id and column == "record_id":
            value = _legacy_eat_record_id(row_mapping)
        elif column in row_mapping:
            value = row_mapping[column]
        elif column in spec.optional_target_columns:
            value = None
        else:
            logger.warning("跳过缺少必要字段的旧 persona 行: %s column=%s", location, column)
            return None
        values.append(value)

    row = tuple(values)
    if not _legacy_persona_row_has_required_values(spec, row):
        logger.warning("跳过缺少关键字段的旧 persona 行: %s", location)
        return None
    return row


def _legacy_persona_row_has_required_values(
    spec: _LegacyPersonaTableSpec,
    row: tuple[Any, ...],
) -> bool:
    for column, value in zip(spec.target_columns, row, strict=True):
        if column in spec.optional_target_columns:
            continue
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    for column in spec.key_columns:
        value = row[spec.target_columns.index(column)]
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    if spec.id_must_be_one:
        id_value = _required_int(row[spec.target_columns.index("id")])
        if id_value != 1:
            return False
    return True


def _legacy_eat_record_id(record: dict[str, Any]) -> str | None:
    record_id = _optional_text(record.get("record_id"))
    if record_id:
        return record_id
    data = _json_loads(record.get("record_json"), default={})
    if isinstance(data, dict):
        for key in ("record_id", "eat_id", "id"):
            record_id = _optional_text(data.get(key))
            if record_id:
                return record_id
    fallback = record.get("id")
    if fallback in (None, ""):
        fallback = record.get("rowid")
    fallback_text = _optional_text(fallback)
    return f"eat_{fallback_text}" if fallback_text else None


def _insert_persona_rows(
    db: DebataDB,
    persona_id: str,
    rows: Sequence[tuple[_LegacyPersonaTableSpec, tuple[Any, ...]]],
) -> tuple[int, int]:
    if not rows:
        return 0, 0

    imported = 0
    skipped = 0
    with closing(_connect_for_import(db)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_by_table = _existing_persona_rows_by_table(conn, persona_id)
            for spec, row in rows:
                key = _legacy_persona_row_key(spec, row)
                existing = existing_by_table[spec.target_table].get(key)
                if existing is not None:
                    skipped += 1
                    if not _legacy_persona_rows_equivalent(existing, spec, row):
                        logger.warning(
                            "跳过冲突的旧 persona 行 table=%s key=%s persona_id=%s",
                            spec.source_table,
                            ",".join(str(item) for item in key),
                            persona_id,
                        )
                    continue

                _insert_persona_row(conn, persona_id, spec, row)
                existing_by_table[spec.target_table][key] = row
                imported += 1
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    return imported, skipped


def _existing_persona_rows_by_table(
    conn: sqlite3.Connection,
    persona_id: str,
) -> dict[str, dict[tuple[Any, ...], sqlite3.Row]]:
    existing: dict[str, dict[tuple[Any, ...], sqlite3.Row]] = {}
    for spec in _LEGACY_PERSONA_TABLE_SPECS:
        rows = conn.execute(
            f"""
            SELECT *
            FROM {_quote_sql_identifier(spec.target_table)}
            WHERE persona_id = ?
            """,
            (persona_id,),
        ).fetchall()
        existing[spec.target_table] = {
            tuple(row[column] for column in spec.key_columns): row
            for row in rows
        }
    return existing


def _legacy_persona_row_key(
    spec: _LegacyPersonaTableSpec,
    row: tuple[Any, ...],
) -> tuple[Any, ...]:
    return tuple(row[spec.target_columns.index(column)] for column in spec.key_columns)


def _insert_persona_row(
    conn: sqlite3.Connection,
    persona_id: str,
    spec: _LegacyPersonaTableSpec,
    row: tuple[Any, ...],
) -> None:
    columns = ("persona_id", *spec.target_columns)
    conn.execute(
        f"""
        INSERT INTO {_quote_sql_identifier(spec.target_table)}
            ({_sql_identifier_list(columns)})
        VALUES ({", ".join("?" for _ in columns)})
        """,
        (persona_id, *row),
    )


def _legacy_persona_rows_equivalent(
    existing: sqlite3.Row | tuple[Any, ...],
    spec: _LegacyPersonaTableSpec,
    incoming: tuple[Any, ...],
) -> bool:
    return all(
        _persona_row_value(existing, spec, column) == value
        for column, value in zip(spec.target_columns, incoming, strict=True)
    )


def _persona_row_value(
    row: sqlite3.Row | tuple[Any, ...],
    spec: _LegacyPersonaTableSpec,
    column: str,
) -> Any:
    if isinstance(row, tuple):
        return row[spec.target_columns.index(column)]
    return row[column]


def _read_legacy_archive_message_rows(
    path: Path,
) -> tuple[list[_LegacyArchiveMessageRow], int, bool]:
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            if not _legacy_sqlite_table_exists(conn, "archive_messages"):
                logger.warning("旧 archive.sqlite3 缺少 archive_messages 表，跳过归档导入: %s", path)
                return [], 0, False

            columns = _legacy_sqlite_table_columns(conn, "archive_messages")
            missing_columns = sorted(
                set(_LEGACY_ARCHIVE_MESSAGE_REQUIRED_COLUMNS) - columns
            )
            if missing_columns:
                skipped = _legacy_sqlite_row_count(conn, "archive_messages")
                logger.warning(
                    "旧 archive.sqlite3 archive_messages 缺少必要列，跳过归档导入: "
                    "%s missing=%s",
                    path,
                    ",".join(missing_columns),
                )
                return [], skipped, False

            select_columns = [
                column
                for column in _LEGACY_ARCHIVE_MESSAGE_COLUMNS
                if column in columns
            ]
            source_rows = conn.execute(
                f"""
                SELECT {_sql_identifier_list(select_columns)}
                FROM archive_messages
                ORDER BY rowid ASC
                """
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("读取旧 archive.sqlite3 失败，跳过归档导入: %s error=%s", path, exc)
        return [], 0, False

    rows: list[_LegacyArchiveMessageRow] = []
    skipped = 0
    for index, source_row in enumerate(source_rows, start=1):
        archive_row = _legacy_archive_message_row_from_mapping(
            source_row,
            f"{path}:archive_messages row {index}",
        )
        if archive_row is None:
            skipped += 1
            continue
        rows.append(archive_row)
    return rows, skipped, True


def _read_legacy_archive_media_rows(
    path: Path,
) -> tuple[list[_LegacyArchiveMediaRow], int, bool]:
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            if not _legacy_sqlite_table_exists(conn, "archive_message_media"):
                logger.warning(
                    "旧 archive.sqlite3 缺少 archive_message_media 表，跳过归档媒体导入: %s",
                    path,
                )
                return [], 0, False

            columns = _legacy_sqlite_table_columns(conn, "archive_message_media")
            missing_columns = sorted(set(_LEGACY_ARCHIVE_MEDIA_COLUMNS) - columns)
            if missing_columns:
                skipped = _legacy_sqlite_row_count(conn, "archive_message_media")
                logger.warning(
                    "旧 archive.sqlite3 archive_message_media 缺少必要列，跳过归档媒体: "
                    "%s missing=%s",
                    path,
                    ",".join(missing_columns),
                )
                return [], skipped, True

            source_rows = conn.execute(
                f"""
                SELECT {_sql_identifier_list(_LEGACY_ARCHIVE_MEDIA_COLUMNS)}
                FROM archive_message_media
                ORDER BY id ASC
                """
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning(
            "读取旧 archive.sqlite3 媒体表失败，跳过归档媒体导入: %s error=%s",
            path,
            exc,
        )
        return [], 0, False

    rows: list[_LegacyArchiveMediaRow] = []
    skipped = 0
    for index, source_row in enumerate(source_rows, start=1):
        media_row = _legacy_archive_media_row_from_mapping(
            source_row,
            f"{path}:archive_message_media row {index}",
        )
        if media_row is None:
            skipped += 1
            continue
        rows.append(media_row)
    return rows, skipped, True


def _legacy_archive_message_row_from_mapping(
    record: sqlite3.Row,
    location: str,
) -> _LegacyArchiveMessageRow | None:
    rowid = _positive_int(_mapping_value(record, "rowid"))
    archive_id = _required_archive_id(_mapping_value(record, "archive_id"))
    if rowid is None or archive_id is None:
        logger.warning("跳过缺少关键字段的旧归档消息行: %s", location)
        return None

    return _LegacyArchiveMessageRow(
        rowid=rowid,
        archive_id=archive_id,
        timestamp=_mapping_value(record, "timestamp"),
        timestamp_unix=_mapping_value(record, "timestamp_unix"),
        date_key=_mapping_value(record, "date_key"),
        month_key=_mapping_value(record, "month_key"),
        conversation_id=_mapping_value(record, "conversation_id"),
        conversation_type=_mapping_value(record, "conversation_type"),
        target_id=_mapping_value(record, "target_id"),
        sender_id=_mapping_value(record, "sender_id"),
        sender_name=_mapping_value(record, "sender_name"),
        sender_role=_mapping_value(record, "sender_role"),
        direction=_mapping_value(record, "direction"),
        message_kind=_mapping_value(record, "message_kind"),
        content=_mapping_value(record, "content"),
        content_search=_mapping_value(record, "content_search"),
        original_msg_id=_mapping_value(record, "original_msg_id"),
        reply_to_msg_id=_mapping_value(record, "reply_to_msg_id"),
        metadata_json=_mapping_value(record, "metadata_json"),
        record_json=_mapping_value(record, "record_json"),
        created_at=_mapping_value(record, "created_at"),
    )


def _legacy_archive_media_row_from_mapping(
    record: sqlite3.Row,
    location: str,
) -> _LegacyArchiveMediaRow | None:
    media_id = _positive_int(_mapping_value(record, "id"))
    archive_id = _required_archive_id(_mapping_value(record, "archive_id"))
    if media_id is None or archive_id is None:
        logger.warning("跳过缺少关键字段的旧归档媒体行: %s", location)
        return None

    return _LegacyArchiveMediaRow(
        id=media_id,
        archive_id=archive_id,
        media_type=_mapping_value(record, "media_type"),
        workspace_path=_mapping_value(record, "workspace_path"),
        original_name=_mapping_value(record, "original_name"),
        metadata_json=_mapping_value(record, "metadata_json"),
    )


def _insert_archive_rows(
    db: DebataDB,
    persona_id: str,
    message_rows: Sequence[_LegacyArchiveMessageRow],
    media_rows: Sequence[_LegacyArchiveMediaRow],
) -> tuple[int, int]:
    imported = 0
    skipped = 0
    with closing(_connect_for_import(db)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            messages_by_rowid = _existing_archive_messages_by_rowid(conn, persona_id)
            messages_by_archive_id = _existing_archive_messages_by_archive_id(
                conn,
                persona_id,
            )
            blocked_archive_ids: set[str] = set()
            for row in message_rows:
                existing_by_rowid = messages_by_rowid.get(row.rowid)
                existing_by_archive_id = messages_by_archive_id.get(row.archive_id)
                if (
                    existing_by_rowid is not None
                    and existing_by_archive_id is not None
                    and _archive_message_identity(existing_by_rowid)
                    != _archive_message_identity(existing_by_archive_id)
                ):
                    skipped += 1
                    blocked_archive_ids.add(row.archive_id)
                    logger.warning(
                        "跳过冲突的旧归档消息 rowid=%s archive_id=%s persona_id=%s",
                        row.rowid,
                        row.archive_id,
                        persona_id,
                    )
                    continue

                existing = existing_by_rowid or existing_by_archive_id
                if existing is not None:
                    skipped += 1
                    if not _legacy_archive_messages_equivalent(existing, row):
                        blocked_archive_ids.add(row.archive_id)
                        logger.warning(
                            "跳过冲突的旧归档消息 rowid=%s archive_id=%s persona_id=%s",
                            row.rowid,
                            row.archive_id,
                            persona_id,
                        )
                    continue

                _insert_archive_message_row(conn, persona_id, row)
                messages_by_rowid[row.rowid] = row
                messages_by_archive_id[row.archive_id] = row
                imported += 1

            archive_ids = set(messages_by_archive_id)
            media_by_id = _existing_archive_media_by_id(conn, persona_id)
            for row in media_rows:
                if row.archive_id in blocked_archive_ids:
                    skipped += 1
                    logger.warning(
                        "跳过关联冲突归档消息的旧归档媒体 id=%s archive_id=%s persona_id=%s",
                        row.id,
                        row.archive_id,
                        persona_id,
                    )
                    continue
                if row.archive_id not in archive_ids:
                    skipped += 1
                    logger.warning(
                        "跳过孤儿旧归档媒体 id=%s archive_id=%s persona_id=%s",
                        row.id,
                        row.archive_id,
                        persona_id,
                    )
                    continue

                existing = media_by_id.get(row.id)
                if existing is not None:
                    skipped += 1
                    if not _legacy_archive_media_equivalent(existing, row):
                        logger.warning(
                            "跳过冲突的旧归档媒体 id=%s archive_id=%s persona_id=%s",
                            row.id,
                            row.archive_id,
                            persona_id,
                        )
                    continue

                _insert_archive_media_row(conn, persona_id, row)
                media_by_id[row.id] = row
                imported += 1
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    return imported, skipped


def _existing_archive_messages_by_rowid(
    conn: sqlite3.Connection,
    persona_id: str,
) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM archive_messages
        WHERE persona_id = ?
        """,
        (persona_id,),
    ).fetchall()
    return {int(row["rowid"]): row for row in rows}


def _existing_archive_messages_by_archive_id(
    conn: sqlite3.Connection,
    persona_id: str,
) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM archive_messages
        WHERE persona_id = ?
        """,
        (persona_id,),
    ).fetchall()
    return {str(row["archive_id"]): row for row in rows}


def _existing_archive_media_by_id(
    conn: sqlite3.Connection,
    persona_id: str,
) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM archive_message_media
        WHERE persona_id = ?
        """,
        (persona_id,),
    ).fetchall()
    return {int(row["id"]): row for row in rows}


def _insert_archive_message_row(
    conn: sqlite3.Connection,
    persona_id: str,
    row: _LegacyArchiveMessageRow,
) -> None:
    conn.execute(
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
        (
            persona_id,
            row.rowid,
            row.archive_id,
            row.timestamp,
            row.timestamp_unix,
            row.date_key,
            row.month_key,
            row.conversation_id,
            row.conversation_type,
            row.target_id,
            row.sender_id,
            row.sender_name,
            row.sender_role,
            row.direction,
            row.message_kind,
            row.content,
            row.content_search,
            row.original_msg_id,
            row.reply_to_msg_id,
            row.metadata_json,
            row.record_json,
            row.created_at,
        ),
    )


def _insert_archive_media_row(
    conn: sqlite3.Connection,
    persona_id: str,
    row: _LegacyArchiveMediaRow,
) -> None:
    conn.execute(
        """
        INSERT INTO archive_message_media (
            persona_id, id, archive_id, media_type, workspace_path,
            original_name, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            persona_id,
            row.id,
            row.archive_id,
            row.media_type,
            row.workspace_path,
            row.original_name,
            row.metadata_json,
        ),
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
    db: DebataDB,
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


_LEGACY_ARCHIVE_MESSAGE_COLUMNS = (
    "rowid",
    "archive_id",
    "timestamp",
    "timestamp_unix",
    "date_key",
    "month_key",
    "conversation_id",
    "conversation_type",
    "target_id",
    "sender_id",
    "sender_name",
    "sender_role",
    "direction",
    "message_kind",
    "content",
    "content_search",
    "original_msg_id",
    "reply_to_msg_id",
    "metadata_json",
    "record_json",
    "created_at",
)
_LEGACY_ARCHIVE_MESSAGE_REQUIRED_COLUMNS = tuple(
    column for column in _LEGACY_ARCHIVE_MESSAGE_COLUMNS if column != "record_json"
)
_LEGACY_ARCHIVE_MEDIA_COLUMNS = (
    "id",
    "archive_id",
    "media_type",
    "workspace_path",
    "original_name",
    "metadata_json",
)


_LEGACY_PERSONA_TABLE_SPECS = (
    _LegacyPersonaTableSpec(
        source_table="schema_version",
        target_table="persona_schema_version_legacy",
        target_columns=("id", "version", "updated_at"),
        key_columns=("id",),
        required_source_columns=("version", "updated_at"),
        id_fixed_to_one=True,
    ),
    _LegacyPersonaTableSpec(
        source_table="persona_state",
        target_table="persona_state",
        target_columns=("id", "state_json", "updated_at"),
        key_columns=("id",),
        required_source_columns=("id", "state_json", "updated_at"),
        id_must_be_one=True,
    ),
    _LegacyPersonaTableSpec(
        source_table="persona_state_log",
        target_table="persona_state_log",
        target_columns=("id", "state_json", "created_at"),
        key_columns=("id",),
        required_source_columns=("id", "state_json", "created_at"),
    ),
    _LegacyPersonaTableSpec(
        source_table="persona_update_audits",
        target_table="persona_update_audits",
        target_columns=(
            "id",
            "audit_json",
            "trigger",
            "conversation_id",
            "user_id",
            "created_at",
        ),
        key_columns=("id",),
        optional_target_columns=("trigger", "conversation_id", "user_id"),
        required_source_columns=("id", "audit_json", "created_at"),
    ),
    _LegacyPersonaTableSpec(
        source_table="effects",
        target_table="persona_effects",
        target_columns=(
            "effect_id",
            "effect_json",
            "expires_at",
            "active",
            "created_at",
            "updated_at",
        ),
        key_columns=("effect_id",),
        optional_target_columns=("expires_at",),
        required_source_columns=(
            "effect_id",
            "effect_json",
            "active",
            "created_at",
            "updated_at",
        ),
    ),
    _LegacyPersonaTableSpec(
        source_table="todos",
        target_table="persona_todos",
        target_columns=(
            "todo_id",
            "todo_json",
            "completed",
            "expires_at",
            "created_at",
            "updated_at",
        ),
        key_columns=("todo_id",),
        optional_target_columns=("expires_at",),
        required_source_columns=(
            "todo_id",
            "todo_json",
            "completed",
            "created_at",
            "updated_at",
        ),
    ),
    _LegacyPersonaTableSpec(
        source_table="cues",
        target_table="persona_cues",
        target_columns=(
            "cue_id",
            "cue_json",
            "expires_at",
            "active",
            "created_at",
            "updated_at",
        ),
        key_columns=("cue_id",),
        optional_target_columns=("expires_at",),
        required_source_columns=(
            "cue_id",
            "cue_json",
            "active",
            "created_at",
            "updated_at",
        ),
    ),
    _LegacyPersonaTableSpec(
        source_table="inner_monologues",
        target_table="persona_inner_monologues",
        target_columns=("id", "monologue_json", "created_at"),
        key_columns=("id",),
        required_source_columns=("id", "monologue_json", "created_at"),
    ),
    _LegacyPersonaTableSpec(
        source_table="user_profiles",
        target_table="persona_user_profiles",
        target_columns=("user_id", "profile_json", "created_at", "updated_at"),
        key_columns=("user_id",),
        required_source_columns=("user_id", "profile_json", "created_at", "updated_at"),
    ),
    _LegacyPersonaTableSpec(
        source_table="important_memories",
        target_table="persona_important_state_legacy",
        target_columns=("id", "memories_json", "updated_at"),
        key_columns=("id",),
        required_source_columns=("id", "memories_json", "updated_at"),
        id_must_be_one=True,
    ),
    _LegacyPersonaTableSpec(
        source_table="daily_trajectories",
        target_table="persona_daily_trajectories",
        target_columns=("id", "trajectory_json", "created_at"),
        key_columns=("id",),
        required_source_columns=("id", "trajectory_json", "created_at"),
    ),
    _LegacyPersonaTableSpec(
        source_table="persona_arc",
        target_table="persona_arc",
        target_columns=("id", "event_json", "created_at"),
        key_columns=("id",),
        required_source_columns=("id", "event_json", "created_at"),
    ),
    _LegacyPersonaTableSpec(
        source_table="sleep_records",
        target_table="persona_sleep_records",
        target_columns=(
            "record_id",
            "record_json",
            "started_at",
            "ended_at",
            "created_at",
            "updated_at",
        ),
        key_columns=("record_id",),
        optional_target_columns=("started_at", "ended_at"),
        required_source_columns=("record_id", "record_json", "created_at", "updated_at"),
    ),
    _LegacyPersonaTableSpec(
        source_table="eat_records",
        target_table="persona_eat_records",
        target_columns=(
            "id",
            "record_id",
            "record_json",
            "ended_at",
            "status",
            "created_at",
        ),
        key_columns=("id",),
        optional_target_columns=("record_id", "ended_at", "status"),
        required_source_columns=("id", "record_json", "created_at"),
        derive_eat_record_id=True,
    ),
)


def _legacy_archive_messages_equivalent(
    existing: sqlite3.Row | _LegacyArchiveMessageRow,
    incoming: _LegacyArchiveMessageRow,
) -> bool:
    return all(
        _archive_message_value(existing, column) == getattr(incoming, column)
        for column in _LEGACY_ARCHIVE_MESSAGE_COLUMNS
    )


def _legacy_archive_media_equivalent(
    existing: sqlite3.Row | _LegacyArchiveMediaRow,
    incoming: _LegacyArchiveMediaRow,
) -> bool:
    return all(
        _archive_media_value(existing, column) == getattr(incoming, column)
        for column in _LEGACY_ARCHIVE_MEDIA_COLUMNS
    )


def _archive_message_identity(row: sqlite3.Row | _LegacyArchiveMessageRow) -> tuple[int, str]:
    return (
        int(_archive_message_value(row, "rowid")),
        str(_archive_message_value(row, "archive_id")),
    )


def _insert_usage_records(
    db: DebataDB,
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


def _connect_for_import(db: DebataDB) -> sqlite3.Connection:
    conn = sqlite3.connect(db.path, timeout=db.busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(db.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _domain_row_count(
    db: DebataDB,
    table: str,
    persona_id: str | None,
) -> int:
    if table not in _PERSONA_SCOPED_IMPORT_TABLES:
        raise ValueError(f"unsupported import domain table: {table}")
    with closing(_connect_for_import(db)) as conn:
        if persona_id is None:
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {_quote_sql_identifier(table)}
                WHERE persona_id IS NULL
                """
            ).fetchone()
        else:
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {_quote_sql_identifier(table)}
                WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
    return int(row[0])


def _persona_domain_row_count(db: DebataDB, persona_id: str) -> int:
    total = 0
    with closing(_connect_for_import(db)) as conn:
        for table in _PERSONA_DOMAIN_TABLES:
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {_quote_sql_identifier(table)}
                WHERE persona_id = ?
                """,
                (persona_id,),
            ).fetchone()
            total += int(row[0])
    return total


_PERSONA_SCOPED_IMPORT_TABLES = frozenset(
    {
        "history_records",
        "important_memories",
        "rolling_summary",
        "usage_records",
        "event_log",
        "archive_messages",
    }
)


_PERSONA_DOMAIN_TABLES = (
    "persona_schema_version_legacy",
    "persona_state",
    "persona_state_log",
    "persona_update_audits",
    "persona_effects",
    "persona_todos",
    "persona_cues",
    "persona_inner_monologues",
    "persona_user_profiles",
    "persona_important_state_legacy",
    "persona_daily_trajectories",
    "persona_arc",
    "persona_sleep_records",
    "persona_eat_records",
)


def _legacy_sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone()
    return row is not None


def _legacy_sqlite_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({_quote_sql_identifier(table)})").fetchall()
    }


def _legacy_sqlite_row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {_quote_sql_identifier(table)}"
        ).fetchone()[0]
    )


def _sql_identifier_list(columns: Sequence[str]) -> str:
    return ", ".join(_quote_sql_identifier(column) for column in columns)


def _quote_sql_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _mapping_value(record: sqlite3.Row | dict[str, Any], key: str) -> Any:
    if isinstance(record, dict):
        return record.get(key)
    return record[key] if key in record.keys() else None


def _event_value(record: sqlite3.Row | _LegacyEventRow, key: str) -> Any:
    if isinstance(record, _LegacyEventRow):
        return getattr(record, key)
    return record[key]


def _archive_message_value(
    record: sqlite3.Row | _LegacyArchiveMessageRow,
    key: str,
) -> Any:
    if isinstance(record, _LegacyArchiveMessageRow):
        return getattr(record, key)
    return record[key]


def _archive_media_value(
    record: sqlite3.Row | _LegacyArchiveMediaRow,
    key: str,
) -> Any:
    if isinstance(record, _LegacyArchiveMediaRow):
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


def _important_memory_id(item: Any) -> str:
    if isinstance(item, dict):
        item_id = _optional_text(item.get("id"))
        if item_id:
            return item_id
    digest = hashlib.sha256(_canonical_json_data(item).encode("utf-8")).hexdigest()[:32]
    return f"fallback:{digest}"


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


def _json_loads(data: Any, *, default: Any) -> Any:
    if not isinstance(data, str) or not data.strip():
        return default
    try:
        return orjson.loads(data)
    except orjson.JSONDecodeError:
        return default


def _record_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _required_text(value: Any) -> str | None:
    text = _optional_text(value)
    return text


def _required_archive_id(value: Any) -> str | None:
    return _optional_text(value)


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
    "import_legacy_memory_files_async",
]
