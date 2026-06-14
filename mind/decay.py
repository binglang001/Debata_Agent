"""mind 层生理衰减计算。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .age import AgeProfile, neutral_age_profile


class DecayEngine:
    """纯计算衰减引擎。"""

    def __init__(self, physiology_cfg: Any, age_profile: AgeProfile | None) -> None:
        self.physiology_cfg = physiology_cfg
        self.age_profile = age_profile or neutral_age_profile()

    def tick_energy(
        self,
        current: float,
        elapsed_hours: float,
        current_action: str,
        hours_since_sleep: float,
        now_hour: float,
        *,
        mode: str,
    ) -> float:
        """推进精力值。"""

        if mode == "disabled":
            return current

        energy_cfg = _read_field(self.physiology_cfg, "energy")
        if current_action == "sleeping":
            night_bonus = 1.3 if _in_hour_window(now_hour, 22.0, 6.0) else 1.0
            delta = (
                _cfg_number(energy_cfg, ("recovery_per_hour_sleep",), default=8.333)
                * self.age_profile.energy_recovery_mult
                * night_bonus
                * elapsed_hours
            )
            return clamp_percent(current + delta)
        if current_action == "eating":
            delta = _cfg_number(energy_cfg, ("recovery_per_hour_eat",), default=15.0) * elapsed_hours
            return clamp_percent(current + delta)

        sleep_debt = 1 + max(0.0, (hours_since_sleep - 16.0) * 0.2)
        night_factor = (
            1.5
            if _in_hour_window(
                now_hour,
                self.age_profile.bedtime_hour,
                self.age_profile.wakeup_hour,
            )
            else 1.0
        )
        delta = (
            _cfg_number(energy_cfg, ("decay_per_hour",), default=1.5)
            * self.age_profile.energy_decay_mult
            * sleep_debt
            * night_factor
            * elapsed_hours
        )
        return clamp_percent(current - delta)

    def tick_satiety(
        self,
        current: float,
        elapsed_hours: float,
        current_action: str,
        hours_since_eat: float,
        now_hour: float,
        *,
        mode: str,
    ) -> float:
        """推进饱腹值。"""

        if mode == "disabled":
            return current

        satiety_cfg = _read_field(self.physiology_cfg, "satiety")
        if current_action == "eating":
            delta = _cfg_number(satiety_cfg, ("recovery_per_minute",), default=0.5) * elapsed_hours * 60.0
            return clamp_percent(current + delta)

        hunger_factor = 1 + max(0.0, (hours_since_eat - 8.0) * 0.25)
        delta = (
            _cfg_number(satiety_cfg, ("decay_per_hour",), default=1.0)
            * self.age_profile.satiety_decay_mult
            * hunger_factor
            * elapsed_hours
        )
        return clamp_percent(current - delta)

    def check_collapse(
        self,
        energy: float,
        energy_critical_since: float | None,
        now: float,
        grace_seconds: float,
        *,
        mode: str,
    ) -> bool:
        """检查精力归零是否超过昏睡宽限期。"""

        return (
            mode == "tool"
            and energy <= 0
            and energy_critical_since is not None
            and now - energy_critical_since > grace_seconds
        )


def clamp_percent(value: float) -> float:
    return min(100.0, max(0.0, float(value)))


def _in_hour_window(now_hour: float, start: float, end: float) -> bool:
    """判断小时是否落在 [start,end) 窗口内，支持跨午夜。"""

    now_value = float(now_hour) % 24
    start_value = float(start) % 24
    end_value = float(end) % 24
    if start_value == end_value:
        return True
    if start_value < end_value:
        return start_value <= now_value < end_value
    return now_value >= start_value or now_value < end_value


def is_hour_in_window(now_hour: float, start: float, end: float) -> bool:
    """兼容旧导出名。"""

    return _in_hour_window(now_hour, start, end)


def _cfg_number(source: Any, names: tuple[str, ...], *, default: float) -> float:
    value = _read_field(source, *names)
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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

    model_dump = getattr(source, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return _read_field(dumped, *names)
    legacy_dict = getattr(source, "dict", None)
    if callable(legacy_dict):
        dumped = legacy_dict()
        if isinstance(dumped, Mapping):
            return _read_field(dumped, *names)
    return None
