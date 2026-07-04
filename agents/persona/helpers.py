"""人格管理通用 helper。"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime
from typing import Any

from mind import Cue, Effect, PersonaState, Todo, UserProfile, clamp_percent

from .update_helpers import _optional_float
from .update_models import _RecoveryEstimate

logger = logging.getLogger("agents.persona_agent")


def _iter_records(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return list(value)
    return [value]


def _record_field(record: Any, *names: str) -> Any:
    if isinstance(record, dict):
        for name in names:
            if name in record:
                return record[name]
        return None
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    if is_dataclass(record) and not isinstance(record, type):
        return _record_field(asdict(record), *names)
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return _record_field(dumped, *names)
    return None


def _record_sleep_type(record: Any) -> str:
    return str(_record_field(record, "sleep_type", "type") or "").strip()


def _record_date_key(record: Any) -> str:
    explicit = _record_field(record, "date", "day", "target_date")
    parsed = _date_key_from_value(explicit)
    if parsed:
        return parsed
    for name in ("created_at", "timestamp", "ended_at", "started_at"):
        parsed = _date_key_from_value(_record_field(record, name))
        if parsed:
            return parsed
    return ""


def _profile_snapshot(profile: UserProfile | None) -> dict[str, Any] | None:
    return asdict(profile) if profile is not None else None


def _append_field_change(
    changes: list[dict[str, Any]],
    field_name: str,
    before: Any,
    after: Any,
    **metadata: Any,
) -> None:
    if before == after:
        return
    changes.append(
        {
            "field": field_name,
            "before": before,
            "after": after,
            **{key: value for key, value in metadata.items() if value is not None},
        }
    )


def _audit_summary(applied_changes: dict[str, list[dict[str, Any]]]) -> str:
    parts = [f"{key}:{len(value)}" for key, value in applied_changes.items() if value]
    return "；".join(parts) if parts else "no changes"


def _format_relevant_long_term_memory(
    memories: Any,
    conversation_id: str,
    user_id: str | None,
) -> str:
    lines: list[str] = []
    scopes = {"global", conversation_id}
    if user_id:
        scopes.add(f"user:{user_id}")
    for item in _iter_records(memories):
        if isinstance(item, str):
            text = item.strip()
            if text:
                lines.append(f"- {text}")
            continue
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "global").strip()
        if scope and scope not in scopes:
            continue
        content = str(
            item.get("content") or item.get("memory_text") or item.get("text") or item.get("summary") or ""
        ).strip()
        if not content:
            continue
        memory_id = str(item.get("id") or item.get("memory_id") or "").strip()
        prefix = f"{memory_id} " if memory_id else ""
        lines.append(f"- {prefix}{content}")
        if len(lines) >= 5:
            break
    return "\n".join(lines)


def _format_recent_audits_section(audits: list[dict[str, Any]]) -> str:
    lines = []
    for audit in audits[:5]:
        audit_id = str(audit.get("id") or audit.get("audit_id") or "").strip()
        user_id = str(audit.get("user_id") or audit.get("inferred_user_id") or "").strip()
        conversation_id = str(audit.get("conversation_id") or "").strip()
        summary = str(audit.get("summary") or audit.get("reason") or audit.get("trigger") or "").strip()
        parts = []
        if audit_id:
            parts.append(f"ID: {audit_id}")
        if user_id:
            parts.append(f"用户ID: {user_id}")
        if conversation_id:
            parts.append(f"会话ID: {conversation_id}")
        if summary:
            parts.append(f"摘要: {summary}")
        if not parts:
            parts.append(json.dumps(audit, ensure_ascii=False, sort_keys=True, default=str))
        lines.append("- " + "；".join(parts))
    if not lines:
        return ""
    return "\n".join(["<最近审计>", *lines, "</最近审计>"])


def _date_key_from_value(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value)).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return ""


def _consolidation_monologue(result: Any) -> str:
    if result is None:
        return ""
    value = _record_field(result, "tomorrow_monologue", "latest_monologue")
    return str(value or "").strip()


def _recovery_estimate_payload(estimate: _RecoveryEstimate) -> dict[str, Any]:
    return {key: value for key, value in estimate.model_dump(exclude_none=True).items() if value != ""}


def _is_substantial_meal(
    meal_type: str,
    description: str,
    duration_minutes: float | None,
) -> bool:
    meal_text = f"{meal_type} {description}".lower()
    if any(token in meal_text for token in ("snack", "点心", "零食", "小吃", "垫肚子")):
        return False
    substantial_tokens = (
        "breakfast",
        "lunch",
        "dinner",
        "meal",
        "正餐",
        "早餐",
        "午餐",
        "晚餐",
        "早饭",
        "午饭",
        "晚饭",
        "米饭",
        "面",
        "饭",
        "菜",
    )
    return (duration_minutes is not None and duration_minutes >= 25) or any(
        token in meal_text for token in substantial_tokens
    )


def _meal_satiety_target(meal_type: str, duration_minutes: float | None) -> float:
    meal = meal_type.strip().lower()
    base = 68.0
    if meal in {"lunch", "dinner", "午餐", "晚餐", "午饭", "晚饭", "正餐"}:
        base = 78.0
    elif meal in {"breakfast", "早餐", "早饭"}:
        base = 72.0
    if duration_minutes is not None:
        base += min(max(duration_minutes - 25.0, 0.0) * 0.4, 8.0)
    return clamp_percent(base)


async def _maybe_await_call(
    target: Any,
    method_name: str,
    *args: Any,
    default: Any = None,
    **kwargs: Any,
) -> Any:
    method = getattr(target, method_name, None)
    if method is None:
        return default
    try:
        result = method(*args, **kwargs)
    except TypeError:
        if args or kwargs:
            result = method()
        else:
            raise
    if inspect.isawaitable(result):
        return await result
    return result


def _call_subconscious_starter(
    starter: Any,
    state_snapshot: PersonaState,
    event: dict[str, Any],
    *,
    prefer_state_snapshot: bool,
) -> Any:
    try:
        signature = inspect.signature(starter)
    except (TypeError, ValueError):
        return _call_subconscious_starter_fallback(
            starter,
            state_snapshot,
            event,
            prefer_state_snapshot=prefer_state_snapshot,
        )

    parameters = list(signature.parameters.values())
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters)
    accepts_varargs = any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in parameters)
    positional = [
        param
        for param in parameters
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    names = {param.name for param in parameters}

    if accepts_kwargs or "trigger_event" in names:
        return starter(state_snapshot, trigger_event=event)
    if accepts_varargs or len(positional) >= 2:
        return starter(state_snapshot, event)
    if len(positional) == 0:
        return starter()

    first_name = positional[0].name.lower()
    if prefer_state_snapshot or "state" in first_name or "snapshot" in first_name:
        return starter(state_snapshot)
    return starter(event)


def _call_subconscious_starter_fallback(
    starter: Any,
    state_snapshot: PersonaState,
    event: dict[str, Any],
    *,
    prefer_state_snapshot: bool,
) -> Any:
    try:
        return starter(state_snapshot, trigger_event=event)
    except TypeError:
        try:
            return starter(state_snapshot, event)
        except TypeError:
            return starter(state_snapshot if prefer_state_snapshot else event)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end])
    except json.JSONDecodeError:
        logger.debug("人格管理 JSON 解析失败", exc_info=True)
        return None
    return parsed if isinstance(parsed, dict) else None


_ACTION_TODO_SCOPE_BY_TYPE = {
    "sleep": {"", "persona", "sleep", "sleeping", "rest", "energy", "physiological", "short_term"},
    "eat": {
        "",
        "persona",
        "eat",
        "eating",
        "meal",
        "food",
        "satiety",
        "drink",
        "water",
        "physiological",
        "short_term",
    },
}

_ACTION_TODO_INCLUDE_BY_TYPE = {
    "sleep": ("睡", "睡觉", "睡眠", "休息", "休眠", "补觉", "躺下", "小睡"),
    "eat": (
        "吃饭",
        "吃点",
        "吃东西",
        "进食",
        "用餐",
        "喝水",
        "喝点",
        "喝饮料",
        "吃早餐",
        "吃午饭",
        "吃晚饭",
        "觅食",
        "填饱肚子",
    ),
}
_ACTION_TODO_EXCLUDE = (
    "提醒用户",
    "提醒对方",
    "提醒主人",
    "提醒我",
    "提醒一下我",
    "提醒下我",
    "叫我",
    "叫一下我",
    "叫下我",
    "通知我",
    "通知本人",
    "通知一下我",
    "通知下我",
    "喊我",
    "一会提醒我",
    "一会儿提醒我",
    "待会提醒我",
    "到点提醒我",
    "叫醒用户",
    "叫醒对方",
    "叫醒主人",
    "明早",
    "明天",
    "稍后",
    "后续",
)
_ACTION_TODO_TITLE_EXCLUDE = (
    "用户",
    "主人",
)
_ACTION_TODO_MEDICINE_EXCLUDE = (
    "吃药",
    "用药",
    "服药",
    "药",
)


def _todo_matches_current_action_start(todo: Todo, action_type: str) -> bool:
    scope = str(todo.scope or "").strip().lower()
    allowed_scopes = _ACTION_TODO_SCOPE_BY_TYPE.get(action_type, set())
    if scope not in allowed_scopes:
        return False

    title = str(todo.title or "")
    text = f"{title} {todo.reason or ''}".strip()
    if not text:
        return False
    compact_text = "".join(text.split())
    if any(excluded in text or excluded in compact_text for excluded in _ACTION_TODO_EXCLUDE):
        return False
    if action_type in {"eat", "sleep"} and any(
        excluded in text for excluded in _ACTION_TODO_MEDICINE_EXCLUDE
    ):
        return False
    if any(excluded in title for excluded in _ACTION_TODO_TITLE_EXCLUDE):
        return False
    return any(included in text for included in _ACTION_TODO_INCLUDE_BY_TYPE.get(action_type, ()))


def _action_until_expired(action_until: Any, now: float) -> bool:
    if action_until is None:
        return False
    try:
        return float(action_until) <= now
    except (TypeError, ValueError):
        return False


def _format_action_context(state: PersonaState, now: float) -> str:
    action = str(state.current_action or "").strip().lower()
    if not action:
        return ""
    label_by_action = {
        "awake": "清醒",
        "sleeping": "睡眠中",
        "collapsing": "体力崩溃休息中",
        "eating": "进食中",
    }
    label = label_by_action.get(action, state.current_action)
    parts = [f"- 当前动作: {label}"]
    action_until = _optional_float(state.action_until)
    if action_until is not None and action_until > now:
        remaining_minutes = max(1, int((action_until - now + 59) // 60))
        ends_at = datetime.fromtimestamp(action_until).strftime("%Y-%m-%d %H:%M:%S")
        parts.append(f"预计结束: {ends_at}")
        parts.append(f"剩余约 {remaining_minutes} 分钟")
        if action in {"sleeping", "collapsing"}:
            parts.append("尚未醒来，普通入站消息只记录到潜意识，不应当表现为刚醒")
        elif action == "eating":
            parts.append(
                "尚未结束进食，普通入站消息只记录到潜意识缓冲，不应在当前动作结束前回复或表现为已结束"
            )
    elif action_until is not None:
        parts.append("预计结束时间已到，等待睡眠结束/状态结算逻辑处理")
    return "；".join(parts)


def _coerce_state(value: Any) -> PersonaState:
    if isinstance(value, PersonaState):
        return replace(value)
    if isinstance(value, dict):
        return PersonaState(**_filter_dataclass_fields(PersonaState, value))
    return PersonaState()


def _coerce_effect(value: Any) -> Effect | None:
    return _coerce_dataclass(Effect, value)


def _coerce_todo(value: Any) -> Todo | None:
    return _coerce_dataclass(Todo, value)


def _coerce_cue(value: Any) -> Cue | None:
    return _coerce_dataclass(Cue, value)


def _coerce_profile(value: Any) -> UserProfile | None:
    return _coerce_dataclass(UserProfile, value)


def _coerce_dataclass(cls: Any, value: Any) -> Any | None:
    if isinstance(value, cls):
        return replace(value)
    if isinstance(value, dict):
        try:
            return cls(**_filter_dataclass_fields(cls, value))
        except TypeError:
            return None
    if is_dataclass(value):
        try:
            return cls(**_filter_dataclass_fields(cls, asdict(value)))
        except TypeError:
            return None
    return None


def _filter_dataclass_fields(cls: Any, data: dict[str, Any]) -> dict[str, Any]:
    names = {field.name for field in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


def _read_number(source: Any, *path: str, default: float) -> float:
    value = _read_field(source, *path)
    if value is None or isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _read_field(source: Any, *path: str) -> Any:
    current = source
    for name in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(name)
            continue
        current = getattr(current, name, None)
    return current


def _elapsed_hours(last_tick_at: float, now: float) -> float:
    if last_tick_at <= 0 or now <= last_tick_at:
        return 0.0
    return (now - last_tick_at) / 3600.0


def _hours_since(timestamp: float | None, now: float) -> float:
    if timestamp is None or timestamp <= 0 or now <= timestamp:
        return 0.0
    return (now - timestamp) / 3600.0


def _hour_of_day(timestamp: float) -> float:
    local = datetime.fromtimestamp(timestamp)
    return local.hour + local.minute / 60.0


def _bounded_duration(value: float, maximum: float) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = 0.0
    return max(0.0, min(duration, float(maximum)))


def _infer_user_id(conversation_id: str, participants: Any) -> str | None:
    from_conversation = _user_id_from_conversation(conversation_id)
    if from_conversation:
        return from_conversation
    if isinstance(participants, list) and participants:
        for item in participants:
            user_id = _participant_user_id(item)
            if user_id:
                return user_id
    return None


def _user_id_from_conversation(conversation_id: str) -> str | None:
    raw = str(conversation_id or "")
    if raw.startswith("private:"):
        value = raw.split(":", 1)[1].strip()
        return value or None
    return None


def _participant_user_id(participant: Any) -> str | None:
    if isinstance(participant, dict):
        for key in ("user_id", "id", "qq", "focus_user_id", "target_user_id"):
            value = participant.get(key)
            if value:
                return str(value).strip() or None
        return None
    if participant:
        return str(participant).strip() or None
    return None


def _participant_display_name(participants: Any, user_id: str) -> str:
    if not isinstance(participants, list):
        return ""
    for item in participants:
        if _participant_user_id(item) != user_id:
            continue
        if not isinstance(item, dict):
            return ""
        for key in ("display_name", "nickname", "name", "card"):
            value = item.get(key)
            if value:
                return str(value).strip()
    return ""


def _next_profile_interaction_count(existing: UserProfile, now: float) -> int:
    if existing.last_interaction_at == now:
        return existing.interaction_count
    return existing.interaction_count + 1
