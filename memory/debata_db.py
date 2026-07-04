"""debata.db 基础 schema、版本与备份契约。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DEBATA_DB_SCHEMA_VERSION = 1
DEBATA_DB_INITIAL_MIGRATION_ID = "v1_initial_schema"
DEFAULT_BUSY_TIMEOUT_MS = 30_000


@dataclass(frozen=True, slots=True)
class DebataDBSchemaVersion:
    """SQLite PRAGMA 与内部迁移表的版本读数。"""

    user_version: int
    migration_version: int | None


class DebataDBVersionError(RuntimeError):
    """当前代码不支持打开更高版本的 debata.db。"""


class DebataDB:
    """debata.db 的最小连接、schema 初始化与版本读取入口。"""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._conn: sqlite3.Connection | None = None

    def load(self) -> DebataDB:
        """打开数据库并幂等初始化 v1 schema。"""

        conn = self.connect()
        self.ensure_schema(conn)
        return self

    def close(self) -> None:
        """关闭当前持有的连接。"""

        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def connect(self) -> sqlite3.Connection:
        """返回带项目约定 PRAGMA 的 sqlite3 连接。"""

        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
            conn.row_factory = sqlite3.Row
            _configure_connection(conn, self.busy_timeout_ms)
            self._conn = conn
        return self._conn

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        """创建 debata.db v1 空表契约，并写入统一版本记录。"""

        target = conn if conn is not None else self.connect()
        _raise_if_future_schema(self.schema_version(target))
        with target:
            for statement in _V1_SCHEMA_STATEMENTS:
                target.execute(statement)
            target.execute(f"PRAGMA user_version = {DEBATA_DB_SCHEMA_VERSION}")
            target.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, migration_id, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    DEBATA_DB_SCHEMA_VERSION,
                    DEBATA_DB_INITIAL_MIGRATION_ID,
                    _utc_timestamp(),
                ),
            )

    def schema_version(self, conn: sqlite3.Connection | None = None) -> DebataDBSchemaVersion:
        """读取 PRAGMA user_version 与内部迁移表中的最高版本。"""

        target = conn if conn is not None else self.connect()
        user_version = int(target.execute("PRAGMA user_version").fetchone()[0])
        try:
            row = target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.OperationalError:
            migration_version = None
        else:
            migration_version = None if row is None or row[0] is None else int(row[0])
        return DebataDBSchemaVersion(
            user_version=user_version,
            migration_version=migration_version,
        )


def backup_existing_database(
    db_path: str | Path,
    *,
    schema_version: int = DEBATA_DB_SCHEMA_VERSION,
    timestamp: str | datetime | None = None,
) -> Path | None:
    """把已存在的 debata.db 备份到同级 backups/；源库不存在时不创建备份。"""

    source = Path(db_path)
    if not source.exists():
        return None

    backups_dir = source.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = _backup_timestamp(timestamp)
    target, target_stat = _reserve_unique_backup_path(
        backups_dir,
        f"{source.stem}-v{int(schema_version)}-{stamp}",
        source.suffix or ".db",
    )

    source_conn: sqlite3.Connection | None = None
    try:
        source_uri = f"file:{quote(source.resolve().as_posix(), safe='/:')}?mode=ro"
        source_conn = sqlite3.connect(source_uri, uri=True)
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    except Exception:
        _unlink_reserved_backup(target, target_stat)
        raise
    finally:
        if source_conn is not None:
            source_conn.close()
    return target


def _configure_connection(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _backup_timestamp(timestamp: str | datetime | None) -> str:
    if timestamp is None:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if isinstance(timestamp, datetime):
        value = timestamp
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return timestamp


def _raise_if_future_schema(version: DebataDBSchemaVersion) -> None:
    future_parts = []
    if version.user_version > DEBATA_DB_SCHEMA_VERSION:
        future_parts.append(f"user_version={version.user_version}")
    if (
        version.migration_version is not None
        and version.migration_version > DEBATA_DB_SCHEMA_VERSION
    ):
        future_parts.append(f"migration_version={version.migration_version}")
    if future_parts:
        details = ", ".join(future_parts)
        raise DebataDBVersionError(
            f"debata.db schema version is newer than supported: {details}; "
            f"supported_version={DEBATA_DB_SCHEMA_VERSION}"
        )


def _reserve_unique_backup_path(
    directory: Path,
    stem: str,
    suffix: str,
) -> tuple[Path, os.stat_result]:
    counter = 1
    candidate = directory / f"{stem}{suffix}"
    while True:
        try:
            with candidate.open("xb") as reserved:
                reserved_stat = os.fstat(reserved.fileno())
            return candidate, reserved_stat
        except FileExistsError:
            pass
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1


def _unlink_reserved_backup(path: Path, reserved_stat: os.stat_result) -> None:
    try:
        current_stat = path.stat()
    except FileNotFoundError:
        return
    if os.path.samestat(current_stat, reserved_stat):
        path.unlink()


_V1_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        migration_id TEXT UNIQUE NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS history_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT NOT NULL,
        history_index INTEGER NOT NULL,
        conversation_id TEXT,
        role TEXT,
        content_hash TEXT,
        content_length INTEGER NOT NULL DEFAULT 0,
        record_json TEXT NOT NULL,
        timestamp TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(persona_id, history_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_persona_index ON history_records(persona_id, history_index)",
    "CREATE INDEX IF NOT EXISTS idx_history_conversation ON history_records(persona_id, conversation_id, history_index)",
    "CREATE INDEX IF NOT EXISTS idx_history_role ON history_records(persona_id, role)",
    "CREATE INDEX IF NOT EXISTS idx_history_content_hash ON history_records(content_hash)",
    """
    CREATE TABLE IF NOT EXISTS important_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        timestamp TEXT,
        scope TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        content TEXT,
        item_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at TEXT NOT NULL,
        UNIQUE(persona_id, memory_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_important_persona_time ON important_memories(persona_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_important_persona_scope ON important_memories(persona_id, scope)",
    "CREATE INDEX IF NOT EXISTS idx_important_pinned ON important_memories(persona_id, pinned)",
    """
    CREATE TABLE IF NOT EXISTS rolling_summary (
        persona_id TEXT PRIMARY KEY,
        summary_text TEXT NOT NULL DEFAULT '',
        archived_until_json TEXT NOT NULL DEFAULT '{}',
        active_start_index INTEGER,
        summary_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_log (
        persona_id TEXT NOT NULL,
        event_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_uuid TEXT NOT NULL,
        conversation_id TEXT,
        session_id TEXT,
        turn_id TEXT,
        source TEXT,
        external_id TEXT,
        tool_call_id TEXT,
        parent_event_id INTEGER,
        idempotency_key TEXT,
        timestamp_unix REAL NOT NULL,
        created_at_unix REAL NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        PRIMARY KEY(persona_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_projection_state (
        persona_id TEXT NOT NULL,
        name TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(persona_id, name)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_log_persona_idempotency ON event_log(persona_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_event_log_conversation_event ON event_log(persona_id, conversation_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_type_event ON event_log(persona_id, event_type, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_session_event ON event_log(persona_id, session_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_external ON event_log(persona_id, source, external_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_log_parent ON event_log(persona_id, parent_event_id)",
    """
    CREATE TABLE IF NOT EXISTS archive_messages (
        persona_id TEXT NOT NULL,
        rowid INTEGER NOT NULL,
        archive_id TEXT NOT NULL,
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
        created_at TEXT,
        PRIMARY KEY(persona_id, rowid),
        UNIQUE(persona_id, archive_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS archive_message_media (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        archive_id TEXT NOT NULL,
        media_type TEXT,
        workspace_path TEXT,
        original_name TEXT,
        metadata_json TEXT,
        PRIMARY KEY(persona_id, id),
        FOREIGN KEY(persona_id, archive_id)
            REFERENCES archive_messages(persona_id, archive_id)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_archive_time ON archive_messages(persona_id, timestamp_unix)",
    "CREATE INDEX IF NOT EXISTS idx_archive_conversation_time ON archive_messages(persona_id, conversation_id, timestamp_unix)",
    "CREATE INDEX IF NOT EXISTS idx_archive_sender_time ON archive_messages(persona_id, sender_id, timestamp_unix)",
    "CREATE INDEX IF NOT EXISTS idx_archive_original_msg ON archive_messages(persona_id, original_msg_id)",
    "CREATE INDEX IF NOT EXISTS idx_archive_date ON archive_messages(persona_id, date_key)",
    "CREATE INDEX IF NOT EXISTS idx_archive_record_json ON archive_messages(record_json)",
    "CREATE INDEX IF NOT EXISTS idx_archive_media_archive ON archive_message_media(persona_id, archive_id)",
    """
    CREATE TABLE IF NOT EXISTS usage_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT,
        ts REAL,
        provider TEXT,
        model TEXT,
        agent TEXT,
        operation TEXT,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        record_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_records(ts)",
    "CREATE INDEX IF NOT EXISTS idx_usage_provider_model ON usage_records(provider, model)",
    "CREATE INDEX IF NOT EXISTS idx_usage_agent_operation ON usage_records(agent, operation)",
    "CREATE INDEX IF NOT EXISTS idx_usage_persona_ts ON usage_records(persona_id, ts)",
    """
    CREATE TABLE IF NOT EXISTS persona_schema_version_legacy (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL CHECK (id = 1),
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_state (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL CHECK (id = 1),
        state_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_state_log (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_update_audits (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        audit_json TEXT NOT NULL,
        "trigger" TEXT,
        conversation_id TEXT,
        user_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_effects (
        persona_id TEXT NOT NULL,
        effect_id TEXT NOT NULL,
        effect_json TEXT NOT NULL,
        expires_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, effect_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_todos (
        persona_id TEXT NOT NULL,
        todo_id TEXT NOT NULL,
        todo_json TEXT NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, todo_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_cues (
        persona_id TEXT NOT NULL,
        cue_id TEXT NOT NULL,
        cue_json TEXT NOT NULL,
        expires_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, cue_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_inner_monologues (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        monologue_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_user_profiles (
        persona_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_important_state_legacy (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL CHECK (id = 1),
        memories_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_daily_trajectories (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        trajectory_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_arc (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_sleep_records (
        persona_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_json TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, record_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_eat_records (
        persona_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        record_id TEXT,
        record_json TEXT NOT NULL,
        ended_at TEXT,
        status TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY(persona_id, id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_persona_effects_expires ON persona_effects(persona_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_persona_todos_completed ON persona_todos(persona_id, completed)",
    "CREATE INDEX IF NOT EXISTS idx_persona_cues_expires ON persona_cues(persona_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_persona_monologues_recent ON persona_inner_monologues(persona_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_trajectories_recent ON persona_daily_trajectories(persona_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_update_audits_conversation ON persona_update_audits(persona_id, conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_update_audits_user ON persona_update_audits(persona_id, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_persona_sleep_started ON persona_sleep_records(persona_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_persona_eat_record_id ON persona_eat_records(persona_id, record_id)",
)


__all__ = [
    "DEBATA_DB_SCHEMA_VERSION",
    "DebataDB",
    "DebataDBSchemaVersion",
    "DebataDBVersionError",
    "backup_existing_database",
]
