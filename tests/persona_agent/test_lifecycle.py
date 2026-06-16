from __future__ import annotations

import pytest

import agents.persona_agent as persona_agent_mod
from mind import PersonaState
from tests.persona_agent.helpers import FakeDB, FakeProvider, LinearDecay, _agent, _pm_cfg


@pytest.mark.asyncio
async def test_start_shutdown_state_roundtrip_and_context_modes():
    state = PersonaState(energy=25, satiety=18, mood=72, social_need=34)
    db = FakeDB(state)
    disabled = _agent(db, pm_cfg=_pm_cfg())

    await disabled.start()
    assert db.loaded is True
    assert disabled.get_state_snapshot() == state
    disabled_context = disabled.get_context_for_chat("private:u1")
    assert "心情" in disabled_context
    assert "社交需求" in disabled_context
    assert "累" not in disabled_context
    assert "饿" not in disabled_context
    assert "年龄档位" not in disabled_context
    await disabled.shutdown()
    assert db.saved[-1] == state

    tool = _agent(FakeDB(state), pm_cfg=_pm_cfg(energy="tool", satiety="tool"))
    await tool.start()
    tool_context = tool.get_context_for_chat("private:u1")
    assert "累" in tool_context
    assert "饿" in tool_context
    await tool.shutdown()


@pytest.mark.asyncio
async def test_after_turn_skips_llm_update_while_sleeping(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 100.0)
    state = PersonaState(
        current_action="sleeping",
        action_until=700.0,
        latest_monologue="睡着前的念头。",
    )
    db = FakeDB(state)
    provider = FakeProvider(
        [
            """
            {"latest_monologue": "刚睡醒，脑子还有点迷糊。"}
            """
        ]
    )
    statuses = []
    agent = _agent(db, provider=provider, status_callback=statuses.append)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户发来一条消息")

    snapshot = agent.get_state_snapshot()
    assert provider.calls == []
    assert snapshot.current_action == "sleeping"
    assert snapshot.action_until == 700.0
    assert snapshot.latest_monologue == "睡着前的念头。"
    assert db.monologues == []
    assert db.logs[-1]["event"] == "after_turn"
    assert db.logs[-1]["skipped"] is True
    assert db.logs[-1]["skip_reason"] == "persona_resting"
    assert statuses[-1]["state"] == "idle"
    assert statuses[-1]["text"] == "人格休息中，跳过普通状态更新"
    await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_until", [None, "bad"])
async def test_after_turn_does_not_skip_resting_action_without_valid_until(monkeypatch, action_until):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 100.0)
    state = PersonaState(
        current_action="sleeping",
        action_until=action_until,  # type: ignore[arg-type]
        latest_monologue="旧独白。",
    )
    db = FakeDB(state)
    provider = FakeProvider(['{"latest_monologue": "正常更新。"}'])
    agent = _agent(db, provider=provider)

    await agent.start()
    assert agent.is_resting() is False
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户发来一条消息")

    assert len(provider.calls) == 1
    assert db.logs[-1]["event"] == "after_turn"
    assert db.logs[-1].get("skipped") is None
    assert agent.get_state_snapshot().latest_monologue == "正常更新。"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_get_context_for_chat_marks_sleeping_not_awake(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 100.0)
    state = PersonaState(current_action="sleeping", action_until=3700.0)
    agent = _agent(FakeDB(state))

    await agent.start()
    context = agent.get_context_for_chat("private:u1")

    assert "当前动作: 睡眠中" in context
    assert "预计结束:" in context
    assert "剩余约 60 分钟" in context
    assert "尚未醒来" in context
    assert "不应当表现为刚醒" in context
    await agent.shutdown()


@pytest.mark.asyncio
async def test_get_context_for_chat_marks_eating_not_finished(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 100.0)
    state = PersonaState(current_action="eating", action_until=1000.0)
    agent = _agent(FakeDB(state))

    await agent.start()
    context = agent.get_context_for_chat("private:u1")

    assert "当前动作: 进食中" in context
    assert "普通入站消息只记录到潜意识缓冲" in context
    assert "不应在当前动作结束前回复" in context
    assert "表现为已结束" in context
    await agent.shutdown()



@pytest.mark.asyncio
async def test_start_reconciles_stale_awake_state_immediately(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(PersonaState(energy=50, satiety=60, last_tick_at=1000.0))
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 4600.0)
    await agent.start()

    snapshot = agent.get_state_snapshot()
    assert snapshot.last_tick_at == 4600.0
    assert snapshot.energy == 49.0
    assert snapshot.satiety == 58.0
    assert decay.energy_calls == [
        {
            "current": 50,
            "elapsed_hours": 1.0,
            "mode": "tool",
            "current_action": "awake",
        }
    ]
    assert decay.satiety_calls[0]["current_action"] == "awake"
    assert db.saved[-1].last_tick_at == 4600.0
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_segments_sleeping_until_action_until_then_awake(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            energy=20,
            satiety=80,
            current_action="sleeping",
            action_until=8200.0,
            last_sleep_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 87400.0)
    await agent.start()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert snapshot.last_tick_at == 87400.0
    assert snapshot.active_sleep_record_id is None
    assert snapshot.energy == pytest.approx(18.0)
    assert [call["current_action"] for call in decay.energy_calls] == ["sleeping", "awake"]
    assert [call["elapsed_hours"] for call in decay.energy_calls] == [2.0, 22.0]
    assert any(
        log.get("event") == "action_finished"
        and log.get("previous_action") == "sleeping"
        for log in db.logs
    )
    assert db.sleep_updates == []
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_logs_offline_reconcile_after_crossing_action_until(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            current_action="sleeping",
            action_until=1600.0,
            energy=20,
            last_sleep_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 5200.0)
    await agent.start()

    reconcile_log = next(
        log for log in db.logs if log.get("event") == "startup_reconciled"
    )
    assert reconcile_log["reconcile_source"] == "offline_reconcile"
    assert reconcile_log["previous_action"] == "sleeping"
    assert reconcile_log["current_action"] == "awake"
    assert reconcile_log["previous_action_until"] == 1600.0
    assert reconcile_log["current_action_until"] is None
    assert reconcile_log["final"]["current_action"] == "awake"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_finishes_persisted_active_sleep_record_after_restart(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            energy=20,
            satiety=80,
            current_action="sleeping",
            action_until=8200.0,
            active_sleep_record_id="sleep_restart",
            last_sleep_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 87400.0)
    await agent.start()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert snapshot.active_sleep_record_id is None
    assert db.sleep_updates[0][0] == "sleep_restart"
    assert db.sleep_updates[0][1]["ended_at"] == 8200.0
    assert db.sleep_updates[0][1]["status"] == "finished"
    assert db.sleep_updates[0][1]["finish_reason"] == "action_until"
    assert db.sleep_updates[0][1]["recovery_source"] == "offline_reconcile"
    assert db.saved[-1].active_sleep_record_id is None
    await agent.shutdown()


@pytest.mark.asyncio
async def test_wakeup_after_restart_reconcile_does_not_restore_energy_twice(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(
        PersonaState(
            energy=20,
            satiety=80,
            current_action="sleeping",
            action_until=8200.0,
            active_sleep_record_id="sleep_restart",
            last_sleep_at=1000.0,
            last_tick_at=1000.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 4600.0)
    await agent.start()
    energy_after_start = agent.get_state_snapshot().energy

    await agent.on_wakeup("manual")

    snapshot = agent.get_state_snapshot()
    assert snapshot.energy == energy_after_start
    assert len(decay.energy_calls) == 1
    assert decay.energy_calls[0]["current_action"] == "sleeping"
    assert decay.energy_calls[0]["elapsed_hours"] == 1.0
    assert db.sleep_updates[0][0] == "sleep_restart"
    assert db.sleep_updates[0][1]["ended_at"] == 4600.0
    assert db.sleep_updates[0][1]["status"] == "finished"
    assert db.sleep_updates[0][1]["wakeup_reason"] == "manual"
    assert db.sleep_updates[0][1]["recovery_source"] == "fallback_formula"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_accepts_old_state_without_active_record_id(monkeypatch):
    old_state = {
        "energy": 20,
        "satiety": 80,
        "current_action": "sleeping",
        "action_until": 8200.0,
        "last_sleep_at": 1000.0,
        "last_tick_at": 1000.0,
    }
    db = FakeDB(old_state)
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool", satiety="tool"), decay=LinearDecay())

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 87400.0)
    await agent.start()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert snapshot.active_sleep_record_id is None
    assert db.sleep_updates == []
    await agent.shutdown()



