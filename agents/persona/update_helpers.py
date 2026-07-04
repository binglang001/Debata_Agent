"""人格 update 相关纯转换 helper。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mind import Cue, Effect, Todo

if TYPE_CHECKING:
    from .update_models import _CueUpdate, _EffectUpdate, _TodoUpdate

_TODO_CLOSED_STATUS_VALUES = {
    "completed",
    "complete",
    "done",
    "finished",
    "closed",
    "cancelled",
    "canceled",
    "deleted",
    "missed",
}
_TODO_OPEN_STATUS_VALUES = {
    "open",
    "pending",
    "active",
    "todo",
    "new",
    "in_progress",
    "in-progress",
}

_CLOSE_OPERATIONS = {"close", "delete", "cancel", "complete"}
_UPDATE_OPERATIONS = {"update", "patch"}


def _coerce_level_value(
    value: object,
    *,
    default: object,
    low: float | int,
    medium: float | int,
    high: float | int,
) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return default
    mapping = {
        "low": low,
        "minor": low,
        "weak": low,
        "small": low,
        "light": low,
        "轻": low,
        "低": low,
        "弱": low,
        "轻微": low,
        "medium": medium,
        "mid": medium,
        "moderate": medium,
        "normal": medium,
        "average": medium,
        "中": medium,
        "中等": medium,
        "一般": medium,
        "适中": medium,
        "普通": medium,
        "high": high,
        "major": high,
        "strong": high,
        "large": high,
        "important": high,
        "高": high,
        "强": high,
        "强烈": high,
        "重要": high,
        "urgent": high,
        "critical": high,
        "very_high": high,
        "极高": high,
        "紧急": high,
    }
    return mapping.get(normalized, default)


def _todo_dedupe_key(scope: str, title: str) -> tuple[str, str]:
    normalized_scope = " ".join(str(scope or "persona").strip().lower().split()) or "persona"
    normalized_title = " ".join(str(title or "").strip().lower().split())
    normalized_title = normalized_title.rstrip("。.!！?？；;，,")
    return normalized_scope, normalized_title


def _coerce_model_timestamp(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except ValueError:
        return None


def _normalize_operation(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    mapping = {
        "created": "create",
        "insert": "create",
        "new": "create",
        "add": "create",
        "upsert": "create",
        "modify": "update",
        "modified": "update",
        "patch": "update",
        "edit": "update",
        "edited": "update",
        "close": "close",
        "closed": "close",
        "remove": "delete",
        "removed": "delete",
        "delete": "delete",
        "deleted": "delete",
        "drop": "delete",
        "dropped": "delete",
        "cancel": "cancel",
        "cancelled": "cancel",
        "canceled": "cancel",
        "complete": "complete",
        "completed": "complete",
        "done": "complete",
        "finish": "complete",
        "finished": "complete",
        "noop": "noop",
        "no_op": "noop",
        "none": "noop",
        "ignore": "noop",
        "skip": "noop",
    }
    normalized = mapping.get(text, text)
    if normalized == "cancelled":
        return "cancel"
    return normalized


def _operation_requires_existing(operation: str | None) -> bool:
    return operation in _CLOSE_OPERATIONS or operation in _UPDATE_OPERATIONS


def _todo_update_requires_existing(update: _TodoUpdate) -> bool:
    operation = update.operation
    if operation in _CLOSE_OPERATIONS or operation in _UPDATE_OPERATIONS:
        return True
    status = _todo_update_status(update)
    return status in _TODO_CLOSED_STATUS_VALUES


def _todo_is_expired(todo: Todo, now: float) -> bool:
    if todo.expires_at is None:
        return False
    try:
        return float(todo.expires_at) <= now
    except (TypeError, ValueError):
        return False


def _todo_sort_key(todo: Todo) -> tuple[Any, ...]:
    return (
        -_int_sort_value(todo.priority),
        _time_sort_value(todo.expires_at),
        _time_sort_value(todo.created_at),
        str(todo.id or ""),
    )


def _int_sort_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _time_sort_value(value: Any) -> tuple[int, float | str]:
    parsed = _optional_float(value)
    if parsed is not None:
        return (0, parsed)
    if value is None:
        return (1, "")
    return (1, str(value).strip())


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _todo_patch_text(update: _TodoUpdate, field_name: str, default: str) -> str:
    if field_name in update.model_fields_set:
        return str(getattr(update, field_name) or "")
    return default


def _effect_patch_text(update: _EffectUpdate, field_name: str, default: str) -> str:
    if field_name in update.model_fields_set:
        return str(getattr(update, field_name) or "")
    return default


def _cue_patch_text(update: _CueUpdate, field_name: str, default: str) -> str:
    if field_name in update.model_fields_set:
        return str(getattr(update, field_name) or "")
    return default


def _effect_from_update(
    update: _EffectUpdate,
    now: float,
    existing: Effect | None = None,
) -> Effect:
    fields_set = update.model_fields_set
    created_at = (
        update.created_at
        if "created_at" in fields_set and update.created_at is not None
        else existing.created_at if existing is not None else now
    )
    if "expires_at" in fields_set and update.expires_at is not None:
        expires_at = update.expires_at
    elif "duration_minutes" in fields_set and update.duration_minutes is not None:
        expires_at = created_at + update.duration_minutes * 60.0
    elif existing is not None:
        expires_at = existing.expires_at
    else:
        expires_at = None
    if expires_at is None:
        duration = update.duration_minutes if update.duration_minutes is not None else 60.0
        expires_at = created_at + duration * 60.0
    return Effect(
        id=update.id or (existing.id if existing is not None else f"effect_{uuid4().hex}"),
        name=_effect_patch_text(update, "name", existing.name if existing is not None else update.effect_type),
        effect_type=_effect_patch_text(
            update,
            "effect_type",
            existing.effect_type if existing is not None else "mood",
        ),
        intensity=(
            update.intensity
            if "intensity" in fields_set or existing is None
            else existing.intensity
        ),
        prompt_hint=_effect_patch_text(
            update,
            "prompt_hint",
            existing.prompt_hint if existing is not None else "",
        ),
        source_detail=_effect_patch_text(
            update,
            "source_detail",
            existing.source_detail if existing is not None else "",
        ),
        created_at=created_at,
        expires_at=expires_at,
    )


def _todo_from_update(update: _TodoUpdate, now: float) -> Todo:
    return Todo(
        id=update.id or f"todo_{uuid4().hex}",
        title=update.title,
        reason=update.reason,
        priority=update.priority,
        scope=update.scope,
        created_at=update.created_at or now,
        expires_at=update.expires_at,
    )


def _todo_record_from_update(
    update: _TodoUpdate,
    now: float,
    existing: Todo | None,
) -> dict[str, Any]:
    status = _todo_update_status(update)
    fields_set = update.model_fields_set
    if existing is not None:
        record = asdict(existing)
        for field_name in ("title", "reason", "priority", "scope", "created_at", "expires_at"):
            if field_name in fields_set:
                record[field_name] = getattr(update, field_name)
    elif status is None or status in _TODO_OPEN_STATUS_VALUES:
        record = asdict(_todo_from_update(update, now))
    else:
        record = {"id": update.id or f"todo_{uuid4().hex}"}
        for field_name in ("title", "reason", "priority", "scope", "created_at", "expires_at"):
            if field_name in fields_set:
                record[field_name] = getattr(update, field_name)

    record["id"] = str(update.id or record.get("id") or f"todo_{uuid4().hex}")
    if status is not None:
        record["status"] = status
        record["completed"] = status in _TODO_CLOSED_STATUS_VALUES
    return record


def _todo_update_status(update: _TodoUpdate) -> str | None:
    fields_set = update.model_fields_set
    if update.operation in {"close", "complete"}:
        return "completed"
    if update.operation == "delete":
        return "deleted"
    if update.operation == "cancel":
        return "cancelled"
    if "status" in fields_set and update.status is not None:
        return _normalize_todo_status(update.status)
    for field_name in ("completed", "done", "finished"):
        if field_name in fields_set:
            return "completed" if _truthy_todo_state(getattr(update, field_name)) else "open"
    for field_name in ("cancelled", "canceled"):
        if field_name in fields_set:
            return "cancelled" if _truthy_todo_state(getattr(update, field_name)) else "open"
    return None


def _todo_record_is_closed(record: dict[str, Any]) -> bool:
    status = _normalize_todo_status(record.get("status"))
    if status in _TODO_CLOSED_STATUS_VALUES:
        return True
    for field_name in ("completed", "done", "finished", "cancelled", "canceled"):
        if field_name in record and _truthy_todo_state(record.get(field_name)):
            return True
    return False


def _normalize_todo_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "canceled":
        return "cancelled"
    if text in {"complete", "finished", "closed"}:
        return "completed"
    return text


def _truthy_todo_state(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TODO_CLOSED_STATUS_VALUES or text in {"1", "true", "yes", "y"}:
            return True
        if text in _TODO_OPEN_STATUS_VALUES or text in {"0", "false", "no", "n", ""}:
            return False
    return bool(value)


def _cue_from_update(
    update: _CueUpdate,
    conversation_id: str,
    now: float,
    existing: Cue | None = None,
) -> Cue:
    fields_set = update.model_fields_set
    created_at = (
        update.created_at
        if "created_at" in fields_set and update.created_at is not None
        else existing.created_at if existing is not None else now
    )
    expires_at = (
        update.expires_at
        if "expires_at" in fields_set and update.expires_at is not None
        else existing.expires_at if existing is not None else now + 24 * 3600.0
    )
    return Cue(
        id=update.id or (existing.id if existing is not None else f"cue_{uuid4().hex}"),
        cue_type=_cue_patch_text(
            update,
            "cue_type",
            existing.cue_type if existing is not None else "conversation",
        ),
        summary=_cue_patch_text(
            update,
            "summary",
            existing.summary if existing is not None else "",
        ),
        conversation_id=(
            update.conversation_id
            if "conversation_id" in fields_set and update.conversation_id
            else existing.conversation_id if existing is not None else conversation_id
        ),
        created_at=created_at,
        expires_at=expires_at,
    )
