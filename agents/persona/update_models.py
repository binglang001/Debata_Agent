"""人格 update Pydantic 模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .update_helpers import (
    _coerce_level_value,
    _coerce_model_timestamp,
    _normalize_operation,
)


class _EffectUpdate(BaseModel):
    id: str | None = None
    operation: str | None = None
    name: str = ""
    effect_type: str = "mood"
    intensity: float = 0.0
    prompt_hint: str = ""
    source_detail: str = ""
    duration_minutes: float | None = Field(default=None, ge=0.0)
    created_at: float | None = None
    expires_at: float | None = None

    @field_validator("intensity", mode="before")
    @classmethod
    def normalize_intensity(cls, value: object) -> object:
        return _coerce_level_value(value, default=value, low=25.0, medium=50.0, high=75.0)

    @field_validator("operation", mode="before")
    @classmethod
    def normalize_operation(cls, value: object) -> str | None:
        return _normalize_operation(value)

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def normalize_timestamps(cls, value: object) -> object:
        return _coerce_model_timestamp(value)


class _TodoUpdate(BaseModel):
    id: str | None = None
    operation: str | None = None
    title: str = ""
    reason: str = ""
    priority: int = 1
    scope: str = "persona"
    created_at: float | None = None
    expires_at: float | None = None
    status: str | None = None
    completed: Any = None
    done: Any = None
    finished: Any = None
    cancelled: Any = None
    canceled: Any = None

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, value: object) -> object:
        return _coerce_level_value(value, default=value, low=2, medium=5, high=8)

    @field_validator("operation", mode="before")
    @classmethod
    def normalize_operation(cls, value: object) -> str | None:
        return _normalize_operation(value)

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def normalize_timestamps(cls, value: object) -> object:
        return _coerce_model_timestamp(value)


class _CueUpdate(BaseModel):
    id: str | None = None
    operation: str | None = None
    cue_type: str = "conversation"
    summary: str = ""
    conversation_id: str | None = None
    created_at: float | None = None
    expires_at: float | None = None

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def normalize_timestamps(cls, value: object) -> object:
        return _coerce_model_timestamp(value)

    @field_validator("operation", mode="before")
    @classmethod
    def normalize_operation(cls, value: object) -> str | None:
        return _normalize_operation(value)


def _normalize_traits_field(value: object) -> object:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        for separator in ("、", "，", ",", "；", ";", "\n", "\r"):
            text = text.replace(separator, ",")
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, list):
        traits: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                traits.append(text)
        return traits
    return value


class _ProfileUpdate(BaseModel):
    user_id: str | None = None
    display_name: str = ""
    affinity: float | None = None
    affinity_delta: float | None = None
    summary: str = ""
    traits: list[str] = Field(default_factory=list)
    interaction_count: int | None = None
    last_interaction_at: float | None = None

    @field_validator("traits", mode="before")
    @classmethod
    def normalize_traits(cls, value: object) -> object:
        return _normalize_traits_field(value)


class _RelationshipUpdate(BaseModel):
    user_id: str | None = None
    display_name: str = ""
    affinity: float | None = None
    affinity_delta: float | None = None
    summary: str = ""
    traits: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("traits", mode="before")
    @classmethod
    def normalize_traits(cls, value: object) -> object:
        return _normalize_traits_field(value)


class _TurnUpdate(BaseModel):
    mood: float | None = None
    mood_delta: float | None = None
    social_need: float | None = None
    social_need_delta: float | None = None
    latest_monologue: str = ""
    effects: list[_EffectUpdate] = Field(default_factory=list)
    profiles: list[_ProfileUpdate] = Field(default_factory=list)
    relationships: list[_RelationshipUpdate] = Field(default_factory=list)
    todos: list[_TodoUpdate] = Field(default_factory=list)
    cues: list[_CueUpdate] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_singular_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        _append_singular(normalized, "effect", "effects")
        _append_singular(normalized, "todo", "todos")
        _append_singular(normalized, "cue", "cues")

        profile = normalized.pop("profile", None)
        if profile is not None and "profiles" not in normalized:
            normalized["profiles"] = profile if isinstance(profile, list) else [profile]
        for singular in ("relationship", "relationship_update", "affinity_update"):
            value = normalized.pop(singular, None)
            if value is not None and "relationships" not in normalized:
                normalized["relationships"] = value if isinstance(value, list) else [value]
        for plural in ("relationship_updates", "affinity_updates"):
            value = normalized.pop(plural, None)
            if value is not None and "relationships" not in normalized:
                normalized["relationships"] = value
        for field_name in ("effects", "profiles", "relationships", "todos", "cues"):
            if field_name in normalized:
                normalized[field_name] = _normalize_update_list_field(normalized[field_name])
        return normalized


class _RecoveryEstimate(BaseModel):
    energy: float | None = None
    satiety: float | None = None
    mood: float | None = None
    social_need: float | None = None
    latest_monologue: str = ""
    reason: str = ""

    @field_validator("energy", "satiety", "mood", "social_need", mode="before")
    @classmethod
    def normalize_percent(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class _RecoveryResult(BaseModel):
    estimate: _RecoveryEstimate = Field(default_factory=_RecoveryEstimate)
    source: str = "fallback_formula"
    error: str = ""


def _append_singular(data: dict[str, Any], singular: str, plural: str) -> None:
    value = data.pop(singular, None)
    if value is None or plural in data:
        return
    data[plural] = value if isinstance(value, list) else [value]


def _normalize_update_list_field(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict) and not value:
        return []
    return [value]
