from __future__ import annotations

from dataclasses import replace

import pytest

import agents.persona_agent as persona_agent_mod
from agents.persona_agent import PersonaAgent
from app_config.schema import AgentConfig, PersonaManagementConfig
from mind import Cue, DecayEngine, Effect, PersonaState, Todo, UserProfile
from providers.base import CompletionResult, Usage


class FakeDB:
    def __init__(self, state: PersonaState | dict | None = None) -> None:
        self.state = state or PersonaState()
        self.loaded = False
        self.saved: list[PersonaState] = []
        self.logs: list[dict] = []
        self.effects: list[Effect] = []
        self.todos: list[Todo] = []
        self.closed_todos: dict[str, dict] = {}
        self.cues: list[Cue] = []
        self.profiles: dict[str, UserProfile] = {}
        self.monologues: list[dict] = []
        self.trajectories: list[dict] = []
        self.sleep_records: list[dict] = []
        self.sleep_updates: list[tuple[str, dict]] = []
        self.eat_records: list[dict] = []
        self.eat_updates: list[tuple[str, dict]] = []
        self.expired_effect_calls = 0
        self.expired_cue_calls = 0
        self.missed_todo_calls = 0
        self.missed_todo_updates = 0

    async def load(self) -> None:
        self.loaded = True

    async def get_state(self) -> PersonaState | dict:
        return self.state

    async def save_state(self, state: PersonaState) -> None:
        snapshot = replace(state)
        self.state = snapshot
        self.saved.append(replace(snapshot))

    async def append_state_log(self, entry: dict) -> int:
        self.logs.append(entry)
        return len(self.logs)

    async def get_active_effects(self, now: float) -> list[Effect]:
        return [effect for effect in self.effects if effect.expires_at > now]

    async def add_effect(self, effect: Effect) -> str:
        self.effects = [item for item in self.effects if item.id != effect.id]
        self.effects.append(effect)
        return effect.id

    async def expire_effects(self, now: float) -> int:
        before = len(self.effects)
        self.effects = [effect for effect in self.effects if effect.expires_at > now]
        self.expired_effect_calls += 1
        return before - len(self.effects)

    async def get_todos(self, include_completed: bool = True) -> list:
        if include_completed:
            return [*self.todos, *self.closed_todos.values()]
        return self.todos

    async def upsert_todo(self, todo: Todo | dict) -> str:
        todo_id = todo["id"] if isinstance(todo, dict) else todo.id
        self.todos = [item for item in self.todos if item.id != todo_id]
        if isinstance(todo, dict) and (
            todo.get("completed")
            or str(todo.get("status") or "").lower()
            in {"completed", "complete", "done", "finished", "closed", "cancelled", "canceled", "missed"}
        ):
            self.closed_todos[str(todo_id)] = dict(todo)
            return str(todo_id)
        if isinstance(todo, dict):
            todo = Todo(**{key: value for key, value in todo.items() if key in Todo.__dataclass_fields__})
        self.todos.append(todo)
        return todo.id

    async def mark_expired_todos_missed(self, now: float | None = None) -> int:
        self.missed_todo_calls += 1
        before = len(self.todos)
        kept: list[Todo] = []
        now_value = 0.0 if now is None else float(now)
        for todo in self.todos:
            if persona_agent_mod._todo_is_expired(todo, now_value):
                self.closed_todos[todo.id] = {
                    "id": todo.id,
                    "title": todo.title,
                    "reason": todo.reason,
                    "priority": todo.priority,
                    "scope": todo.scope,
                    "created_at": todo.created_at,
                    "expires_at": todo.expires_at,
                    "status": "missed",
                    "completed": True,
                }
                continue
            kept.append(todo)
        self.todos = kept
        updated = before - len(kept)
        self.missed_todo_updates += updated
        return updated

    async def get_cues(self, now: float) -> list[Cue]:
        return [cue for cue in self.cues if cue.expires_at > now]

    async def upsert_cue(self, cue: Cue) -> str:
        self.cues = [item for item in self.cues if item.id != cue.id]
        self.cues.append(cue)
        return cue.id

    async def expire_cues(self, now: float) -> int:
        before = len(self.cues)
        self.cues = [cue for cue in self.cues if cue.expires_at > now]
        self.expired_cue_calls += 1
        return before - len(self.cues)

    async def all_profiles(self) -> list[UserProfile]:
        return list(self.profiles.values())

    async def upsert_profile(self, profile: UserProfile) -> str:
        self.profiles[profile.user_id] = profile
        return profile.user_id

    async def add_monologue(self, monologue: dict) -> int:
        self.monologues.append(monologue)
        return len(self.monologues)

    async def recent_monologues(self, limit: int = 20) -> list[dict]:
        return self.monologues[-limit:]

    async def add_trajectory(self, trajectory: dict) -> int:
        self.trajectories.append(trajectory)
        return len(self.trajectories)

    async def recent_trajectories(self, limit: int = 20) -> list[dict]:
        return self.trajectories[-limit:]

    async def add_sleep_record(self, record: dict) -> str:
        self.sleep_records.append(record)
        return record["id"]

    async def update_sleep_record(self, record_id: str, updates: dict) -> bool:
        self.sleep_updates.append((record_id, updates))
        return True

    async def add_eat_record(self, record: dict) -> int:
        self.eat_records.append(record)
        return len(self.eat_records)

    async def update_eat_record(self, record_id: str, updates: dict) -> bool:
        self.eat_updates.append((record_id, updates))
        return True


class FakeProvider:
    name = "fake_provider"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_completion(self, messages: list[dict], **kwargs) -> CompletionResult:
        self.calls.append({"messages": messages, **kwargs})
        return CompletionResult(
            content=self.responses.pop(0),
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class FakeDecay:
    def __init__(self, *, collapse: bool = False) -> None:
        self.collapse = collapse
        self.energy_calls: list[dict] = []
        self.satiety_calls: list[dict] = []
        self.collapse_calls: list[dict] = []

    def tick_energy(
        self,
        current,
        elapsed_hours,
        current_action,
        hours_since_sleep,
        now_hour,
        *,
        mode,
    ):
        self.energy_calls.append(
            {
                "current": current,
                "elapsed_hours": elapsed_hours,
                "mode": mode,
                "current_action": current_action,
            }
        )
        return 0.0 if self.collapse else 44.0

    def tick_satiety(
        self,
        current,
        elapsed_hours,
        current_action,
        hours_since_eat,
        now_hour,
        *,
        mode,
    ):
        self.satiety_calls.append({"current": current, "mode": mode})
        return 33.0

    def check_collapse(self, energy, energy_critical_since, now, grace_seconds, *, mode):
        self.collapse_calls.append(
            {
                "energy": energy,
                "energy_critical_since": energy_critical_since,
                "mode": mode,
            }
        )
        return self.collapse


class LinearDecay:
    def __init__(self, *, collapse: bool = False) -> None:
        self.collapse = collapse
        self.energy_calls: list[dict] = []
        self.satiety_calls: list[dict] = []
        self.collapse_calls: list[dict] = []

    def tick_energy(
        self,
        current,
        elapsed_hours,
        current_action,
        hours_since_sleep,
        now_hour,
        *,
        mode,
    ):
        self.energy_calls.append(
            {
                "current": current,
                "elapsed_hours": elapsed_hours,
                "mode": mode,
                "current_action": current_action,
            }
        )
        if mode == "disabled":
            return current
        if current_action == "sleeping":
            return current + elapsed_hours * 10.0
        if current_action == "eating":
            return current + elapsed_hours * 2.0
        return current - elapsed_hours

    def tick_satiety(
        self,
        current,
        elapsed_hours,
        current_action,
        hours_since_eat,
        now_hour,
        *,
        mode,
    ):
        self.satiety_calls.append(
            {
                "current": current,
                "elapsed_hours": elapsed_hours,
                "mode": mode,
                "current_action": current_action,
            }
        )
        if mode == "disabled":
            return current
        if current_action == "eating":
            return current + elapsed_hours * 60.0
        return current - elapsed_hours * 2.0

    def check_collapse(self, energy, energy_critical_since, now, grace_seconds, *, mode):
        self.collapse_calls.append(
            {
                "energy": energy,
                "energy_critical_since": energy_critical_since,
                "mode": mode,
            }
        )
        return self.collapse


class FakeConsolidation:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(
        self,
        state_snapshot: PersonaState,
        recent_history: list,
        sleep_type: str,
    ) -> None:
        self.calls.append(
            {
                "state_snapshot": state_snapshot,
                "recent_history": recent_history,
                "sleep_type": sleep_type,
            }
        )


class FailingConsolidation(FakeConsolidation):
    async def run(
        self,
        state_snapshot: PersonaState,
        recent_history: list,
        sleep_type: str,
    ) -> None:
        await super().run(state_snapshot, recent_history, sleep_type)
        raise RuntimeError("provider failed")


class WritingConsolidation(FakeConsolidation):
    def __init__(self, db: FakeDB, tomorrow_monologue: str) -> None:
        super().__init__()
        self.db = db
        self.tomorrow_monologue = tomorrow_monologue

    async def run(
        self,
        state_snapshot: PersonaState,
        recent_history: list,
        sleep_type: str,
    ) -> dict:
        await super().run(state_snapshot, recent_history, sleep_type)
        await self.db.add_monologue(
            {
                "text": self.tomorrow_monologue,
                "mood": state_snapshot.mood,
                "sleep_type": sleep_type,
                "created_at": persona_agent_mod.time.time(),
            }
        )
        updated = replace(state_snapshot)
        updated.latest_monologue = self.tomorrow_monologue
        updated.last_monologue_at = persona_agent_mod.time.time()
        await self.db.save_state(updated)
        return {"tomorrow_monologue": self.tomorrow_monologue}


def _cfg() -> AgentConfig:
    return AgentConfig(provider="fake", model="unit-test-model")


def _pm_cfg(
    *,
    energy: str = "disabled",
    satiety: str = "disabled",
    daily_fallback_hour: int = 4,
) -> PersonaManagementConfig:
    return PersonaManagementConfig(
        persona_agent={"timer_interval_minutes": 999},
        physiology={
            "energy": {"mode": energy},
            "satiety": {"mode": satiety},
        },
        consolidation={"daily_fallback_hour": daily_fallback_hour},
    )


def _agent(
    db: FakeDB,
    provider: FakeProvider | None = None,
    pm_cfg: PersonaManagementConfig | None = None,
    decay=None,
    consolidation=None,
    subconscious_starter=None,
    status_callback=None,
) -> PersonaAgent:
    cfg = pm_cfg or _pm_cfg()
    return PersonaAgent(
        db,
        provider or FakeProvider([]),
        _cfg(),
        cfg,
        None,
        decay or DecayEngine(cfg.physiology, None),
        consolidation,
        {"name": "unit"},
        status_callback=status_callback,
        subconscious_starter=subconscious_starter,
    )


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
async def test_after_turn_valid_json_updates_state_and_runtime_records():
    db = FakeDB(PersonaState())
    statuses = []
    provider = FakeProvider(
        [
            """
            {
              "mood": 81,
              "social_need": 22,
              "latest_monologue": "今天记得主动问候。",
              "effect": {
                "id": "effect_1",
                "name": "安心",
                "effect_type": "mood",
                "intensity": 2.5,
                "prompt_hint": "语气更安心",
                "source_detail": "unit",
                "expires_at": 9999999999
              },
              "profile": {
                "display_name": "小林",
                "affinity": 0.8,
                "summary": "喜欢短句反馈",
                "traits": ["短句"],
                "interaction_count": 3
              },
              "todo": {
                "id": "todo_1",
                "title": "稍后确认状态",
                "reason": "对方刚提到疲惫",
                "priority": 2,
                "scope": "private:u1"
              },
              "cue": {
                "id": "cue_1",
                "summary": "对方可能需要休息提醒",
                "expires_at": 9999999999
              }
            }
            """
        ]
    )
    agent = _agent(db, provider=provider, status_callback=statuses.append)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "聊到今天很累")

    snapshot = agent.get_state_snapshot()
    assert snapshot.mood == 81
    assert snapshot.social_need == 22
    assert snapshot.latest_monologue == "今天记得主动问候。"
    assert db.effects[0].prompt_hint == "语气更安心"
    assert db.profiles["u1"].summary == "喜欢短句反馈"
    assert db.todos[0].title == "稍后确认状态"
    assert db.cues[0].conversation_id == "private:u1"
    assert db.monologues[0]["text"] == "今天记得主动问候。"
    assert db.logs[-1]["event"] == "after_turn"
    prompt = provider.calls[0]["messages"][1]["content"]
    assert "助手、assistant、当前回复、角色刚说的话，都是当前人格自己的发言" in prompt
    assert "latest_monologue 必须是一人称内心状态" in prompt
    assert "稳定偏好、称呼、长期习惯、关系变化" in prompt
    assert "profile 字段包括 user_id、display_name、summary、traits、affinity" in prompt
    assert "私聊可省略 user_id 由系统推断，群聊必须带 user_id" in prompt
    assert "affinity 是 0-100 的绝对亲近度" in prompt
    assert "不是 0-1，也不是 1-10" in prompt
    assert "0=陌生/排斥" in prompt
    assert "30=疏离" in prompt
    assert "50=普通熟人" in prompt
    assert "70=信任友好" in prompt
    assert "85=亲近在意" in prompt
    assert "95=核心亲密关系" in prompt
    assert "relationship/affinity_delta 是本轮增减分" in prompt
    assert "普通一轮互动通常 -5 到 +5" in prompt
    assert "强烈事件可更大，但要写 reason" in prompt
    assert "优先用 affinity_delta" in prompt
    assert "不要随意用低 absolute affinity 覆盖" in prompt
    assert "profile 事实仍不要塞短期情绪、一次性事件或临时状态" in prompt
    assert "social_need 表示社交未满足度" in prompt
    assert "被关心、亲密互动、有效交流后通常下降" in prompt
    assert "想继续聊、社交兴奋或亲近感不要挤到 social_need" in prompt
    assert "每轮最多新增 1 条 todo" in prompt
    assert "普通情绪、关系画像、观察线索、已发生事件、泛泛改善建议都不要建 todo" in prompt
    assert [(item["state"], item["text"]) for item in statuses] == [
        ("thinking", "人格状态更新中"),
        ("idle", "人格状态更新完成"),
    ]
    context = agent.get_context_for_chat("private:u1")
    assert "语气更安心" in context
    assert "稍后确认状态" in context
    assert "对方可能需要休息提醒" in context
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_relationship_update_and_plain_interaction_touch(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    db = FakeDB(PersonaState())
    db.profiles["u1"] = UserProfile(
        user_id="u1",
        display_name="旧名",
        affinity=20.0,
        summary="旧印象",
        traits=["稳重"],
        interaction_count=2,
        last_interaction_at=900.0,
    )
    provider = FakeProvider(
        [
            """
            {
              "relationship": {
                "affinity_delta": 12,
                "summary": "刚刚有一次轻松亲近的互动",
                "traits": ["会主动关心"]
              }
            }
            """,
            "{}"
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1", "nickname": "小林"}], "用户关心了一句")

    profile = db.profiles["u1"]
    assert profile.display_name == "小林"
    assert profile.affinity == 32.0
    assert profile.summary == "刚刚有一次轻松亲近的互动"
    assert profile.traits == ["会主动关心"]
    assert profile.interaction_count == 3
    assert profile.last_interaction_at == 1000.0

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1100.0)
    await agent.after_turn("system:global", [{"user_id": "u2", "nickname": "阿泉"}], "系统轮实际面向 u2")

    assert db.profiles["u2"].display_name == "阿泉"
    assert db.profiles["u2"].interaction_count == 1
    assert db.profiles["u2"].last_interaction_at == 1100.0
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_accepts_level_words_for_intensity_and_priority():
    db = FakeDB(PersonaState())
    provider = FakeProvider(
        [
            """
            {
              "effect": {
                "id": "effect_level",
                "name": "被理解",
                "effect_type": "buff",
                "intensity": "medium",
                "prompt_hint": "更愿意继续回应",
                "source_detail": "unit",
                "expires_at": 9999999999
              },
              "todo": {
                "id": "todo_level",
                "title": "稍后补一句关心",
                "priority": "medium"
              }
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "对方表达了压力")

    assert len(provider.calls) == 1
    assert db.effects[0].intensity == 50.0
    assert db.todos[0].priority == 5
    assert db.logs[-1]["fallback"] is False
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_accepts_iso_expires_at_for_cue():
    db = FakeDB(PersonaState())
    provider = FakeProvider(
        [
            """
            {
              "mood_delta": 1,
              "cue": {
                "id": "cue_iso",
                "summary": "三天后可能有安排",
                "expires_at": "2026-06-16T00:00:00Z"
              }
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户提到三天后的安排")

    assert len(provider.calls) == 1
    assert db.logs[-1]["fallback"] is False
    assert db.cues[0].id == "cue_iso"
    assert db.cues[0].summary == "三天后可能有安排"
    assert isinstance(db.cues[0].expires_at, float)
    assert db.cues[0].expires_at == pytest.approx(1781568000.0)
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_filters_empty_duplicate_and_extra_new_todos():
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_existing",
            title="已经安排复诊",
            reason="unit",
            priority=2,
            scope="private:u1",
            created_at=10.0,
        ),
        Todo(
            id="todo_water",
            title="提醒喝水",
            reason="unit",
            priority=1,
            scope="private:u1",
            created_at=11.0,
        ),
    ]
    provider = FakeProvider(
        [
            """
            {
              "todos": [
                {"title": "   ", "scope": "private:u1", "priority": 9},
                {"title": "已经安排复诊。", "scope": "private:u1", "priority": 9},
                {
                  "id": "todo_existing",
                  "title": "确认复诊时间",
                  "scope": "private:u1",
                  "priority": 6
                },
                {
                  "id": "todo_model_new_id",
                  "title": "提醒喝水",
                  "scope": "private:u1",
                  "priority": 8
                },
                {
                  "title": "明早叫醒用户",
                  "scope": "private:u1",
                  "priority": 8,
                  "expires_at": 9999999999
                },
                {
                  "title": "晚上提醒吃饭",
                  "scope": "private:u1",
                  "priority": 7,
                  "expires_at": 9999999999
                }
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户说明早需要叫醒")

    todos_by_title = {todo.title: todo for todo in db.todos}
    assert set(todos_by_title) == {"确认复诊时间", "提醒喝水", "明早叫醒用户"}
    assert todos_by_title["确认复诊时间"].id == "todo_existing"
    assert todos_by_title["确认复诊时间"].priority == 6
    assert todos_by_title["提醒喝水"].id == "todo_water"
    assert todos_by_title["明早叫醒用户"].scope == "private:u1"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_can_close_todos_and_removes_from_context():
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_completed",
            title="提醒复诊",
            reason="用户要求",
            priority=6,
            scope="private:u1",
            created_at=10.0,
            expires_at=9999999999,
        ),
        Todo(
            id="todo_done",
            title="提醒喝水",
            reason="生理提醒",
            priority=3,
            scope="private:u1",
            created_at=11.0,
            expires_at=9999999999,
        ),
        Todo(
            id="todo_cancelled",
            title="提醒订票",
            reason="用户临时取消",
            priority=4,
            scope="private:u1",
            created_at=12.0,
            expires_at=9999999999,
        ),
        Todo(
            id="todo_open",
            title="保留的待办",
            reason="仍需执行",
            priority=2,
            scope="private:u1",
            created_at=13.0,
            expires_at=9999999999,
        ),
    ]
    provider = FakeProvider(
        [
            """
            {
              "todos": [
                {"id": "todo_completed", "completed": true},
                {"id": "todo_done", "done": true},
                {"id": "todo_cancelled", "status": "cancelled"}
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户说这些提醒已经不用了")

    assert [todo.id for todo in db.todos] == ["todo_open"]
    assert set(db.closed_todos) == {"todo_completed", "todo_done", "todo_cancelled"}
    assert db.closed_todos["todo_completed"]["status"] == "completed"
    assert db.closed_todos["todo_done"]["status"] == "completed"
    assert db.closed_todos["todo_cancelled"]["status"] == "cancelled"
    context = agent.get_context_for_chat("private:u1")
    proactive_titles = [todo.title for todo in agent.get_todos_for_proactive()]
    assert proactive_titles == ["保留的待办"]
    assert "保留的待办" in context
    assert "提醒复诊" not in context
    assert "提醒喝水" not in context
    assert "提醒订票" not in context
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_partial_todo_update_preserves_metadata():
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_existing",
            title="原始标题",
            reason="保留原因",
            priority=7,
            scope="private:u1",
            created_at=10.0,
            expires_at=9999999999,
        )
    ]
    provider = FakeProvider(
        [
            """
            {
              "todos": [
                {"id": "todo_existing", "title": "更新标题"}
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "只改标题")

    assert len(db.todos) == 1
    updated = db.todos[0]
    assert updated.id == "todo_existing"
    assert updated.title == "更新标题"
    assert updated.reason == "保留原因"
    assert updated.priority == 7
    assert updated.scope == "private:u1"
    assert updated.created_at == 10.0
    assert updated.expires_at == 9999999999
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_marks_expired_todos_missed_and_filters_from_context(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_expired",
            title="已经过期",
            reason="unit",
            priority=9,
            scope="private:u1",
            created_at=1.0,
            expires_at=100.0,
        ),
        Todo(
            id="todo_open",
            title="仍然有效",
            reason="unit",
            priority=1,
            scope="private:u1",
            created_at=2.0,
            expires_at=9999999999,
        ),
    ]
    agent = _agent(db)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await agent.start()
    assert [todo.title for todo in agent.get_todos_for_proactive()] == ["仍然有效"]
    assert "已经过期" not in agent.get_context_for_chat("private:u1")
    assert "todo_expired" in db.closed_todos
    assert db.closed_todos["todo_expired"]["status"] == "missed"
    assert db.closed_todos["todo_expired"]["completed"] is True
    assert [todo.id for todo in await db.get_todos(include_completed=False)] == ["todo_open"]

    audit_todos = await db.get_todos(include_completed=True)
    audit_ids = {todo.id if isinstance(todo, Todo) else todo["id"] for todo in audit_todos}
    assert audit_ids == {"todo_expired", "todo_open"}

    await agent.periodic_tick()
    await agent.periodic_tick()
    assert db.missed_todo_updates == 1
    assert [todo.title for todo in agent.get_todos_for_proactive()] == ["仍然有效"]
    await agent.shutdown()


@pytest.mark.asyncio
async def test_get_todos_for_proactive_sorts_before_context_limit(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id=f"todo_low_{index}",
            title=f"低优旧待办{index}",
            reason="unit",
            priority=1,
            scope="private:u1",
            created_at=float(index),
            expires_at=1000.0 + index,
        )
        for index in range(5)
    ]
    db.todos.append(
        Todo(
            id="todo_high_new",
            title="高优新待办",
            reason="unit",
            priority=10,
            scope="private:u1",
            created_at=999.0,
            expires_at=9999999999,
        )
    )
    agent = _agent(db)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 50.0)
    await agent.start()

    proactive_titles = [todo.title for todo in agent.get_todos_for_proactive()]
    context = agent.get_context_for_chat("private:u1")
    context_todos = context.split("- 待办: ", 1)[1].split("\n", 1)[0]

    assert proactive_titles[0] == "高优新待办"
    assert "高优新待办" in context_todos
    assert "低优旧待办4" not in context_todos
    await agent.shutdown()


@pytest.mark.asyncio
async def test_get_todos_for_proactive_keeps_unparseable_expires_at(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_unparseable",
            title="不可解析时间保守保留",
            reason="unit",
            priority=4,
            scope="private:u1",
            created_at=1.0,
            expires_at="not-a-time",  # type: ignore[arg-type]
        ),
        Todo(
            id="todo_expired",
            title="已过期待办",
            reason="unit",
            priority=9,
            scope="private:u1",
            created_at=2.0,
            expires_at=100.0,
        ),
    ]
    agent = _agent(db)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await agent.start()

    assert [todo.title for todo in agent.get_todos_for_proactive()] == [
        "不可解析时间保守保留"
    ]
    assert "不可解析时间保守保留" in agent.get_context_for_chat("private:u1")
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_malformed_retries_and_total_failure_falls_back():
    db = FakeDB(PersonaState(mood=50, social_need=50))
    provider = FakeProvider(["not json", '{"mood_delta": 5, "social_need_delta": -10}'])
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [], "summary")

    assert len(provider.calls) == 2
    assert agent.get_state_snapshot().mood == 55
    assert agent.get_state_snapshot().social_need == 40

    provider.responses = ["not json", "{bad"]
    await agent.after_turn("private:u1", [], "summary")

    assert len(provider.calls) == 4
    assert db.logs[-1]["fallback"] is True
    assert agent.get_state_snapshot().mood == 55
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
    assert usage_calls[0][1]["operation"] == "persona_periodic"
    assert db.logs[-1]["event"] == "periodic_tick"
    assert db.logs[-1]["fallback"] is False
    assert ("thinking", "人格定时维护中") in [
        (item["state"], item["text"]) for item in statuses
    ]
    assert statuses[-1]["state"] == "idle"
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
