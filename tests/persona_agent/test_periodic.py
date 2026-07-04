from __future__ import annotations

import pytest

import agents.persona_agent as persona_agent_mod
from mind import PersonaState
from tests.persona_agent.helpers import (
    FailingConsolidation,
    FakeConsolidation,
    FakeDB,
    FakeDecay,
    FakeProvider,
    LinearDecay,
    _agent,
    _pm_cfg,
)


@pytest.mark.asyncio
async def test_periodic_tick_disabled_noop_and_tool_decay_collapse(monkeypatch):
    disabled_decay = FakeDecay()
    disabled_db = FakeDB(PersonaState(energy=10, satiety=20, last_tick_at=1000))
    disabled = _agent(disabled_db, pm_cfg=_pm_cfg(), decay=disabled_decay)

    await disabled.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 4600.0)
    await disabled.periodic_tick()

    assert disabled.get_state_snapshot().energy == 10
    assert disabled.get_state_snapshot().satiety == 20
    assert disabled_decay.energy_calls == []
    assert disabled_decay.satiety_calls == []
    await disabled.shutdown()

    tool_decay = FakeDecay(collapse=True)
    tool_db = FakeDB(
        PersonaState(
            energy=1,
            satiety=40,
            mood=70,
            last_tick_at=1000,
            energy_critical_since=900,
        )
    )
    tool = _agent(
        tool_db,
        pm_cfg=_pm_cfg(energy="tool", satiety="tool"),
        decay=tool_decay,
    )

    await tool.start()
    await tool.periodic_tick()

    snapshot = tool.get_state_snapshot()
    assert tool_decay.energy_calls[0]["mode"] == "tool"
    assert tool_decay.satiety_calls[0]["mode"] == "tool"
    assert snapshot.energy == 0
    assert snapshot.satiety == 33
    assert snapshot.current_action == "collapsing"
    assert snapshot.mood == 50
    await tool.shutdown()


@pytest.mark.asyncio
async def test_periodic_tick_calls_llm_and_records_operation(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2000.0)
    db = FakeDB(PersonaState(mood=60, social_need=50))
    provider = FakeProvider(['{"mood_delta": 2, "latest_monologue": "后台整理了一下状态。"}'])
    usage_calls = []
    statuses = []
    agent = _agent(db, provider=provider, status_callback=statuses.append)

    async def record_usage(usage, metadata):
        usage_calls.append((usage, metadata))

    agent.usage_recorder = record_usage

    await agent.start()
    await agent.periodic_tick()

    snapshot = agent.get_state_snapshot()
    assert snapshot.mood == 62
    assert snapshot.latest_monologue == "后台整理了一下状态。"
    assert len(provider.calls) == 1
    prompt = provider.calls[0]["messages"][1]["content"]
    assert "social_need 表示社交未满足度" in prompt
    assert "0-5 非常罕见" in prompt
    assert "10-25 是被回应、被关心、聊天满足后的常见低位" in prompt
    assert "effects、profiles、relationships、todos、cues 必须是 JSON 数组" in prompt
    assert "effects[] 用于会持续一段时间的临时情绪、身体感、语气倾向、行动倾向或关系余韵" in prompt
    assert "cues[] 用于对当前会话或近期互动有用" in prompt
    assert "todos[] 用于之后需要执行、检查、提醒或收尾的具体事项" in prompt
    assert "traits 必须是 JSON 字符串数组" in prompt
    assert usage_calls[0][1]["operation"] == "persona_periodic"
    assert db.logs[-1]["event"] == "periodic_tick"
    assert db.logs[-1]["fallback"] is False
    assert ("thinking", "人格定时维护中") in [
        (item["state"], item["text"]) for item in statuses
    ]
    assert statuses[-1]["state"] == "idle"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_tick_accepts_empty_object_profiles_and_relationships(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2000.0)
    db = FakeDB(PersonaState(mood=60, social_need=50))
    provider = FakeProvider(
        [
            """
            {
              "mood_delta": 1,
              "profiles": {},
              "relationships": {}
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.periodic_tick()

    snapshot = agent.get_state_snapshot()
    assert snapshot.mood == 61
    assert db.profiles == {}
    assert len(provider.calls) == 1
    assert db.logs[-1]["event"] == "periodic_tick"
    assert db.logs[-1]["fallback"] is False
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_tick_accepts_single_objects_for_plural_update_fields(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 2000.0)
    db = FakeDB(PersonaState(mood=60, social_need=50))
    provider = FakeProvider(
        [
            """
            {
              "effects": {
                "name": "后台稳定",
                "effect_type": "mood",
                "intensity": 15,
                "prompt_hint": "状态更平稳",
                "source_detail": "periodic",
                "expires_at": 9999999999
              },
              "profiles": {
                "user_id": "u_periodic_profile",
                "display_name": "周期画像",
                "summary": "后台整理出的稳定信息",
                "affinity": 55
              },
              "relationships": {
                "user_id": "u_periodic_relationship",
                "display_name": "周期关系",
                "summary": "后台关系印象",
                "affinity_delta": 2,
                "reason": "periodic"
              },
              "todos": {
                "title": "后台确认状态",
                "reason": "周期维护",
                "priority": 3,
                "scope": "persona"
              },
              "cues": {
                "summary": "后台维护线索",
                "cue_type": "conversation",
                "expires_at": 9999999999
              }
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.periodic_tick()

    assert len(provider.calls) == 1
    assert db.logs[-1]["event"] == "periodic_tick"
    assert db.logs[-1]["fallback"] is False
    assert db.effects[0].name == "后台稳定"
    assert db.todos[0].title == "后台确认状态"
    assert db.cues[0].summary == "后台维护线索"
    assert db.profiles["u_periodic_profile"].summary == "后台整理出的稳定信息"
    assert db.profiles["u_periodic_relationship"].summary == "后台关系印象"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_tick_mood_drifts_back_before_llm_override(monkeypatch):
    decay = LinearDecay()
    db = FakeDB(PersonaState(mood=99.0, last_tick_at=1000.0))
    provider = FakeProvider(["{}"])
    agent = _agent(db, provider=provider, pm_cfg=_pm_cfg(energy="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 4600.0)
    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 8200.0)
    await agent.periodic_tick()

    assert agent.get_state_snapshot().mood == pytest.approx(98.5)
    await agent.shutdown()

    override_db = FakeDB(PersonaState(mood=99.0, last_tick_at=1000.0))
    override_provider = FakeProvider(['{"mood": 97}'])
    override = _agent(
        override_db,
        provider=override_provider,
        pm_cfg=_pm_cfg(energy="tool"),
        decay=LinearDecay(),
    )
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 4600.0)
    await override.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 8200.0)
    await override.periodic_tick()

    assert override.get_state_snapshot().mood == 97
    await override.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["eating", "sleeping"])
async def test_is_resting_false_when_action_until_expired(monkeypatch, action):
    db = FakeDB(PersonaState(current_action=action, action_until=100.0))
    agent = _agent(db)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await agent.start()

    assert agent.is_resting() is False
    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_tick_finishes_expired_collapse_without_immediate_recollapse(monkeypatch):
    decay = FakeDecay(collapse=True)
    db = FakeDB(
        PersonaState(
            current_action="collapsing",
            action_until=100.0,
            energy=0,
            energy_critical_since=10.0,
            last_tick_at=100.0,
        )
    )
    agent = _agent(db, pm_cfg=_pm_cfg(energy="tool"), decay=decay)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 50.0)
    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await agent.periodic_tick()

    snapshot = agent.get_state_snapshot()
    assert snapshot.current_action == "awake"
    assert snapshot.action_until is None
    assert agent.is_resting() is False
    assert any(
        log.get("event") == "action_finished"
        and log.get("previous_action") == "collapsing"
        for log in db.logs
    )
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_daily_consolidation_uses_real_run_signature(monkeypatch):
    now = 1000.0
    fallback_hour = persona_agent_mod.datetime.fromtimestamp(now).hour
    consolidation = FakeConsolidation()
    agent = _agent(
        FakeDB(PersonaState(mood=61)),
        pm_cfg=_pm_cfg(daily_fallback_hour=fallback_hour),
        consolidation=consolidation,
    )

    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: now)
    await agent.periodic_tick()
    await agent.periodic_tick()

    assert len(consolidation.calls) == 1
    assert isinstance(consolidation.calls[0]["state_snapshot"], PersonaState)
    assert consolidation.calls[0]["state_snapshot"].mood == 61
    assert consolidation.calls[0]["recent_history"] == []
    assert consolidation.calls[0]["sleep_type"] == "daily"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_daily_consolidation_skips_existing_daily_trajectory_after_restart(
    monkeypatch,
):
    now = 1000.0
    date_key = persona_agent_mod.datetime.fromtimestamp(now).date().isoformat()
    fallback_hour = persona_agent_mod.datetime.fromtimestamp(now).hour
    db = FakeDB(PersonaState(mood=61))
    db.trajectories.append({"date": date_key, "summary": "已整理"})
    consolidation = FakeConsolidation()
    agent = _agent(
        db,
        pm_cfg=_pm_cfg(daily_fallback_hour=fallback_hour),
        consolidation=consolidation,
    )

    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: now)
    await agent.periodic_tick()

    assert consolidation.calls == []
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_daily_consolidation_skips_existing_daily_monologue_after_restart(
    monkeypatch,
):
    now = 1000.0
    fallback_hour = persona_agent_mod.datetime.fromtimestamp(now).hour
    db = FakeDB(PersonaState(mood=61))
    db.monologues.append(
        {"text": "今天已整理", "sleep_type": "daily", "created_at": now}
    )
    consolidation = FakeConsolidation()
    agent = _agent(
        db,
        pm_cfg=_pm_cfg(daily_fallback_hour=fallback_hour),
        consolidation=consolidation,
    )

    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: now)
    await agent.periodic_tick()

    assert consolidation.calls == []
    await agent.shutdown()


@pytest.mark.asyncio
async def test_periodic_daily_consolidation_allows_next_day(monkeypatch):
    now = 1000.0 + 24 * 3600.0
    previous_date = persona_agent_mod.datetime.fromtimestamp(now - 24 * 3600.0).date().isoformat()
    fallback_hour = persona_agent_mod.datetime.fromtimestamp(now).hour
    db = FakeDB(PersonaState(mood=61))
    db.trajectories.append({"date": previous_date, "summary": "昨天已整理"})
    consolidation = FakeConsolidation()
    agent = _agent(
        db,
        pm_cfg=_pm_cfg(daily_fallback_hour=fallback_hour),
        consolidation=consolidation,
    )

    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: now)
    await agent.periodic_tick()

    assert len(consolidation.calls) == 1
    assert consolidation.calls[0]["sleep_type"] == "daily"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_failed_daily_consolidation_can_retry_same_day(monkeypatch):
    now = 1000.0
    fallback_hour = persona_agent_mod.datetime.fromtimestamp(now).hour
    consolidation = FailingConsolidation()
    agent = _agent(
        FakeDB(PersonaState(mood=61)),
        pm_cfg=_pm_cfg(daily_fallback_hour=fallback_hour),
        consolidation=consolidation,
    )

    await agent.start()
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: now)
    await agent.periodic_tick()
    await agent.periodic_tick()

    assert len(consolidation.calls) == 2
    await agent.shutdown()
