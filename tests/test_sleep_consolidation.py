from __future__ import annotations

import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

import mind.consolidation as consolidation_mod
from mind import PersonaState
from mind.consolidation import SleepConsolidation
from providers.base import CompletionResult, Usage


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        model="sleep-model",
        temperature=0.2,
        top_p=0.9,
        max_tokens=512,
        reasoning=None,
        first_token_timeout_seconds=5.0,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "daily_trajectory": {"date": "2026-06-12", "summary": "今天整体平稳"},
        "tidy_todos": {
            "new": [
                {
                    "id": "todo_1",
                    "title": "明天复盘计划",
                    "reason": "睡前整理",
                    "priority": 2,
                    "scope": "persona",
                    "created_at": 1.0,
                }
            ]
        },
        "tidy_cues": {
            "new": [
                {
                    "id": "cue_1",
                    "cue_type": "conversation",
                    "summary": "继续跟进计划",
                    "conversation_id": "private:u1",
                    "created_at": 1.0,
                    "expires_at": 99.0,
                }
            ]
        },
        "persona_arc_adjustment": {"has_change": True, "event": "更愿意主动整理目标"},
        "consolidated_memories": [
            {
                "id": "mem_1",
                "content": "用户希望明天继续复盘计划",
                "scope": "global",
                "pinned": False,
            }
        ],
        "tomorrow_monologue": "明天醒来后先把计划理顺。",
    }
    payload.update(overrides)
    return payload


def _json_response(payload: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


class FakeProvider:
    name = "fake-provider"

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, messages: list[dict[str, Any]], **kwargs: Any) -> CompletionResult:
        self.calls.append({"messages": messages, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return CompletionResult(
            content=str(response),
            usage=Usage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
        )


class FakeDB:
    def __init__(self, state: PersonaState | dict | None = None) -> None:
        self.state: PersonaState | dict = state or PersonaState()
        self.saved: list[PersonaState | dict] = []
        self.trajectories: list[dict[str, Any]] = []
        self.arc_events: list[dict[str, Any]] = []
        self.important: list[Any] = [
            {"id": "mem_old", "content": "已有记忆", "scope": "global"}
        ]
        self.monologues: list[Any] = []
        self.todos: list[dict[str, Any]] = []
        self.cues: list[dict[str, Any]] = []

    async def add_trajectory(self, trajectory: dict[str, Any]) -> int:
        self.trajectories.append(trajectory)
        return len(self.trajectories)

    async def add_arc_event(self, event: dict[str, Any]) -> int:
        self.arc_events.append(event)
        return len(self.arc_events)

    async def read_important(self, default: Any = None) -> Any:
        return list(self.important) if self.important else default

    async def write_important(self, data: Any) -> None:
        self.important = list(data)

    async def add_monologue(self, monologue: Any) -> int:
        self.monologues.append(monologue)
        return len(self.monologues)

    async def get_state(self) -> PersonaState | dict:
        return self.state

    async def save_state(self, state: PersonaState | dict) -> None:
        if isinstance(state, PersonaState):
            self.state = replace(state)
            self.saved.append(replace(state))
            return
        self.state = dict(state)
        self.saved.append(dict(state))

    async def upsert_todo(self, todo: dict[str, Any]) -> str:
        self.todos.append(todo)
        return str(todo.get("id") or "")

    async def upsert_cue(self, cue: dict[str, Any]) -> str:
        self.cues.append(cue)
        return str(cue.get("id") or "")


@dataclass(slots=True)
class AgeProfileLike:
    bracket: str = "青少年"
    age: int = 16
    bedtime_hour: float = 22.0
    wakeup_hour: float = 6.5
    ideal_sleep_hours: float = 8.5
    monologue_style: str = "轻快"
    emotional_hint: str = "情绪波动更明显"
    social_hint: str = "需要回应感"


@pytest.mark.asyncio
async def test_sleep_consolidation_valid_json_writes_db_and_returns_dict():
    payload = _payload()
    provider = FakeProvider([_json_response(payload)])
    db = FakeDB()
    usage_records: list[tuple[Usage, dict[str, Any]]] = []

    async def record_usage(usage: Usage, metadata: dict[str, Any]) -> None:
        usage_records.append((usage, metadata))

    agent = SleepConsolidation(db, provider, _cfg(), None, usage_recorder=record_usage)

    result = await agent.run(
        {"mood": 72},
        [{"role": "user", "content": "明天继续复盘计划"}],
        "night",
    )

    assert result == payload
    assert db.trajectories == [payload["daily_trajectory"]]
    assert db.arc_events == [payload["persona_arc_adjustment"]]
    assert db.important == [
        {"id": "mem_old", "content": "已有记忆", "scope": "global"},
        payload["consolidated_memories"][0],
    ]
    assert db.monologues[0]["text"] == payload["tomorrow_monologue"]
    assert db.monologues[0]["mood"] == 72
    assert db.monologues[0]["sleep_type"] == "night"
    assert isinstance(db.monologues[0]["created_at"], float)
    assert db.todos == payload["tidy_todos"]["new"]
    assert db.cues == payload["tidy_cues"]["new"]

    call = provider.calls[0]
    assert call["model"] == "sleep-model"
    assert call["tools"] is None
    assert call["temperature"] == 0.2
    assert call["top_p"] == 0.9
    assert call["max_tokens"] == 512
    assert call["reasoning"] is None
    assert call["stream"] is True
    assert call["timeout"] == 90.0
    assert call["first_token_timeout"] == 10.0
    assert usage_records[0][1] == {
        "provider": "fake-provider",
        "model": "sleep-model",
        "agent": "睡眠整理",
        "operation": "sleep_consolidation",
        "sleep_type": "night",
    }


@pytest.mark.asyncio
async def test_sleep_consolidation_retries_once_after_malformed_json():
    payload = _payload(tomorrow_monologue="第二次成功。")
    provider = FakeProvider(["不是 JSON", json.dumps(payload, ensure_ascii=False)])
    db = FakeDB()
    agent = SleepConsolidation(db, provider, _cfg(), None)

    result = await agent.run({}, [], "nap")

    assert result == payload
    assert len(provider.calls) == 2
    assert db.trajectories == [payload["daily_trajectory"]]
    retry_messages = provider.calls[1]["messages"]
    assert "上一次输出无法解析" in retry_messages[-1]["content"]


@pytest.mark.asyncio
async def test_sleep_consolidation_tomorrow_monologue_updates_latest_state(monkeypatch):
    payload = _payload(tomorrow_monologue="醒来后先整理计划。")
    provider = FakeProvider([json.dumps(payload, ensure_ascii=False)])
    state = PersonaState(mood=72, latest_monologue="旧独白", last_monologue_at=1.0)
    db = FakeDB(state)
    agent = SleepConsolidation(db, provider, _cfg(), None)

    monkeypatch.setattr(consolidation_mod.time, "time", lambda: 1234.0)
    await agent.run(state, [], "night")

    assert db.monologues[0]["created_at"] == 1234.0
    assert db.state.latest_monologue == payload["tomorrow_monologue"]
    assert db.state.last_monologue_at == 1234.0
    assert db.saved[-1].latest_monologue == payload["tomorrow_monologue"]
    assert db.saved[-1].last_monologue_at == 1234.0


@pytest.mark.asyncio
async def test_sleep_consolidation_malformed_json_returns_safe_default_without_throwing():
    provider = FakeProvider(["不是 JSON", "{broken"])
    db = FakeDB()
    agent = SleepConsolidation(db, provider, _cfg(), None)

    result = await agent.run({}, [], "night")

    assert result == {
        "daily_trajectory": None,
        "tidy_todos": {},
        "tidy_cues": {},
        "persona_arc_adjustment": None,
        "consolidated_memories": [],
        "tomorrow_monologue": "",
    }
    assert len(provider.calls) == 2
    assert db.trajectories == []
    assert db.arc_events == []
    assert db.monologues == []


@pytest.mark.asyncio
async def test_sleep_consolidation_provider_exception_returns_safe_default_without_throwing():
    provider = FakeProvider([RuntimeError("boom"), RuntimeError("boom again")])
    db = FakeDB()
    agent = SleepConsolidation(db, provider, _cfg(), None)

    result = await agent.run({}, [], "night")

    assert result["daily_trajectory"] is None
    assert result["consolidated_memories"] == []
    assert len(provider.calls) == 2
    assert db.trajectories == []


@pytest.mark.asyncio
async def test_sleep_consolidation_age_profile_none_prompt_has_no_age_block():
    provider = FakeProvider([json.dumps(_payload(), ensure_ascii=False)])
    db = FakeDB()
    agent = SleepConsolidation(db, provider, _cfg(), None)

    await agent.run({}, [], "night")

    prompt = "\n".join(message["content"] for message in provider.calls[0]["messages"])
    assert "<年龄信息>" not in prompt
    assert "</年龄信息>" not in prompt


@pytest.mark.asyncio
async def test_sleep_consolidation_prompt_constrains_consolidated_memories():
    provider = FakeProvider([json.dumps(_payload(), ensure_ascii=False)])
    db = FakeDB()
    agent = SleepConsolidation(db, provider, _cfg(), None)

    await agent.run({}, [], "night")

    prompt = "\n".join(message["content"] for message in provider.calls[0]["messages"])
    assert "consolidated_memories 只允许写入长期稳定事实" in prompt
    assert "明确主语或明确对象" in prompt
    assert "不要把 tomorrow_monologue 或 latest_monologue 式文本写进 important memory" in prompt
    assert "内心独白、短期情绪或泛泛关系感受" in prompt
    assert "无合格事实返回空数组" in prompt


@pytest.mark.asyncio
async def test_sleep_consolidation_age_profile_prompt_contains_age_info():
    provider = FakeProvider([json.dumps(_payload(), ensure_ascii=False)])
    db = FakeDB()
    agent = SleepConsolidation(db, provider, _cfg(), AgeProfileLike())

    await agent.run({}, [], "night")

    prompt = "\n".join(message["content"] for message in provider.calls[0]["messages"])
    assert "<年龄信息>" in prompt
    assert "年龄：16" in prompt
    assert "档位：青少年" in prompt
    assert "理想睡眠小时：8.5" in prompt
