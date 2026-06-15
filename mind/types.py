"""mind 层纯数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PersonaState:
    """人格运行时状态。"""

    energy: float = 80.0
    satiety: float = 80.0
    mood: float = 65.0
    social_need: float = 50.0
    current_action: str = "awake"
    action_until: float | None = None
    energy_critical_since: float | None = None
    active_sleep_record_id: str | None = None
    active_eat_record_id: str | None = None
    last_eat_at: float | None = None
    last_sleep_at: float | None = None
    last_tick_at: float = 0.0
    last_monologue_at: float = 0.0
    latest_monologue: str = ""


@dataclass(slots=True)
class Effect:
    """短期效果。"""

    id: str
    name: str
    effect_type: str
    intensity: float
    prompt_hint: str
    source_detail: str
    created_at: float
    expires_at: float


@dataclass(slots=True)
class Todo:
    """人格后台待处理事项。"""

    id: str
    title: str
    reason: str
    priority: int
    scope: str
    created_at: float
    expires_at: float | None = None
    status: str = "open"
    completed: bool = False


@dataclass(slots=True)
class Cue:
    """可被后续逻辑消费的线索。"""

    id: str
    cue_type: str
    summary: str
    conversation_id: str
    created_at: float
    expires_at: float


@dataclass(slots=True)
class UserProfile:
    """用户画像。"""

    user_id: str
    display_name: str = ""
    affinity: float = 0.0
    summary: str = ""
    traits: list[str] = field(default_factory=list)
    interaction_count: int = 0
    last_interaction_at: float = 0.0
