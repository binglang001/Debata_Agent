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
    finally:
        if should_close_db:
            target_db.close()

    return LegacyMemoryImportResult(
        backup_path=backup_path,
        history=history_result,
        important=important_result,
        rolling_summary=rolling_result,
        usage=usage_result,
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


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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
