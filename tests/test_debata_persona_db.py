"""DebataPersonaDB legacy persona domains 测试。"""

from __future__ import annotations

import sqlite3

import pytest

from memory.debata_db import DebataDB
from memory.debata_stores import DebataPersonaDB
from mind import Cue, Effect, PersonaState, Todo, UserProfile
from mind.db import SCHEMA_VERSION


@pytest.mark.asyncio
async def test_debata_persona_db_load_schema_version_empty_persona_and_export(tmp_path):
    db = DebataPersonaDB(tmp_path / "debata.db", " yuexi ")

    assert db.persona_id == "yuexi"
    assert isinstance(db.db, DebataDB)
    await db.load()

    with sqlite3.connect(tmp_path / "debata.db") as conn:
        row = conn.execute(
            """
            SELECT version FROM persona_schema_version_legacy
            WHERE persona_id = ? AND id = 1
            """,
            ("yuexi",),
        ).fetchone()

    assert row == (SCHEMA_VERSION,)
    with pytest.raises(ValueError, match="persona_id must not be empty"):
        DebataPersonaDB(tmp_path / "debata.db", "  ")

    from memory import DebataPersonaDB as PackageDebataPersonaDB

    assert PackageDebataPersonaDB is DebataPersonaDB


@pytest.mark.asyncio
async def test_debata_persona_db_state_logs_and_update_audits_are_persona_scoped(tmp_path):
    db_path = tmp_path / "debata.db"
    yuexi = DebataPersonaDB(db_path, "yuexi")
    jiu = DebataPersonaDB(db_path, "jiu")
    await yuexi.load()
    await jiu.load()

    assert isinstance(await yuexi.get_state(), PersonaState)
    assert await yuexi.get_state(default=None) is None

    state = PersonaState(mood=72.0, energy=61.0, latest_monologue="今天状态稳定")
    await yuexi.save_state(state)
    await jiu.save_state(PersonaState(mood=12.0))

    assert await yuexi.get_state() == state
    assert (await jiu.get_state()).mood == 12.0
    assert await yuexi.append_state_log({"event": "y1"}) == 1
    assert await yuexi.append_state_log({"event": "y2"}) == 2
    assert await jiu.append_state_log({"event": "j1"}) == 1
    assert await yuexi.recent_state_logs(limit=2) == [{"event": "y2"}, {"event": "y1"}]
    assert await jiu.recent_state_logs(limit=2) == [{"event": "j1"}]

    assert await yuexi.append_update_audit(
        {
            "trigger": "message",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"mood": 10},
        }
    ) == 1
    assert await yuexi.append_update_audit(
        {
            "trigger": "tick",
            "conversation_id": "private:u2",
            "user_id": "u2",
            "update": {"energy": 20},
        }
    ) == 2
    assert await jiu.append_update_audit(
        {
            "trigger": "message",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"mood": 99},
        }
    ) == 1

    assert await yuexi.recent_update_audits(limit=1) == [
        {
            "trigger": "tick",
            "conversation_id": "private:u2",
            "user_id": "u2",
            "update": {"energy": 20},
        }
    ]
    assert await yuexi.recent_update_audits(conversation_id="private:u1") == [
        {
            "trigger": "message",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"mood": 10},
        }
    ]
    assert await yuexi.recent_update_audits(user_id="u1") == [
        {
            "trigger": "message",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "update": {"mood": 10},
        }
    ]


@pytest.mark.asyncio
async def test_debata_persona_db_effect_todo_cue_profile_crud_and_types(tmp_path):
    db = DebataPersonaDB(tmp_path / "debata.db", "yuexi")
    await db.load()

    active_effect = Effect(
        id="effect_1",
        name="buff",
        effect_type="mood",
        intensity=1.5,
        prompt_hint="保持温和",
        source_detail="unit",
        created_at=10.0,
        expires_at=4_102_444_800.0,
    )
    expired_effect = Effect(
        id="effect_2",
        name="old",
        effect_type="mood",
        intensity=0.2,
        prompt_hint="",
        source_detail="unit",
        created_at=1.0,
        expires_at=1.0,
    )
    assert await db.add_effect(active_effect) == "effect_1"
    await db.add_effect(expired_effect)
    effects = await db.get_active_effects(now=1_767_225_600.0)
    assert effects == [active_effect]
    assert isinstance(effects[0], Effect)
    assert await db.expire_effects(now=1_767_225_600.0) == 1
    assert await db.remove_effects("effect_1") == 1

    todo = Todo(
        id="todo_1",
        title="写测试",
        reason="覆盖 Debata persona",
        priority=2,
        scope="persona",
        created_at=10.0,
        expires_at=4_102_444_800.0,
    )
    await db.upsert_todo(todo)
    await db.upsert_todo(
        {
            "id": "todo_expired",
            "title": "过期",
            "reason": "unit",
            "priority": 9,
            "scope": "persona",
            "created_at": 1.0,
            "expires_at": 1.0,
        }
    )
    await db.upsert_todo({"id": "todo_done", "title": "完成", "status": "done"})
    open_todos = await db.get_todos(include_completed=False)
    assert open_todos == [todo]
    assert isinstance(open_todos[0], Todo)
    await db.upsert_todo({"id": "todo_1", "title": "写更多测试"})
    patched = (await db.get_todos(include_completed=False))[0]
    assert patched.title == "写更多测试"
    assert patched.reason == "覆盖 Debata persona"
    assert await db.mark_expired_todos_missed(now=150.0) == 1
    missed = {todo.id: todo for todo in await db.get_todos(include_completed=True)}
    assert missed["todo_expired"].status == "missed"
    assert missed["todo_expired"].completed is True
    assert await db.remove_todos(["todo_1", "todo_done"]) == 2

    cue = Cue(
        id="cue_1",
        cue_type="conversation",
        summary="提醒喝水",
        conversation_id="private:u1",
        created_at=10.0,
        expires_at=4_102_444_800.0,
    )
    await db.upsert_cue(cue)
    await db.upsert_cue({"id": "cue_old", "summary": "old", "expires_at": 1.0})
    cues = await db.get_cues(now=1_767_225_600.0)
    assert cues == [cue]
    assert isinstance(cues[0], Cue)
    assert await db.expire_cues(now=1_767_225_600.0) == 1
    assert await db.remove_cues("cue_1") == 1

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
    await db.upsert_profile({"user_id": "u2", "nickname": "李四"})
    assert await db.get_profile("u1") == profile
    assert isinstance(await db.get_profile("u1"), UserProfile)
    assert (await db.get_profile("u2")).display_name == "李四"
    assert await db.get_profile("") is None
    assert await db.all_profiles() == [
        profile,
        UserProfile(user_id="u2", display_name="李四"),
    ]


@pytest.mark.asyncio
async def test_debata_persona_db_recent_records_updates_and_important(tmp_path):
    db = DebataPersonaDB(tmp_path / "debata.db", "yuexi")
    await db.load()

    assert await db.add_monologue({"text": "第一条"}) == 1
    assert await db.add_monologue({"text": "第二条"}) == 2
    assert await db.recent_monologues(limit=1) == [{"text": "第二条"}]

    assert await db.add_trajectory({"date": "2026-06-18", "summary": "开始"}) == 1
    assert await db.recent_trajectories(limit=5) == [
        {"date": "2026-06-18", "summary": "开始"}
    ]
    assert await db.add_arc_event({"event": "created"}) == 1
    assert await db.add_arc_event({"event": "updated"}) == 2
    assert await db.recent_arc_events(limit=2) == [
        {"event": "updated"},
        {"event": "created"},
    ]

    assert await db.add_sleep_record({"id": "sleep_1", "started_at": "22:00"}) == "sleep_1"
    assert await db.update_sleep_record("sleep_1", {"ended_at": "07:00"}) is True
    assert await db.add_sleep_record({"id": "sleep_2", "started_at": "23:00"}) == "sleep_2"
    assert await db.update_sleep_record("missing", {"ended_at": "08:00"}) is False
    assert await db.recent_sleep_records(limit=2) == [
        {"id": "sleep_2", "started_at": "23:00"},
        {"id": "sleep_1", "record_id": "sleep_1", "started_at": "22:00", "ended_at": "07:00"},
    ]

    assert await db.add_eat_record({"id": "eat_1", "food": "面包", "status": "active"}) == 1
    assert await db.add_eat_record({"food": "米饭"}) == 2
    assert await db.update_eat_record("eat_1", {"ended_at": 160.0, "status": "finished"}) is True
    assert await db.update_eat_record("missing", {"ended_at": 200.0}) is False
    assert await db.recent_eat_records(limit=2) == [
        {"food": "米饭"},
        {
            "id": "eat_1",
            "food": "面包",
            "record_id": "eat_1",
            "ended_at": 160.0,
            "status": "finished",
        },
    ]

    assert await db.read_important(default=[]) == []
    memories = [{"id": "mem_1", "content": "张三是朋友", "scope": "global"}]
    await db.write_important(memories)
    assert await db.read_important(default=[]) == memories
    assert await db.important_count() == 1

    with sqlite3.connect(tmp_path / "debata.db") as conn:
        row = conn.execute(
            """
            SELECT ended_at, status FROM persona_eat_records
            WHERE persona_id = ? AND record_id = ?
            """,
            ("yuexi", "eat_1"),
        ).fetchone()
    assert row == ("160.0", "finished")


@pytest.mark.asyncio
async def test_debata_persona_db_personas_have_independent_ids_and_mutations(tmp_path):
    db_path = tmp_path / "debata.db"
    yuexi = DebataPersonaDB(db_path, "yuexi")
    jiu = DebataPersonaDB(db_path, "jiu")
    await yuexi.load()
    await jiu.load()

    assert await yuexi.add_monologue({"text": "y"}) == 1
    assert await jiu.add_monologue({"text": "j"}) == 1
    assert await yuexi.add_trajectory({"date": "y"}) == 1
    assert await jiu.add_trajectory({"date": "j"}) == 1
    assert await yuexi.add_arc_event({"event": "y"}) == 1
    assert await jiu.add_arc_event({"event": "j"}) == 1
    assert await yuexi.add_eat_record({"id": "same", "food": "y"}) == 1
    assert await jiu.add_eat_record({"id": "same", "food": "j"}) == 1

    await yuexi.add_effect({"id": "effect_shared", "expires_at": 1.0})
    await jiu.add_effect({"id": "effect_shared", "expires_at": 4_102_444_800.0})
    await yuexi.upsert_cue({"id": "cue_shared", "expires_at": 1.0})
    await jiu.upsert_cue({"id": "cue_shared", "expires_at": 4_102_444_800.0})
    await yuexi.upsert_todo({"id": "todo_shared", "expires_at": 1.0})
    await jiu.upsert_todo({"id": "todo_shared", "expires_at": 4_102_444_800.0})

    assert await yuexi.expire_effects(now=150.0) == 1
    assert await yuexi.expire_cues(now=150.0) == 1
    assert await yuexi.mark_expired_todos_missed(now=150.0) == 1
    assert [effect.id for effect in await jiu.get_active_effects(now=150.0)] == ["effect_shared"]
    assert [cue.id for cue in await jiu.get_cues(now=150.0)] == ["cue_shared"]
    assert [todo.id for todo in await jiu.get_todos(include_completed=False)] == ["todo_shared"]

    assert await yuexi.recent_monologues(limit=5) == [{"text": "y"}]
    assert await jiu.recent_monologues(limit=5) == [{"text": "j"}]
    assert await yuexi.recent_eat_records(limit=5) == [{"id": "same", "food": "y"}]
    assert await jiu.recent_eat_records(limit=5) == [{"id": "same", "food": "j"}]
