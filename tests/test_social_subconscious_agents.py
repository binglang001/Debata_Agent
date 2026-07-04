from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents.persona_agent import PersonaAgent
from agents.social_agent import SocialAgent
from agents.subconscious_agent import SubconsciousAgent
from app_config.schema import (
    AgentConfig,
    PersonaManagementConfig,
    PersonaManagementSubconsciousConfig,
)
from mind import DecayEngine, PersonaState
from providers.base import CompletionResult, Usage


def _decision(decision: str, reason: str = "有明确社交理由") -> str:
    return json.dumps(
        {
            "decision": decision,
            "reason": reason,
            "targets": ["private:1001"],
            "suggested_intent": "reply",
            "suggested_content": "收到",
        },
        ensure_ascii=False,
    )


class FakeProvider:
    name = "fake"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, **kwargs})
        content = self.responses.pop(0) if self.responses else _decision("skip", "")
        return CompletionResult(
            content=content,
            usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )


class FakePersona:
    def __init__(
        self,
        *,
        resting: bool = False,
        energy: float = 80.0,
        energy_mode: str = "disabled",
        todos: list[Any] | None = None,
    ) -> None:
        self.resting = resting
        self.state_snapshot = {"energy": energy}
        self.energy_mode = energy_mode
        self.todos = todos or []
        self.wakeups: list[str] = []

    def is_resting(self) -> bool:
        return self.resting

    def get_todos_for_proactive(self) -> list[Any]:
        return list(self.todos)

    async def on_wakeup(self, reason: str) -> None:
        self.wakeups.append(reason)


class FakePersonaDB:
    def __init__(self, state: PersonaState) -> None:
        self.state = state
        self.saved: list[PersonaState] = []

    async def load(self) -> None:
        return None

    async def get_state(self) -> PersonaState:
        return self.state

    async def save_state(self, state: PersonaState) -> None:
        self.state = state
        self.saved.append(state)

    async def get_active_effects(self, now: float) -> list:
        return []

    async def get_todos(self, include_completed: bool = True) -> list:
        return []

    async def get_cues(self, now: float) -> list:
        return []

    async def all_profiles(self) -> list:
        return []


def _persona_pm_cfg(energy_mode: str) -> PersonaManagementConfig:
    return PersonaManagementConfig(
        persona_agent={"timer_interval_minutes": 999},
        physiology={"energy": {"mode": energy_mode}},
    )


def _real_persona_agent(energy: float, energy_mode: str) -> PersonaAgent:
    pm_cfg = _persona_pm_cfg(energy_mode)
    return PersonaAgent(
        FakePersonaDB(PersonaState(energy=energy)),
        FakeProvider([]),
        AgentConfig(provider="fake", model="persona"),
        pm_cfg,
        None,
        DecayEngine(pm_cfg.physiology, None),
        None,
        {"name": "unit"},
    )


@pytest.mark.asyncio
async def test_social_agent_resting_skip_does_not_call_provider():
    provider = FakeProvider([_decision("full")])
    agent = SocialAgent(
        provider,
        AgentConfig(provider="fake", model="social"),
        persona_agent=FakePersona(resting=True),
    )

    assert await agent.decide([{"role": "user", "content": "在吗"}]) == {
        "decision": "skip",
        "reason": "persona_resting",
        "targets": [],
        "suggested_intent": "",
        "suggested_content": "",
    }
    assert provider.calls == []


@pytest.mark.asyncio
async def test_social_agent_resting_necessary_todo_continues_to_provider():
    provider = FakeProvider([_decision("full", "必要待办需要处理")])
    agent = SocialAgent(
        provider,
        AgentConfig(provider="fake", model="social"),
        persona_agent=FakePersona(
            resting=True,
            todos=[
                {
                    "title": "提醒主人喝水",
                    "reason": "生理必要动作",
                    "priority": 8,
                    "completed": False,
                }
            ],
        ),
    )

    result = await agent.decide([{"role": "user", "content": "后台检查"}])

    assert result["decision"] == "full"
    assert result["reason"] == "必要待办需要处理"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_social_agent_resting_expired_necessary_todo_still_skips():
    provider = FakeProvider([_decision("full")])
    agent = SocialAgent(
        provider,
        AgentConfig(provider="fake", model="social"),
        persona_agent=FakePersona(
            resting=True,
            todos=[
                {
                    "title": "提醒主人喝水",
                    "reason": "生理必要动作",
                    "priority": 8,
                    "expires_at": 1.0,
                    "completed": False,
                }
            ],
        ),
    )

    assert (await agent.decide([]))["reason"] == "persona_resting"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_social_agent_resting_necessary_keywords_can_come_from_reason_or_scope():
    provider = FakeProvider([_decision("full", "scope 必要待办")])
    agent = SocialAgent(
        provider,
        AgentConfig(provider="fake", model="social"),
        persona_agent=FakePersona(
            resting=True,
            todos=[
                {
                    "title": "确认状态",
                    "reason": "普通说明",
                    "scope": "private:1001 必须处理",
                    "priority": 7,
                    "expires_at": 4_102_444_800.0,
                    "completed": False,
                }
            ],
        ),
    )

    result = await agent.decide([])

    assert result["decision"] == "full"
    assert result["reason"] == "scope 必要待办"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_social_agent_resting_low_priority_todo_still_skips():
    provider = FakeProvider([_decision("full")])
    agent = SocialAgent(
        provider,
        AgentConfig(provider="fake", model="social"),
        persona_agent=FakePersona(
            resting=True,
            todos=[{"title": "提醒主人喝水", "priority": 2, "completed": False}],
        ),
    )

    assert (await agent.decide([]))["reason"] == "persona_resting"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_social_agent_low_energy_skips_only_in_tool_mode():
    tool_persona = _real_persona_agent(energy=5, energy_mode="tool")
    disabled_persona = _real_persona_agent(energy=5, energy_mode="disabled")
    await tool_persona.start()
    await disabled_persona.start()
    try:
        tool_provider = FakeProvider([_decision("full")])
        tool_agent = SocialAgent(
            tool_provider,
            AgentConfig(provider="fake", model="social"),
            persona_agent=tool_persona,
        )
        assert tool_persona.physiology_energy_mode == "tool"
        assert (await tool_agent.decide([]))["decision"] == "skip"
        assert tool_provider.calls == []

        disabled_provider = FakeProvider([_decision("full")])
        disabled_agent = SocialAgent(
            disabled_provider,
            AgentConfig(provider="fake", model="social"),
            persona_agent=disabled_persona,
        )
        assert disabled_persona.physiology_energy_mode == "disabled"
        assert (await disabled_agent.decide([]))["decision"] == "full"
        assert len(disabled_provider.calls) == 1
    finally:
        await tool_persona.shutdown()
        await disabled_persona.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("full", (True, "reason-full")),
        ("text_lite", (True, "reason-text_lite")),
        ("skip", (False, "")),
        ("react", (False, "")),
    ],
)
async def test_social_agent_should_act_maps_valid_json_decisions(decision, expected):
    usage_records: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    async def usage_recorder(_usage, metadata):
        usage_records.append(metadata)

    provider = FakeProvider([_decision(decision, f"reason-{decision}")])
    agent = SocialAgent(
        provider,
        AgentConfig(provider="fake", model="social"),
        usage_recorder=usage_recorder,
        status_callback=statuses.append,
    )

    assert await agent.should_act([{"role": "user", "content": "测试"}]) == expected
    assert usage_records[0]["agent"] == "社交决策"
    assert usage_records[0]["operation"] == "social_decide"
    assert statuses[0]["agent"] == "社交决策"


@pytest.mark.asyncio
async def test_social_agent_malformed_json_retries_once_then_falls_back():
    provider = FakeProvider(["not-json", "{bad"])
    agent = SocialAgent(provider, AgentConfig(provider="fake", model="social"))

    assert await agent.decide([]) == {
        "decision": "skip",
        "reason": "",
        "targets": [],
        "suggested_intent": "",
        "suggested_content": "",
    }
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_subconscious_agent_keyword_triggers_wake_callback():
    wakes: list[dict[str, Any]] = []
    persona = FakePersona()

    async def wake_callback(messages, reason):
        wakes.append({"messages": messages, "reason": reason})

    agent = SubconsciousAgent(
        provider=None,
        cfg=PersonaManagementSubconsciousConfig(
            merge_window_seconds=0.01,
            max_window_seconds=1.0,
            wake_keywords=["上线"],
            min_wake_score=0.5,
        ),
        persona_agent=persona,
        wake_callback=wake_callback,
    )

    await agent.start({"energy": 80})
    await agent.on_message("等你上线后叫我", "1001", 10)
    await asyncio.sleep(0.05)

    assert len(wakes) == 1
    assert wakes[0]["messages"][0]["text"] == "等你上线后叫我"
    assert "关键词" in wakes[0]["reason"]
    assert persona.wakeups == [wakes[0]["reason"]]


@pytest.mark.asyncio
async def test_subconscious_agent_start_accepts_trigger_event():
    agent = SubconsciousAgent(
        provider=None,
        cfg=PersonaManagementSubconsciousConfig(),
    )
    state = PersonaState(energy=55)
    event = {"event": "sleep_start", "sleep_type": "long_sleep"}

    await agent.start(state, trigger_event=event)

    assert agent.is_active is True
    assert agent._state_snapshot == state
    assert agent._trigger_event == event

    await agent.stop()
    assert agent._trigger_event is None


@pytest.mark.asyncio
async def test_subconscious_agent_high_affinity_triggers_immediately_by_max_window():
    wakes: list[dict[str, Any]] = []
    agent = SubconsciousAgent(
        provider=None,
        cfg=PersonaManagementSubconsciousConfig(
            merge_window_seconds=10.0,
            max_window_seconds=0.0,
            min_wake_score=0.5,
        ),
        wake_callback=lambda messages, reason: wakes.append(
            {"messages": messages, "reason": reason}
        ),
    )

    await agent.start({"energy": 80})
    await agent.on_message("普通消息", "1001", 90)

    assert len(wakes) == 1
    assert "高亲密度" in wakes[0]["reason"]


@pytest.mark.asyncio
async def test_subconscious_agent_low_score_does_not_wake():
    wakes: list[dict[str, Any]] = []
    agent = SubconsciousAgent(
        provider=None,
        cfg=PersonaManagementSubconsciousConfig(
            merge_window_seconds=0.01,
            max_window_seconds=1.0,
            wake_keywords=["重要"],
            min_wake_score=0.5,
        ),
        wake_callback=lambda messages, reason: wakes.append(
            {"messages": messages, "reason": reason}
        ),
    )

    await agent.start({"energy": 80})
    await agent.on_message("普通闲聊", "1001", 10)
    await asyncio.sleep(0.05)

    assert wakes == []


@pytest.mark.asyncio
async def test_subconscious_agent_stop_cancels_timer_and_active_state():
    wakes: list[dict[str, Any]] = []
    agent = SubconsciousAgent(
        provider=None,
        cfg=PersonaManagementSubconsciousConfig(
            merge_window_seconds=0.02,
            max_window_seconds=1.0,
            wake_keywords=["醒来"],
            min_wake_score=0.5,
        ),
        wake_callback=lambda messages, reason: wakes.append(
            {"messages": messages, "reason": reason}
        ),
    )

    await agent.start({"energy": 80})
    await agent.on_message("醒来看看", "1001", 10)
    await agent.stop()
    await asyncio.sleep(0.05)

    assert agent.is_active is False
    assert wakes == []


@pytest.mark.asyncio
async def test_subconscious_agent_stop_start_does_not_restore_buffer():
    wakes: list[dict[str, Any]] = []
    agent = SubconsciousAgent(
        provider=None,
        cfg=PersonaManagementSubconsciousConfig(
            merge_window_seconds=10.0,
            max_window_seconds=100.0,
            wake_keywords=["醒来"],
            min_wake_score=0.5,
        ),
        wake_callback=lambda messages, reason: wakes.append(
            {"messages": messages, "reason": reason}
        ),
    )

    await agent.start({"energy": 80})
    await agent.on_message("醒来看看", "1001", 10)
    assert [item["text"] for item in agent._buffer] == ["醒来看看"]

    await agent.stop()
    assert agent._buffer == []

    await agent.start({"energy": 70})
    assert agent._buffer == []
    assert agent._first_msg_at is None

    await agent._evaluate_buffer()
    assert wakes == []
    await agent.stop()
