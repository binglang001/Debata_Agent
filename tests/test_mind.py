from __future__ import annotations

from dataclasses import asdict

import pytest
from pydantic import BaseModel

from mind import (
    AgeProfile,
    Cue,
    DecayEngine,
    Effect,
    PersonaState,
    Todo,
    UserProfile,
    _in_hour_window,
    neutral_age_profile,
    resolve_age_profile,
)


class AgeBracketModel(BaseModel):
    name: str
    min_age: int
    max_age: int
    energy_decay_multiplier: float = 1.0
    energy_recovery_multiplier: float = 1.0
    satiety_decay_multiplier: float = 1.0
    mood_volatility_multiplier: float = 1.0
    bedtime_hour: float = 23.0
    wakeup_hour: float = 7.0
    ideal_sleep_hours: float = 8.0
    monologue_style: str = ""
    emotional_hint: str = ""
    social_hint: str = ""


class BracketsModel(BaseModel):
    brackets: list[AgeBracketModel]


class EnergyConfig:
    decay_per_hour = 1.5
    recovery_per_hour_sleep = 8.333
    recovery_per_hour_eat = 15.0


class SatietyConfig:
    decay_per_hour = 1.0
    recovery_per_minute = 0.5


class PhysiologyObject:
    energy = EnergyConfig()
    satiety = SatietyConfig()


def _age_profile() -> AgeProfile:
    return AgeProfile(
        bracket="unit",
        age=20,
        energy_decay_mult=2.0,
        energy_recovery_mult=0.5,
        satiety_decay_mult=1.5,
        mood_volatility_mult=1.0,
        bedtime_hour=22.0,
        wakeup_hour=6.0,
        ideal_sleep_hours=8.0,
        monologue_style="unit",
        emotional_hint="unit",
        social_hint="unit",
    )


def test_dataclass_defaults_match_contract():
    state = PersonaState()
    assert asdict(state) == {
        "energy": 80.0,
        "satiety": 80.0,
        "mood": 65.0,
        "social_need": 50.0,
        "current_action": "awake",
        "action_until": None,
        "energy_critical_since": None,
        "active_sleep_record_id": None,
        "active_eat_record_id": None,
        "last_eat_at": None,
        "last_sleep_at": None,
        "last_tick_at": 0.0,
        "last_monologue_at": 0.0,
        "latest_monologue": "",
    }
    assert not hasattr(state, "persona_id")
    assert not hasattr(state, "metadata")

    effect = Effect(
        id="effect-1",
        name="calm",
        effect_type="mood",
        intensity=1.5,
        prompt_hint="calm hint",
        source_detail="unit",
        created_at=1.0,
        expires_at=2.0,
    )
    assert asdict(effect)["effect_type"] == "mood"

    todo = Todo(
        id="todo-1",
        title="review",
        reason="unit",
        priority=2,
        scope="persona",
        created_at=1.0,
    )
    assert todo.expires_at is None

    cue = Cue(
        id="cue-1",
        cue_type="conversation",
        summary="unit",
        conversation_id="conv-1",
        created_at=1.0,
        expires_at=2.0,
    )
    assert cue.summary == "unit"

    profile = UserProfile(user_id="u1")
    assert asdict(profile) == {
        "user_id": "u1",
        "display_name": "",
        "affinity": 0.0,
        "summary": "",
        "traits": [],
        "interaction_count": 0,
        "last_interaction_at": 0.0,
    }


def test_age_none_default_none_returns_none():
    assert resolve_age_profile(None, [], default_age=None) is None


def test_age_matches_pydantic_config_and_alias_fields():
    profile = resolve_age_profile(
        None,
        BracketsModel(
            brackets=[
                AgeBracketModel(
                    name="child",
                    min_age=0,
                    max_age=12,
                    energy_decay_multiplier=1.5,
                    energy_recovery_multiplier=1.2,
                    satiety_decay_multiplier=1.1,
                    mood_volatility_multiplier=1.3,
                    bedtime_hour=21,
                    wakeup_hour=7,
                    ideal_sleep_hours=10,
                    monologue_style="curious",
                    emotional_hint="sensitive",
                    social_hint="needs feedback",
                ),
                AgeBracketModel(name="teen", min_age=13, max_age=18),
            ]
        ),
        default_age=12,
    )

    assert profile is not None
    assert profile.bracket == "child"
    assert profile.age == 12
    assert profile.energy_decay_mult == 1.5
    assert profile.energy_recovery_mult == 1.2
    assert profile.satiety_decay_mult == 1.1
    assert profile.mood_volatility_mult == 1.3
    assert profile.bedtime_hour == 21
    assert profile.wakeup_hour == 7
    assert profile.ideal_sleep_hours == 10
    assert profile.monologue_style == "curious"
    assert profile.emotional_hint == "sensitive"
    assert profile.social_hint == "needs feedback"


def test_unmatched_age_returns_neutral_profile():
    profile = resolve_age_profile(
        99,
        {"brackets": [{"bracket": "child", "min": 0, "max": 12}]},
    )

    assert profile == neutral_age_profile(99)
    assert profile.bracket == "中性"
    assert profile.energy_decay_mult == 1.0


def test_neutral_profile_without_age_marks_no_age():
    profile = neutral_age_profile()

    assert profile.bracket == "无年龄"
    assert profile.age == 0


def test_cross_midnight_window():
    assert _in_hour_window(23, 23, 7)
    assert _in_hour_window(2, 23, 7)
    assert not _in_hour_window(7, 23, 7)
    assert not _in_hour_window(12, 23, 7)


def test_energy_disabled_is_noop():
    engine = DecayEngine(PhysiologyObject(), _age_profile())

    assert (
        engine.tick_energy(
            50,
            elapsed_hours=1,
            current_action="awake",
            hours_since_sleep=20,
            now_hour=23,
            mode="disabled",
        )
        == 50
    )


def test_tool_energy_awake_sleeping_and_eating_formulas():
    engine = DecayEngine(PhysiologyObject(), _age_profile())

    assert engine.tick_energy(
        100,
        elapsed_hours=2,
        current_action="awake",
        hours_since_sleep=18,
        now_hour=23,
        mode="tool",
    ) == pytest.approx(87.4)
    assert engine.tick_energy(
        50,
        elapsed_hours=2,
        current_action="sleeping",
        hours_since_sleep=20,
        now_hour=23,
        mode="tool",
    ) == pytest.approx(60.8329)
    assert engine.tick_energy(
        50,
        elapsed_hours=0.5,
        current_action="eating",
        hours_since_sleep=20,
        now_hour=12,
        mode="tool",
    ) == pytest.approx(57.5)


def test_satiety_disabled_and_tool_formulas():
    engine = DecayEngine(PhysiologyObject(), _age_profile())

    assert (
        engine.tick_satiety(
            60,
            elapsed_hours=1,
            current_action="awake",
            hours_since_eat=10,
            now_hour=12,
            mode="disabled",
        )
        == 60
    )
    assert engine.tick_satiety(
        100,
        elapsed_hours=2,
        current_action="awake",
        hours_since_eat=10,
        now_hour=12,
        mode="tool",
    ) == pytest.approx(95.5)
    assert engine.tick_satiety(
        60,
        elapsed_hours=0.5,
        current_action="eating",
        hours_since_eat=10,
        now_hour=12,
        mode="tool",
    ) == pytest.approx(75.0)


def test_decay_engine_none_age_profile_uses_neutral_numbers():
    engine = DecayEngine(
        {
            "energy": {
                "decay_per_hour": 1,
                "recovery_per_hour_sleep": 10,
                "recovery_per_hour_eat": 5,
            },
            "satiety": {"decay_per_hour": 2, "recovery_per_minute": 1},
        },
        None,
    )

    assert engine.tick_energy(100, 1, "awake", 0, 12, mode="tool") == pytest.approx(99)
    assert engine.tick_satiety(100, 1, "awake", 0, 12, mode="tool") == pytest.approx(98)


def test_collapse_grace():
    engine = DecayEngine(PhysiologyObject(), _age_profile())

    assert not engine.check_collapse(0, 100, 161, 60, mode="disabled")
    assert not engine.check_collapse(1, 100, 161, 60, mode="tool")
    assert not engine.check_collapse(0, None, 161, 60, mode="tool")
    assert not engine.check_collapse(0, 100, 160, 60, mode="tool")
    assert engine.check_collapse(0, 100, 161, 60, mode="tool")
