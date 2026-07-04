from __future__ import annotations

from dataclasses import replace

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
        self.update_audits: list[dict] = []
        self.important: list[dict | str] = []
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

    async def remove_effects(self, ids) -> int:
        effect_ids = {str(item) for item in (ids if isinstance(ids, list | tuple | set) else [ids])}
        before = len(self.effects)
        self.effects = [effect for effect in self.effects if effect.id not in effect_ids]
        return before - len(self.effects)

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
            in {"completed", "complete", "done", "finished", "closed", "cancelled", "canceled", "deleted", "missed"}
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

    async def remove_cues(self, ids) -> int:
        cue_ids = {str(item) for item in (ids if isinstance(ids, list | tuple | set) else [ids])}
        before = len(self.cues)
        self.cues = [cue for cue in self.cues if cue.id not in cue_ids]
        return before - len(self.cues)

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

    async def append_update_audit(self, entry: dict) -> int:
        self.update_audits.append(entry)
        return len(self.update_audits)

    async def recent_update_audits(
        self,
        limit: int = 20,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict]:
        records = list(reversed(self.update_audits))
        if conversation_id is not None:
            records = [item for item in records if item.get("conversation_id") == conversation_id]
        if user_id is not None:
            records = [item for item in records if item.get("user_id") == user_id]
        return records[:limit]

    async def read_important(self, default=None):
        return list(self.important) if self.important else default

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
