"""PersonaDB SQLite 存储测试。"""

from __future__ import annotations

import sqlite3

import pytest

from mind import Cue, Effect, PersonaState, Todo, UserProfile
from mind.db import SCHEMA_VERSION, PersonaDB


@pytest.mark.asyncio
async def test_persona_db_state_default_roundtrip_and_schema(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    expected_tables = {
        "persona_state",
        "persona_state_log",
        "persona_update_audits",
        "effects",
        "todos",
        "cues",
        "inner_monologues",
        "user_profiles",
        "important_memories",
        "daily_trajectories",
        "persona_arc",
        "sleep_records",
        "eat_records",
        "schema_version",
    }
    with sqlite3.connect(tmp_path / "persona.sqlite") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        audit_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(persona_update_audits)").fetchall()
        }
        schema_version = conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]
    assert expected_tables <= tables
    assert {
        "id",
        "audit_json",
        "trigger",
        "conversation_id",
        "user_id",
        "created_at",
    } <= audit_columns
    assert schema_version == SCHEMA_VERSION

    default_state = await db.get_state()
    assert isinstance(default_state, PersonaState)
    assert default_state == PersonaState()
    assert await db.get_state(default=None) is None

    state = PersonaState(
        energy=72.5,
        satiety=64.0,
        mood=68.0,
        social_need=42.0,
        current_action="thinking",
        action_until=1_767_225_900.0,
        last_tick_at=1_767_225_600.0,
        latest_monologue="今天要记得休息",
    )
    await db.save_state(state)
    loaded = await db.get_state()
    assert isinstance(loaded, PersonaState)
    assert loaded == state

    log_id = await db.append_state_log({"mood": "calm", "reason": "test"})
    assert log_id == 1


@pytest.mark.asyncio
async def test_persona_db_update_audits_append_limit_and_filters(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    first_id = await db.append_update_audit(
        {
            "trigger": "message",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"mood": 10},
            "applied": {"mood": 10},
        }
    )
    second_id = await db.append_update_audit(
        {
            "trigger": "message",
            "conversation_id": "private:u2",
            "user_id": "u2",
            "update": {"mood": 20},
            "applied": {"mood": 20},
        }
    )
    third_id = await db.append_update_audit(
        {
            "trigger": "tick",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"energy": 30},
            "applied": {"energy": 30},
        }
    )

    assert (first_id, second_id, third_id) == (1, 2, 3)
    assert await db.recent_update_audits(limit=2) == [
        {
            "trigger": "tick",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"energy": 30},
            "applied": {"energy": 30},
        },
        {
            "trigger": "message",
            "conversation_id": "private:u2",
            "user_id": "u2",
            "update": {"mood": 20},
            "applied": {"mood": 20},
        },
    ]
    assert await db.recent_update_audits(conversation_id="private:u1") == [
        {
            "trigger": "tick",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"energy": 30},
            "applied": {"energy": 30},
        },
        {
            "trigger": "message",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"mood": 10},
            "applied": {"mood": 10},
        },
    ]
    assert await db.recent_update_audits(user_id="u2") == [
        {
            "trigger": "message",
            "conversation_id": "private:u2",
            "user_id": "u2",
            "update": {"mood": 20},
            "applied": {"mood": 20},
        }
    ]
    assert await db.recent_update_audits(
        conversation_id="private:u1",
        user_id="u1",
        limit=1,
    ) == [
        {
            "trigger": "tick",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"energy": 30},
            "applied": {"energy": 30},
        }
    ]


@pytest.mark.asyncio
async def test_persona_db_effect_todo_cue_profile_crud_and_expire(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    active_effect = Effect(
        id="effect_1",
        name="buff",
        effect_type="mood",
        intensity=1.5,
        prompt_hint="保持温和",
        source_detail="unit-test",
        created_at=10.0,
        expires_at=4_102_444_800.0,
    )
    expired_effect = Effect(
        id="effect_2",
        name="old",
        effect_type="mood",
        intensity=0.2,
        prompt_hint="",
        source_detail="unit-test",
        created_at=1.0,
        expires_at=1.0,
    )
    active_effect_id = await db.add_effect(active_effect)
    await db.add_effect(expired_effect)
    active_effects = await db.get_active_effects(now=1_767_225_600.0)
    assert active_effects == [active_effect]
    assert isinstance(active_effects[0], Effect)
    assert await db.expire_effects(now=1_767_225_600.0) == 1
    assert await db.remove_effects([active_effect_id]) == 1
    assert await db.get_active_effects(now=1_767_225_600.0) == []

    todo = Todo(
        id="todo_1",
        title="写测试",
        reason="覆盖数据库契约",
        priority=2,
        scope="persona",
        created_at=10.0,
        expires_at=4_102_444_800.0,
    )
    await db.upsert_todo(todo)
    await db.upsert_todo(
        {
            "id": "todo_open",
            "title": "仍未完成",
            "reason": "open 状态不应被视为完成",
            "priority": 1,
            "scope": "persona",
            "created_at": 10.5,
            "status": "open",
        }
    )
    await db.upsert_todo(
        {
            "id": "todo_2",
            "title": "已完成",
            "reason": "旧状态兼容",
            "priority": 1,
            "scope": "persona",
            "created_at": 11.0,
            "status": "done",
        }
    )
    await db.upsert_todo(
        {
            "id": "todo_expired",
            "title": "已过期",
            "reason": "未完成但过期时不应出现在 open 查询",
            "priority": 9,
            "scope": "persona",
            "created_at": 9.0,
            "expires_at": 1.0,
        }
    )
    open_todos = await db.get_todos(include_completed=False)
    assert [item.id for item in open_todos] == ["todo_1", "todo_open"]
    assert open_todos[0] == todo
    assert open_todos[1].title == "仍未完成"
    assert isinstance(open_todos[0], Todo)
    all_todos = await db.get_todos(include_completed=True)
    assert {item.id for item in all_todos} == {
        "todo_1",
        "todo_open",
        "todo_2",
        "todo_expired",
    }
    await db.upsert_todo(
        Todo(
            id="todo_1",
            title="写更多测试",
            reason="覆盖数据库契约",
            priority=3,
            scope="persona",
            created_at=12.0,
        )
    )
    assert (await db.get_todos(include_completed=False))[0].title == "写更多测试"
    assert await db.remove_todos("todo_1") == 1

    cue = Cue(
        id="cue_1",
        cue_type="conversation",
        summary="提醒喝水",
        conversation_id="private:u1",
        created_at=10.0,
        expires_at=4_102_444_800.0,
    )
    expired_cue = Cue(
        id="cue_2",
        cue_type="conversation",
        summary="过期线索",
        conversation_id="private:u1",
        created_at=1.0,
        expires_at=1.0,
    )
    cue_id = await db.upsert_cue(cue)
    await db.upsert_cue(expired_cue)
    cues = await db.get_cues(now=1_767_225_600.0)
    assert cues == [cue]
    assert isinstance(cues[0], Cue)
    assert await db.expire_cues(now=1_767_225_600.0) == 1
    assert await db.remove_cues([cue_id]) == 1
    assert await db.get_cues(now=1_767_225_600.0) == []

    profile = UserProfile(
        user_id="u1",
        display_name="张三",
        affinity=0.8,
        summary="喜欢咖啡",
        traits=["咖啡"],
        interaction_count=3,
        last_interaction_at=10.0,
    )
    await db.upsert_profile(profile)
    loaded_profile = await db.get_profile("u1")
    assert loaded_profile == profile
    assert isinstance(loaded_profile, UserProfile)

    await db.upsert_profile({"user_id": "u2", "nickname": "李四"})
    legacy_profile = await db.get_profile("u2")
    assert isinstance(legacy_profile, UserProfile)
    assert legacy_profile.display_name == "李四"
    assert await db.all_profiles() == [
        profile,
        UserProfile(user_id="u2", display_name="李四"),
    ]


@pytest.mark.asyncio
async def test_persona_db_open_todos_filter_iso_timezone_z_and_numeric_expiry(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    await db.upsert_todo(
        {
            "id": "todo_iso_z_expired",
            "title": "Z 时间已过期",
            "reason": "unit",
            "priority": 5,
            "scope": "persona",
            "created_at": 1.0,
            "expires_at": "2000-01-01T00:00:00Z",
        }
    )
    await db.upsert_todo(
        {
            "id": "todo_iso_offset_expired",
            "title": "带时区时间已过期",
            "reason": "unit",
            "priority": 5,
            "scope": "persona",
            "created_at": 2.0,
            "expires_at": "2000-01-01T08:00:00+08:00",
        }
    )
    await db.upsert_todo(
        {
            "id": "todo_unparseable_kept",
            "title": "不可解析时间保守保留",
            "reason": "unit",
            "priority": 4,
            "scope": "persona",
            "created_at": 3.0,
            "expires_at": "not-a-time",
        }
    )
    await db.upsert_todo(
        {
            "id": "todo_numeric_future",
            "title": "数字时间戳仍有效",
            "reason": "unit",
            "priority": 3,
            "scope": "persona",
            "created_at": 4.0,
            "expires_at": 4_102_444_800.0,
        }
    )
    await db.upsert_todo(
        {
            "id": "todo_numeric_expired",
            "title": "数字时间戳已过期",
            "reason": "unit",
            "priority": 9,
            "scope": "persona",
            "created_at": 5.0,
            "expires_at": 1.0,
        }
    )

    open_todos = await db.get_todos(include_completed=False)

    assert [todo.id for todo in open_todos] == [
        "todo_unparseable_kept",
        "todo_numeric_future",
    ]
    assert open_todos[0].expires_at == 0.0
    assert open_todos[1].expires_at == 4_102_444_800.0


@pytest.mark.asyncio
async def test_persona_db_marks_expired_todos_missed_idempotently(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    await db.upsert_todo(
        {
            "id": "todo_expired",
            "title": "已错过的提醒",
            "reason": "unit",
            "priority": 9,
            "scope": "private:u1",
            "created_at": 1.0,
            "expires_at": 100.0,
        }
    )
    await db.upsert_todo(
        {
            "id": "todo_open",
            "title": "仍然有效",
            "reason": "unit",
            "priority": 1,
            "scope": "private:u1",
            "created_at": 2.0,
            "expires_at": 4_102_444_800.0,
        }
    )

    assert await db.mark_expired_todos_missed(now=150.0) == 1
    assert [todo.id for todo in await db.get_todos(include_completed=False)] == ["todo_open"]

    audit_todos = {todo.id: todo for todo in await db.get_todos(include_completed=True)}
    assert set(audit_todos) == {"todo_expired", "todo_open"}
    assert audit_todos["todo_expired"].status == "missed"
    assert audit_todos["todo_expired"].completed is True
    assert audit_todos["todo_open"].status == "open"
    assert audit_todos["todo_open"].completed is False

    assert await db.mark_expired_todos_missed(now=150.0) == 0
    audit_todos_again = {todo.id: todo for todo in await db.get_todos(include_completed=True)}
    assert audit_todos_again["todo_expired"].status == "missed"
    assert audit_todos_again["todo_expired"].completed is True


@pytest.mark.asyncio
async def test_persona_db_partial_todo_update_preserves_metadata_and_can_complete(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    await db.upsert_todo(
        {
            "id": "todo_patch",
            "title": "旧标题",
            "reason": "保留原因",
            "priority": 8,
            "scope": "private:u1",
            "created_at": 123.0,
            "expires_at": 4_102_444_800.0,
        }
    )
    await db.upsert_todo({"id": "todo_patch", "title": "新标题"})

    patched = (await db.get_todos(include_completed=False))[0]
    assert patched.id == "todo_patch"
    assert patched.title == "新标题"
    assert patched.reason == "保留原因"
    assert patched.priority == 8
    assert patched.scope == "private:u1"
    assert patched.created_at == 123.0
    assert patched.expires_at == 4_102_444_800.0

    await db.upsert_todo({"id": "todo_patch", "done": True})

    assert await db.get_todos(include_completed=False) == []
    completed = (await db.get_todos(include_completed=True))[0]
    assert completed.title == "新标题"
    assert completed.reason == "保留原因"
    assert completed.priority == 8


@pytest.mark.asyncio
async def test_persona_db_recent_records_and_important_memory(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    await db.add_monologue({"text": "第一条"})
    await db.add_monologue({"text": "第二条"})
    assert await db.recent_monologues(limit=1) == [{"text": "第二条"}]

    await db.add_trajectory({"date": "2026-06-12", "summary": "开始"})
    assert await db.recent_trajectories(limit=5) == [
        {"date": "2026-06-12", "summary": "开始"}
    ]
    await db.append_state_log({"event": "state_old"})
    await db.append_state_log({"event": "state_new"})
    assert await db.recent_state_logs(limit=1) == [{"event": "state_new"}]
    assert await db.recent_state_logs(limit=2) == [
        {"event": "state_new"},
        {"event": "state_old"},
    ]

    assert await db.add_arc_event({"event": "created"}) == 1
    assert await db.add_arc_event({"event": "updated"}) == 2
    assert await db.recent_arc_events(limit=1) == [{"event": "updated"}]
    assert await db.recent_arc_events(limit=2) == [
        {"event": "updated"},
        {"event": "created"},
    ]

    assert await db.add_sleep_record({"id": "sleep_1", "started_at": "22:00"}) == "sleep_1"
    assert await db.update_sleep_record("sleep_1", {"ended_at": "07:00"}) is True
    assert await db.add_sleep_record({"id": "sleep_2", "started_at": "23:00"}) == "sleep_2"
    assert await db.recent_sleep_records(limit=1) == [
        {"id": "sleep_2", "started_at": "23:00"}
    ]
    assert await db.recent_sleep_records(limit=2) == [
        {"id": "sleep_2", "started_at": "23:00"},
        {"id": "sleep_1", "record_id": "sleep_1", "started_at": "22:00", "ended_at": "07:00"},
    ]

    assert await db.add_eat_record({"food": "面包"}) == 1
    assert await db.add_eat_record({"food": "米饭"}) == 2
    assert await db.recent_eat_records(limit=1) == [{"food": "米饭"}]
    assert await db.recent_eat_records(limit=2) == [
        {"food": "米饭"},
        {"food": "面包"},
    ]

    assert await db.read_important(default=[]) == []
    memories = [
        {
            "id": "mem_1",
            "timestamp": "T1",
            "content": "张三是朋友",
            "scope": "global",
            "pinned": False,
            "metadata": {"source": "test"},
        }
    ]
    await db.write_important(memories)

    assert await db.read_important(default=[]) == memories
    assert await db.important_count() == 1


@pytest.mark.asyncio
async def test_persona_db_update_eat_record_persists_status_and_end(tmp_path):
    db = PersonaDB(tmp_path / "persona.sqlite")
    await db.load()

    assert await db.add_eat_record(
        {
            "id": "eat_1",
            "meal_type": "breakfast",
            "started_at": 100.0,
            "status": "active",
        }
    ) == 1
    assert await db.update_eat_record(
        "eat_1",
        {"ended_at": 160.0, "status": "finished"},
    ) is True

    assert await db.recent_eat_records(limit=1) == [
        {
            "id": "eat_1",
            "meal_type": "breakfast",
            "record_id": "eat_1",
            "started_at": 100.0,
            "ended_at": 160.0,
            "status": "finished",
        }
    ]

    with sqlite3.connect(tmp_path / "persona.sqlite") as conn:
        row = conn.execute(
            "SELECT ended_at, status FROM eat_records WHERE record_id = ?",
            ("eat_1",),
        ).fetchone()

    assert row == ("160.0", "finished")
    assert await db.update_eat_record("missing", {"ended_at": 200.0}) is False


@pytest.mark.asyncio
async def test_persona_db_migrates_legacy_eat_records_without_record_id(tmp_path):
    db_path = tmp_path / "persona.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE eat_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO eat_records (record_json, created_at) VALUES (?, ?)",
            ('{"id": "eat_from_json", "food": "面"}', "2026-06-13 08:00:00"),
        )
        conn.execute(
            "INSERT INTO eat_records (record_json, created_at) VALUES (?, ?)",
            ('{"food": "粥"}', "2026-06-13 09:00:00"),
        )

    db = PersonaDB(db_path)
    await db.load()

    assert await db.recent_eat_records(limit=2) == [
        {"food": "粥", "id": "eat_2", "record_id": "eat_2"},
        {"food": "面", "id": "eat_from_json", "record_id": "eat_from_json"},
    ]

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, record_id FROM eat_records ORDER BY id ASC"
        ).fetchall()

    assert rows == [(1, "eat_from_json"), (2, "eat_2")]
    assert await db.update_eat_record(
        "eat_2",
        {"ended_at": 120.0, "status": "finished"},
    ) is True

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT ended_at, status FROM eat_records WHERE record_id = ?",
            ("eat_2",),
        ).fetchone()

    assert row == ("120.0", "finished")


@pytest.mark.asyncio
async def test_persona_db_migrates_legacy_schema_with_audits_additively(tmp_path):
    db_path = tmp_path / "persona.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE persona_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE effects (
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
            CREATE TABLE user_profiles (
                user_id TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO schema_version (id, version, updated_at) VALUES (1, 1, ?)",
            ("2026-06-13 10:00:00",),
        )
        conn.execute(
            "INSERT INTO persona_state (id, state_json, updated_at) VALUES (1, ?, ?)",
            ('{"mood": 66.0, "energy": 55.0}', "2026-06-13 10:00:00"),
        )
        conn.execute(
            """
            INSERT INTO effects (
                effect_id, effect_json, expires_at, active, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (
                "effect_legacy",
                '{"id": "effect_legacy", "name": "legacy", "expires_at": 4102444800.0}',
                "4102444800.0",
                "2026-06-13 10:00:00",
                "2026-06-13 10:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO user_profiles (user_id, profile_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                "u1",
                '{"user_id": "u1", "display_name": "旧用户", "affinity": 0.5}',
                "2026-06-13 10:00:00",
                "2026-06-13 10:00:00",
            ),
        )

    db = PersonaDB(db_path)
    await db.load()

    with sqlite3.connect(db_path) as conn:
        audit_table = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'persona_update_audits'
            """
        ).fetchone()
        schema_version = conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()[0]

    assert audit_table == ("persona_update_audits",)
    assert schema_version == SCHEMA_VERSION

    state = await db.get_state()
    assert state.mood == 66.0
    assert state.energy == 55.0
    assert [effect.id for effect in await db.get_active_effects(now=1.0)] == [
        "effect_legacy"
    ]
    assert await db.get_profile("u1") == UserProfile(
        user_id="u1",
        display_name="旧用户",
        affinity=0.5,
    )
