"""年龄档位解析。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class AgeProfile:
    """年龄档位对生理、作息和提示词的影响。"""

    bracket: str
    age: int
    energy_decay_mult: float
    energy_recovery_mult: float
    satiety_decay_mult: float
    mood_volatility_mult: float
    bedtime_hour: float
    wakeup_hour: float
    ideal_sleep_hours: float
    monologue_style: str
    emotional_hint: str
    social_hint: str


def resolve_age_profile(
    age: int | None,
    brackets_cfg: list[Any] | Any,
    default_age: int | None = None,
) -> AgeProfile | None:
    """按年龄解析年龄档；没有年龄时返回 None，表示不启用年龄系统。"""

    resolved_age = _coerce_int(age)
    if resolved_age is None:
        resolved_age = _coerce_int(default_age)
    if resolved_age is None:
        return None

    for bracket_cfg in _iter_brackets(brackets_cfg):
        if _age_matches(resolved_age, bracket_cfg):
            return _profile_from_config(resolved_age, bracket_cfg)
    return neutral_age_profile(resolved_age)


def neutral_age_profile(age: int | None = None) -> AgeProfile:
    """返回不会改变数值计算的中性年龄档。"""

    return AgeProfile(
        bracket="无年龄" if age is None else "中性",
        age=0 if age is None else age,
        energy_decay_mult=1.0,
        energy_recovery_mult=1.0,
        satiety_decay_mult=1.0,
        mood_volatility_mult=1.0,
        bedtime_hour=23.0,
        wakeup_hour=7.0,
        ideal_sleep_hours=8.0,
        monologue_style="",
        emotional_hint="",
        social_hint="",
    )


def _iter_brackets(brackets_cfg: Any) -> list[Any]:
    if brackets_cfg is None:
        return []
    if isinstance(brackets_cfg, Mapping):
        for key in ("brackets", "age_brackets", "profiles"):
            value = brackets_cfg.get(key)
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
                return list(value)
        if _looks_like_bracket(brackets_cfg):
            return [brackets_cfg]
        return []

    dumped = _model_dump(brackets_cfg)
    if dumped is not None:
        return _iter_brackets(dumped)

    if isinstance(brackets_cfg, Iterable) and not isinstance(brackets_cfg, (str, bytes)):
        return list(brackets_cfg)

    for name in ("brackets", "age_brackets", "profiles"):
        value = getattr(brackets_cfg, name, None)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            return list(value)
    if _looks_like_bracket(brackets_cfg):
        return [brackets_cfg]
    return []


def _looks_like_bracket(value: Any) -> bool:
    return any(
        _read_field(value, name) is not None
        for name in ("name", "bracket", "min", "min_age", "max", "max_age")
    )


def _age_matches(age: int, bracket_cfg: Any) -> bool:
    min_age = _coerce_int(_read_field(bracket_cfg, "min", "min_age"))
    max_age = _coerce_int(_read_field(bracket_cfg, "max", "max_age"))
    if min_age is not None and age < min_age:
        return False
    if max_age is not None and age > max_age:
        return False
    return min_age is not None or max_age is not None


def _profile_from_config(age: int, bracket_cfg: Any) -> AgeProfile:
    neutral = neutral_age_profile(age)
    bracket = _read_field(bracket_cfg, "bracket", "name")
    return AgeProfile(
        bracket=str(bracket or neutral.bracket),
        age=age,
        energy_decay_mult=_coerce_float(
            _read_field(bracket_cfg, "energy_decay_mult", "energy_decay_multiplier"),
            default=neutral.energy_decay_mult,
        ),
        energy_recovery_mult=_coerce_float(
            _read_field(bracket_cfg, "energy_recovery_mult", "energy_recovery_multiplier"),
            default=neutral.energy_recovery_mult,
        ),
        satiety_decay_mult=_coerce_float(
            _read_field(bracket_cfg, "satiety_decay_mult", "satiety_decay_multiplier"),
            default=neutral.satiety_decay_mult,
        ),
        mood_volatility_mult=_coerce_float(
            _read_field(bracket_cfg, "mood_volatility_mult", "mood_volatility_multiplier"),
            default=neutral.mood_volatility_mult,
        ),
        bedtime_hour=_coerce_float(
            _read_field(bracket_cfg, "bedtime_hour", "bedtime"),
            default=neutral.bedtime_hour,
        ),
        wakeup_hour=_coerce_float(
            _read_field(bracket_cfg, "wakeup_hour", "wakeup"),
            default=neutral.wakeup_hour,
        ),
        ideal_sleep_hours=_coerce_float(
            _read_field(bracket_cfg, "ideal_sleep_hours", "sleep_hours"),
            default=neutral.ideal_sleep_hours,
        ),
        monologue_style=str(_read_field(bracket_cfg, "monologue_style") or ""),
        emotional_hint=str(_read_field(bracket_cfg, "emotional_hint") or ""),
        social_hint=str(_read_field(bracket_cfg, "social_hint") or ""),
    )


def _read_field(source: Any, *names: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)

    dumped = _model_dump(source)
    if dumped is None:
        return None
    return _read_field(dumped, *names)


def _model_dump(value: Any) -> Mapping[str, Any] | None:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, Mapping) else None
    legacy_dict = getattr(value, "dict", None)
    if callable(legacy_dict):
        dumped = legacy_dict()
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any, *, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
