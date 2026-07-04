from __future__ import annotations

import pytest

import agents.persona_agent as persona_agent_mod
from mind import PersonaState, Todo
from tests.persona_agent.helpers import (
    FakeConsolidation,
    FakeDB,
    FakeProvider,
    LinearDecay,
    WritingConsolidation,
    _agent,
    _pm_cfg,
)


@pytest.mark.asyncio
async def test_sleep_start_settles_existing_eating_before_replacement(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            energy=70,
            satiety=20,
            current_action="eating",
            action_until=8200.0,
            active_eat_record_id="eat_active",
            last_eat_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2800.0)
    await agent.on_sleep_start(60)

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "sleeping"
    assert snapshot.satiety == pytest.approx(50.0)
    assert snapshot.last_tick_at == 2800.0
    assert db.eat_updates[0][0] == "eat_active"
    assert db.eat_updates[0][1]["ended_at"] == 2800.0
    assert db.eat_updates[0][1]["status"] == "finished"
    assert db.eat_updates[0][1]["finish_reason"] == "replaced"
    assert db.eat_updates[0][1]["recovery_source"] == "fallback_formula"
    assert decay.satiety_calls[-1]["current_action"] == "eating"

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 6400.0)
    await agent.periodic_tick()

    assert [call["current_action"] for call in decay.satiety_calls] == [
        "eating",
        "sleeping",
    ]
    assert decay.satiety_calls[-1]["elapsed_hours"] == 1.0
    await agent.shutdown()


@pytest.mark.asyncio
async def test_sleep_start_completes_sleep_action_todos(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(id="todo_sleep", title="去睡觉", reason="现在该休息", priority=7, scope="persona", created_at=900.0),
        Todo(id="todo_physiological_sleep", title="睡觉", reason="困了，自己说了睡了睡了，需要执行睡眠动作", priority=8, scope="physiological", created_at=900.0),
        Todo(id="todo_rest", title="躺下休息", reason="", priority=5, scope="sleep", created_at=900.0),
        Todo(id="todo_wakeup", title="明早叫醒用户", reason="提醒用户起床", priority=9, scope="persona", created_at=900.0),
        Todo(id="todo_private", title="去睡觉", reason="", priority=4, scope="private:u1", created_at=900.0),
    ]
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool"))

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    await agent.on_sleep_start(60)

    assert [todo.id for todo in db.todos] == ["todo_wakeup", "todo_private"]
    assert [todo.id for todo in agent.get_todos_for_proactive()] == ["todo_wakeup", "todo_private"]
    assert set(db.closed_todos) == {"todo_sleep", "todo_physiological_sleep", "todo_rest"}
    assert db.closed_todos["todo_sleep"]["status"] == "completed"
    assert db.closed_todos["todo_sleep"]["completed"] is True
    assert db.closed_todos["todo_sleep"]["completed_at"] == 1000.0
    assert db.closed_todos["todo_physiological_sleep"]["status"] == "completed"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_trigger_collapse_settles_existing_sleeping_before_replacement(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            energy=20,
            satiety=80,
            current_action="sleeping",
            action_until=8200.0,
            active_sleep_record_id="sleep_active",
            last_sleep_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2800.0)
    await agent.trigger_collapse()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "collapsing"
    assert snapshot.energy == pytest.approx(25.0)
    assert snapshot.last_tick_at == 2800.0
    assert db.sleep_updates[0][0] == "sleep_active"
    assert db.sleep_updates[0][1]["ended_at"] == 2800.0
    assert db.sleep_updates[0][1]["status"] == "finished"
    assert db.sleep_updates[0][1]["finish_reason"] == "replaced"
    assert db.sleep_updates[0][1]["recovery_source"] == "fallback_formula"
    assert decay.energy_calls[-1]["current_action"] == "sleeping"

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 6400.0)
    await agent.periodic_tick()

    assert [call["current_action"] for call in decay.energy_calls] == [
        "sleeping",
        "sleeping",
    ]
    assert decay.energy_calls[-1]["elapsed_hours"] == 1.0
    await agent.shutdown()


@pytest.mark.asyncio
async def test_eat_start_settles_existing_sleeping_before_replacement(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            energy=20,
            satiety=80,
            current_action="sleeping",
            action_until=8200.0,
            active_sleep_record_id="sleep_active",
            last_sleep_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2800.0)
    await agent.on_eat_start("snack", 30, "bread")

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "eating"
    assert snapshot.energy == pytest.approx(25.0)
    assert snapshot.last_tick_at == 2800.0
    assert db.sleep_updates[0][0] == "sleep_active"
    assert db.sleep_updates[0][1]["ended_at"] == 2800.0
    assert db.sleep_updates[0][1]["status"] == "finished"
    assert db.sleep_updates[0][1]["finish_reason"] == "replaced"
    assert db.sleep_updates[0][1]["recovery_source"] == "fallback_formula"
    assert decay.energy_calls[-1]["current_action"] == "sleeping"

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 4600.0)
    await agent.periodic_tick()

    assert [call["current_action"] for call in decay.energy_calls] == [
        "sleeping",
        "eating",
    ]
    assert decay.energy_calls[-1]["elapsed_hours"] == 0.5
    await agent.shutdown()


@pytest.mark.parametrize(
    "title",
    [
        "提醒一下我吃饭",
        "叫一下我喝水",
        "通知本人吃饭",
        "一会 提醒我喝水",
    ],
)
def test_todo_matches_current_action_start_excludes_eat_external_reminders(title):
    todo = Todo(id="todo", title=title, reason="", priority=1, scope="persona", created_at=0.0)

    assert persona_agent_mod._todo_matches_current_action_start(todo, "eat") is False


@pytest.mark.parametrize(
    "title",
    [
        "吃药后睡觉",
        "服药后休息",
        "提醒我睡觉",
    ],
)
def test_todo_matches_current_action_start_excludes_sleep_external_or_medicine(title):
    todo = Todo(id="todo", title=title, reason="", priority=1, scope="persona", created_at=0.0)

    assert persona_agent_mod._todo_matches_current_action_start(todo, "sleep") is False


def test_todo_matches_current_action_start_keeps_self_eat_todo_reason():
    todo = Todo(
        id="todo",
        title="去觅食填饱肚子",
        reason="用户也让我先去吃饭",
        priority=1,
        scope="short_term",
        created_at=0.0,
    )

    assert persona_agent_mod._todo_matches_current_action_start(todo, "eat") is True


@pytest.mark.asyncio
async def test_eat_start_completes_eat_action_todos_but_keeps_external_reminders(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(id="todo_eat", title="去吃饭", reason="午餐时间到了", priority=7, scope="persona", created_at=900.0),
        Todo(id="todo_physiological_eat", title="去吃点东西填饱肚子", reason="", priority=7, scope="physiological", created_at=900.0),
        Todo(id="todo_short_term_forage", title="去觅食填饱肚子", reason="用户也让我先去吃饭", priority=7, scope="short_term", created_at=900.0),
        Todo(id="todo_water", title="喝水", reason="", priority=5, scope="", created_at=900.0),
        Todo(id="todo_remind_water", title="稍后提醒用户喝水", reason="提醒对方补水", priority=8, scope="persona", created_at=900.0),
        Todo(id="todo_tomorrow", title="明天提醒主人吃药", reason="", priority=9, scope="eat", created_at=900.0),
        Todo(id="todo_medicine", title="吃药", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_medicine_water", title="吃药喝水", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_after_medicine_water", title="吃药后喝水", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_remember_medicine", title="记得吃药", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_remind_me_eat", title="提醒我吃饭", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_call_me_water", title="叫我喝水", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_notify_me_eat", title="通知我吃饭", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_later_remind_me_water", title="一会儿提醒我喝水", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_private", title="吃饭", reason="", priority=4, scope="private:u1", created_at=900.0),
    ]
    agent = _agent(db, pm_cfg=_pm_cfg(satiety="tool"))

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    await agent.on_eat_start("lunch", 30, "rice")

    assert [todo.id for todo in db.todos] == [
        "todo_remind_water",
        "todo_tomorrow",
        "todo_medicine",
        "todo_medicine_water",
        "todo_after_medicine_water",
        "todo_remember_medicine",
        "todo_remind_me_eat",
        "todo_call_me_water",
        "todo_notify_me_eat",
        "todo_later_remind_me_water",
        "todo_private",
    ]
    assert [todo.id for todo in agent.get_todos_for_proactive()] == [
        "todo_tomorrow",
        "todo_remind_water",
        "todo_after_medicine_water",
        "todo_call_me_water",
        "todo_later_remind_me_water",
        "todo_medicine",
        "todo_medicine_water",
        "todo_notify_me_eat",
        "todo_remember_medicine",
        "todo_remind_me_eat",
        "todo_private",
    ]
    assert set(db.closed_todos) == {
        "todo_eat",
        "todo_physiological_eat",
        "todo_short_term_forage",
        "todo_water",
    }
    assert db.closed_todos["todo_eat"]["status"] == "completed"
    assert db.closed_todos["todo_physiological_eat"]["status"] == "completed"
    assert db.closed_todos["todo_short_term_forage"]["status"] == "completed"
    assert db.closed_todos["todo_water"]["completed"] is True
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_completes_active_eating_action_todos_but_keeps_external_reminders(monkeypatch):
    db = FakeDB(
        PersonaState(
            current_action="eating",
            action_until=1900.0,
            last_tick_at=1000.0,
        )
    )
    db.todos = [
        Todo(id="todo_physiological_eat", title="去吃点东西填饱肚子", reason="", priority=7, scope="physiological", created_at=900.0),
        Todo(id="todo_short_term_forage", title="去觅食填饱肚子", reason="已经开始吃饭了", priority=7, scope="short_term", created_at=900.0),
        Todo(id="todo_remind_water", title="稍后提醒用户喝水", reason="提醒对方补水", priority=8, scope="persona", created_at=900.0),
        Todo(id="todo_medicine", title="吃药", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_after_medicine_water", title="吃药后喝水", reason="", priority=6, scope="persona", created_at=900.0),
        Todo(id="todo_remind_me_eat", title="提醒我吃饭", reason="", priority=6, scope="persona", created_at=900.0),
    ]
    agent = _agent(db, pm_cfg=_pm_cfg(satiety="tool"))

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()

    assert [todo.id for todo in db.todos] == [
        "todo_remind_water",
        "todo_medicine",
        "todo_after_medicine_water",
        "todo_remind_me_eat",
    ]
    assert [todo.id for todo in agent.get_todos_for_proactive()] == [
        "todo_remind_water",
        "todo_after_medicine_water",
        "todo_medicine",
        "todo_remind_me_eat",
    ]
    assert set(db.closed_todos) == {"todo_physiological_eat", "todo_short_term_forage"}
    assert db.closed_todos["todo_physiological_eat"]["status"] == "completed"
    assert db.closed_todos["todo_physiological_eat"]["completed_at"] == 1000.0
    assert db.closed_todos["todo_short_term_forage"]["status"] == "completed"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_does_not_complete_expired_eating_action_todos(monkeypatch):
    db = FakeDB(
        PersonaState(
            current_action="eating",
            action_until=900.0,
            last_tick_at=800.0,
        )
    )
    db.todos = [
        Todo(id="todo_physiological_eat", title="去吃点东西填饱肚子", reason="", priority=7, scope="physiological", created_at=700.0),
        Todo(id="todo_short_term_forage", title="去觅食填饱肚子", reason="", priority=7, scope="short_term", created_at=700.0),
    ]
    agent = _agent(db, pm_cfg=_pm_cfg(satiety="tool"))

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert [todo.id for todo in db.todos] == ["todo_physiological_eat", "todo_short_term_forage"]
    assert db.closed_todos == {}
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_segments_eating_until_action_until_then_awake(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            energy=70,
            satiety=20,
            current_action="eating",
            action_until=1600.0,
            active_eat_record_id="eat_restart",
            last_eat_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 5200.0)
    await agent.start()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert snapshot.active_eat_record_id is None
    assert snapshot.last_tick_at == 5200.0
    assert snapshot.energy == pytest.approx(69.3333333333)
    assert snapshot.satiety == pytest.approx(28.0)
    assert [call["current_action"] for call in decay.energy_calls] == ["eating", "awake"]
    assert [call["elapsed_hours"] for call in decay.energy_calls] == [
        pytest.approx(1.0 / 6.0),
        1.0,
    ]
    assert [call["current_action"] for call in decay.satiety_calls] == ["eating", "awake"]
    assert db.eat_updates[0][0] == "eat_restart"
    assert db.eat_updates[0][1]["ended_at"] == 1600.0
    assert db.eat_updates[0][1]["status"] == "finished"
    assert db.eat_updates[0][1]["finish_reason"] == "action_until"
    assert db.eat_updates[0][1]["recovery_source"] == "offline_reconcile"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_expired_collapse_recovers_on_start_and_tick_without_recollapse(monkeypatch):
    start_decay = LinearDecay(collapse=True)
    start_db = FakeDB(
        PersonaState(
            current_action="collapsing",
            action_until=100.0,
            energy=0,
            energy_critical_since=10.0,
            last_tick_at=100.0,
        )
    )
    start_agent = _agent(start_db, pm_cfg=_pm_cfg(energy="tool"), decay=start_decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await start_agent.start()

    snapshot = start_agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert start_decay.collapse_calls == []
    assert any(
        log.get("event") == "action_finished"
        and log.get("previous_action") == "collapsing"
        for log in start_db.logs
    )
    await start_agent.shutdown()

    tick_decay = LinearDecay(collapse=True)
    tick_db = FakeDB(
        PersonaState(
            current_action="collapsing",
            action_until=100.0,
            energy=0,
            energy_critical_since=10.0,
            last_tick_at=100.0,
        )
    )
    tick_agent = _agent(tick_db, pm_cfg=_pm_cfg(energy="tool"), decay=tick_decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 50.0)
    await tick_agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await tick_agent.periodic_tick()

    snapshot = tick_agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert tick_decay.collapse_calls == []
    await tick_agent.shutdown()


@pytest.mark.asyncio
async def test_collapsing_reconcile_uses_sleeping_recovery_until_action_until(monkeypatch):
    decay = LinearDecay(collapse=True)
    db = FakeDB(
        PersonaState(
            current_action="collapsing",
            action_until=8200.0,
            energy=0,
            satiety=80,
            energy_critical_since=10.0,
            last_sleep_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 11800.0)
    await agent.start()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert snapshot.last_tick_at == 11800.0
    assert snapshot.energy == pytest.approx(19.0)
    assert [call["current_action"] for call in decay.energy_calls] == ["sleeping", "awake"]
    assert [call["elapsed_hours"] for call in decay.energy_calls] == [2.0, 1.0]
    assert decay.collapse_calls == []
    assert any(
        log.get("event") == "action_finished"
        and log.get("previous_action") == "collapsing"
        for log in db.logs
    )
    assert db.saved[-1].current_action == "awake"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_frequent_restart_start_saves_new_last_tick_at(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(PersonaState(last_tick_at=1000.0))

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1300.0)
    first = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)
    await first.start()
    await first.shutdown()

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1600.0)
    second = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)
    await second.start()

    saved_tick_times = [state.last_tick_at for state in db.saved]
    assert 1300.0 in saved_tick_times
    assert 1600.0 in saved_tick_times
    assert db.saved[-1].last_tick_at == 1600.0
    assert db.state.last_tick_at == 1600.0
    await second.shutdown()



@pytest.mark.asyncio
async def test_sleep_and_eat_disabled_or_tool_records(monkeypatch):
    disabled = _agent(FakeDB(), pm_cfg=_pm_cfg())
    await disabled.start()
    assert await disabled.on_sleep_start(30) == {"status": "disabled"}
    assert await disabled.on_eat_start("breakfast", 15, "toast") == {"status": "disabled"}
    await disabled.shutdown()

    db = FakeDB()
    consolidation = FakeConsolidation()
    subconscious_events = []

    async def start_subconscious(state_snapshot: PersonaState, trigger_event=None) -> None:
        subconscious_events.append(
            {"state_snapshot": state_snapshot, "trigger_event": trigger_event}
        )

    tool = _agent(
        db,
        pm_cfg=_pm_cfg(energy="tool", satiety="tool"),
        consolidation=consolidation,
        subconscious_starter=start_subconscious,
    )
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)

    await tool.start()
    sleep_result = await tool.on_sleep_start(130)

    assert sleep_result["status"] == "started"
    assert tool.get_state_snapshot().current_action == "sleeping"
    assert tool.get_state_snapshot().action_until == 8800
    assert tool.get_state_snapshot().active_sleep_record_id == sleep_result["record_id"]
    assert db.saved[-1].active_sleep_record_id == sleep_result["record_id"]
    assert db.sleep_records[0]["planned_duration_minutes"] == 130
    assert isinstance(consolidation.calls[0]["state_snapshot"], PersonaState)
    assert consolidation.calls[0]["recent_history"] == []
    assert consolidation.calls[0]["sleep_type"] == "long_sleep"
    assert isinstance(subconscious_events[0]["state_snapshot"], PersonaState)
    assert subconscious_events[0]["trigger_event"]["sleep_type"] == "long_sleep"

    wakeup_state = await tool.on_wakeup("manual")
    assert wakeup_state.current_action == "awake"
    assert db.sleep_updates[0][1]["wakeup_reason"] == "manual"

    eat_result = await tool.on_eat_start("breakfast", 15, "toast")
    assert eat_result["status"] == "started"
    assert tool.get_state_snapshot().current_action == "eating"
    assert tool.get_state_snapshot().action_until == 1900
    assert db.eat_records[0]["meal_type"] == "breakfast"
    assert eat_result["record_id"] == db.eat_records[0]["id"]
    assert eat_result["row_id"] == 1
    await tool.shutdown()


@pytest.mark.asyncio
async def test_eat_finish_uses_recovery_eval_before_fallback(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(PersonaState(satiety=20, last_tick_at=1000.0))
    provider = FakeProvider(
        [
            """
            {
              "satiety": 86,
              "mood": 70,
              "social_need": 44,
              "latest_monologue": "吃完正餐后踏实了很多。"
            }
            """
        ]
    )
    agent = _agent(
        db,
        provider=provider,
        pm_cfg=_pm_cfg(energy="tool", satiety="tool"),
        decay=decay,
    )

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    await agent.on_eat_start("lunch", 30, "米饭和菜")
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2800.0)
    await agent.periodic_tick()

    snapshot = agent.get_state_snapshot()
    assert snapshot.satiety == 86
    assert snapshot.mood == 70
    assert snapshot.social_need == 44
    assert snapshot.latest_monologue == "吃完正餐后踏实了很多。"
    assert db.eat_updates[0][1]["recovery_source"] == "persona_eval"
    assert db.eat_updates[0][1]["recovery_estimate"]["satiety"] == 86.0
    prompt = provider.calls[0]["messages"][1]["content"]
    assert "social_need 表示社交未满足度" in prompt
    assert "30-60 是普通稳定区间" in prompt
    assert "避免连续把 social_need 压到 0" in prompt
    await agent.shutdown()


@pytest.mark.asyncio
async def test_wakeup_uses_sleep_recovery_eval(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(PersonaState(energy=30, current_action="awake", last_tick_at=1000.0))
    provider = FakeProvider(
        [
            """
            {
              "energy": 88,
              "mood": 66,
              "latest_monologue": "睡醒后清醒了很多。"
            }
            """
        ]
    )
    agent = _agent(db, provider=provider, pm_cfg=_pm_cfg(energy="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    await agent.on_sleep_start(90)
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2800.0)
    state = await agent.on_wakeup("manual")

    assert state.energy == 88
    assert state.mood == 66
    assert state.latest_monologue == "睡醒后清醒了很多。"
    assert db.sleep_updates[0][1]["recovery_source"] == "persona_eval"
    assert db.sleep_updates[0][1]["recovery_estimate"]["energy"] == 88.0
    await agent.shutdown()


@pytest.mark.asyncio
async def test_eat_finish_fallback_substantial_meal_recovers_reasonably(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(PersonaState(satiety=20, last_tick_at=1000.0))
    provider = FakeProvider(["not json", "{bad"])
    agent = _agent(
        db,
        provider=provider,
        pm_cfg=_pm_cfg(energy="tool", satiety="tool"),
        decay=decay,
    )

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    await agent.start()
    await agent.on_eat_start("lunch", 30, "米饭和菜")
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2800.0)
    await agent.periodic_tick()

    snapshot = agent.get_state_snapshot()
    assert snapshot.satiety >= 78
    assert snapshot.satiety > 35
    assert db.eat_updates[0][1]["recovery_source"] == "fallback_formula"
    assert db.eat_updates[0][1]["recovery_estimate"]["satiety"] >= 78
    assert any(log.get("event") == "recovery_evaluated" for log in db.logs)
    await agent.shutdown()


@pytest.mark.asyncio
async def test_sleep_start_consolidation_does_not_expose_tomorrow_monologue(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    db = FakeDB(PersonaState(latest_monologue="睡前还有点累。", last_monologue_at=900.0))
    consolidation = WritingConsolidation(db, "明早醒来后先整理计划。")
    agent = _agent(
        db,
        pm_cfg=_pm_cfg(energy="tool"),
        consolidation=consolidation,
    )

    await agent.start()
    result = await agent.on_sleep_start(130)

    snapshot = agent.get_state_snapshot()
    assert result["status"] == "started"
    assert consolidation.calls[0]["sleep_type"] == "long_sleep"
    assert db.monologues[0]["text"] == "明早醒来后先整理计划。"
    assert snapshot.current_action == "sleeping"
    assert snapshot.latest_monologue == "睡前还有点累。"
    assert snapshot.last_monologue_at == 900.0
    assert db.saved[-1].latest_monologue == "睡前还有点累。"
    assert db.saved[-1].last_monologue_at == 900.0
    assert "明早醒来后先整理计划。" not in agent.get_context_for_chat("private:u1")
    await agent.shutdown()
