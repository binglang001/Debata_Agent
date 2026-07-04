from __future__ import annotations

import sqlite3

import pytest

from memory.debata_db import (
    DEBATA_DB_SCHEMA_VERSION,
    DebataDB,
    DebataDBVersionError,
    backup_existing_database,
)

KEY_TABLES = {
    "schema_migrations",
    "history_records",
    "important_memories",
    "rolling_summary",
    "event_log",
    "event_projection_state",
    "archive_messages",
    "archive_message_media",
    "usage_records",
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
}


def test_memory_package_exports_debata_db():
    from memory import DebataDB as PackageDebataDB

    assert PackageDebataDB is DebataDB


def test_debata_db_load_creates_v1_schema_and_versions(tmp_path):
    db_path = tmp_path / "db" / "debata.db"
    db = DebataDB(db_path)

    try:
        db.load()
        version = db.schema_version()
        conn = db.connect()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        migration_row = conn.execute(
            "SELECT version, migration_id FROM schema_migrations"
        ).fetchone()
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        rolling_summary_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(rolling_summary)")
        }
    finally:
        db.close()

    assert KEY_TABLES <= tables
    assert {
        "persona_id",
        "summary_text",
        "archived_until_json",
        "active_start_index",
        "summary_json",
        "updated_at",
    } <= rolling_summary_columns
    assert user_version == DEBATA_DB_SCHEMA_VERSION
    assert migration_row["version"] == DEBATA_DB_SCHEMA_VERSION
    assert migration_row["migration_id"] == "v1_initial_schema"
    assert version.user_version == DEBATA_DB_SCHEMA_VERSION
    assert version.migration_version == DEBATA_DB_SCHEMA_VERSION
    assert foreign_keys == 1
    assert busy_timeout == 30_000


def test_debata_db_load_is_idempotent(tmp_path):
    db_path = tmp_path / "db" / "debata.db"

    first = DebataDB(db_path)
    try:
        first.load()
    finally:
        first.close()

    second = DebataDB(db_path)
    try:
        second.load()
        second.load()
        conn = second.connect()
        migration_count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        migration_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        second.close()

    assert migration_count == 1
    assert migration_version == DEBATA_DB_SCHEMA_VERSION
    assert user_version == DEBATA_DB_SCHEMA_VERSION


def test_debata_db_load_rejects_future_user_version_without_downgrade(tmp_path):
    db_path = tmp_path / "db" / "debata.db"
    db = DebataDB(db_path)
    try:
        db.load()
    finally:
        db.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 2")

    db = DebataDB(db_path)
    try:
        with pytest.raises(DebataDBVersionError, match="user_version=2"):
            db.load()
    finally:
        db.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_debata_db_load_rejects_future_internal_migration_without_downgrade(tmp_path):
    db_path = tmp_path / "db" / "debata.db"
    db = DebataDB(db_path)
    try:
        db.load()
        db.connect().execute(
            """
            INSERT INTO schema_migrations(version, migration_id, applied_at)
            VALUES (2, 'v2_future_schema', '2026-06-18T00:00:00Z')
            """
        )
        db.connect().commit()
    finally:
        db.close()

    db = DebataDB(db_path)
    try:
        with pytest.raises(DebataDBVersionError, match="migration_version=2"):
            db.load()
    finally:
        db.close()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DEBATA_DB_SCHEMA_VERSION
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2


def test_backup_missing_database_returns_none_without_creating_backups_dir(tmp_path):
    db_path = tmp_path / "db" / "debata.db"

    backup_path = backup_existing_database(db_path)

    assert backup_path is None
    assert not (tmp_path / "db" / "backups").exists()


def test_backup_existing_database_is_sqlite_copy_and_does_not_overwrite(tmp_path):
    db_path = tmp_path / "db" / "debata.db"
    db = DebataDB(db_path)
    try:
        db.load()
        db.connect().execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, role, content_hash, content_length, record_json
            )
            VALUES ('yuexi', 1, 'user', 'hash-1', 4, '{"role":"user","content":"test"}')
            """
        )
        db.connect().commit()
    finally:
        db.close()

    first_backup = backup_existing_database(
        db_path,
        timestamp="20260618T120000Z",
    )
    assert first_backup is not None
    assert first_backup.name == "debata-v1-20260618T120000Z.db"
    with sqlite3.connect(first_backup) as backup_conn:
        assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == DEBATA_DB_SCHEMA_VERSION
        assert backup_conn.execute("SELECT COUNT(*) FROM history_records").fetchone()[0] == 1
        backup_conn.execute("CREATE TABLE backup_marker(value TEXT NOT NULL)")
        backup_conn.execute("INSERT INTO backup_marker(value) VALUES ('preserve-first')")

    second_backup = backup_existing_database(
        db_path,
        timestamp="20260618T120000Z",
    )
    assert second_backup is not None
    assert second_backup.name == "debata-v1-20260618T120000Z-1.db"

    with sqlite3.connect(first_backup) as first_conn:
        marker = first_conn.execute("SELECT value FROM backup_marker").fetchone()[0]
    with sqlite3.connect(second_backup) as second_conn:
        second_user_version = second_conn.execute("PRAGMA user_version").fetchone()[0]
        second_tables = {
            row[0]
            for row in second_conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert marker == "preserve-first"
    assert second_user_version == DEBATA_DB_SCHEMA_VERSION
    assert "backup_marker" not in second_tables


def test_backup_existing_database_skips_preexisting_target_file(tmp_path):
    db_path = tmp_path / "db" / "debata.db"
    db = DebataDB(db_path)
    try:
        db.load()
    finally:
        db.close()
    backups_dir = db_path.parent / "backups"
    backups_dir.mkdir(parents=True)
    existing_backup = backups_dir / "debata-v1-20260618T120000Z.db"
    existing_backup.write_text("preexisting backup", encoding="utf-8")

    backup_path = backup_existing_database(db_path, timestamp="20260618T120000Z")

    assert backup_path is not None
    assert backup_path.name == "debata-v1-20260618T120000Z-1.db"
    assert existing_backup.read_text(encoding="utf-8") == "preexisting backup"
    with sqlite3.connect(backup_path) as backup_conn:
        assert backup_conn.execute("PRAGMA user_version").fetchone()[0] == DEBATA_DB_SCHEMA_VERSION


def test_history_records_unique_per_persona_index(tmp_path):
    db = DebataDB(tmp_path / "debata.db")
    try:
        db.load()
        conn = db.connect()
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, conversation_id, role,
                content_hash, content_length, record_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("yuexi", 7, "private:u1", "user", "hash-yuexi-7", 2, "{}"),
        )
        conn.execute(
            """
            INSERT INTO history_records(
                persona_id, history_index, conversation_id, role,
                content_hash, content_length, record_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("jiu", 7, "private:u1", "user", "hash-jiu-7", 2, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO history_records(
                    persona_id, history_index, conversation_id, role,
                    content_hash, content_length, record_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("yuexi", 7, "private:u2", "assistant", "hash-dup", 3, "{}"),
            )
    finally:
        db.close()
