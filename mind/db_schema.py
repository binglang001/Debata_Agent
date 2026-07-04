"""PersonaDB schema 初始化逻辑。"""

from __future__ import annotations

import sqlite3

from mind import db_records as _db_records

SCHEMA_VERSION = 2

_json_dumps = _db_records._json_dumps
_json_loads = _db_records._json_loads
_now_text = _db_records._now_text
_optional_text = _db_records._optional_text


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
        CREATE TABLE IF NOT EXISTS persona_update_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            audit_json TEXT NOT NULL,
            "trigger" TEXT,
            conversation_id TEXT,
            user_id TEXT,
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_update_audits_conversation ON persona_update_audits(conversation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_update_audits_user ON persona_update_audits(user_id)")
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
