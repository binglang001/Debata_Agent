"""人格数据库记录适配与纯 helper。"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_COMPLETED_STATUS_VALUES = {
    "1",
    "true",
    "yes",
    "y",
    "completed",
    "complete",
    "done",
    "finished",
    "closed",
    "cancelled",
    "canceled",
    "missed",
}
_INCOMPLETE_STATUS_VALUES = {
    "0",
    "false",
    "no",
    "n",
    "pending",
    "open",
    "active",
    "in_progress",
    "in-progress",
    "todo",
    "new",
}


def _record_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return dict(record)
    if is_dataclass(record) and not isinstance(record, type):
        return dict(asdict(record))
    model_dump = getattr(record, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(exclude_none=True))
    if isinstance(record, Mapping):
        return dict(record)
    attrs = getattr(record, "__dict__", None)
    if isinstance(attrs, dict):
        return {
            key: value
            for key, value in attrs.items()
            if not key.startswith("_") and not callable(value)
        }
    raise TypeError(f"unsupported persona record type: {type(record)!r}")


def _adapt_record(data: Any, type_names: tuple[str, ...]) -> Any:
    if data is None:
        return None
    if not isinstance(data, dict):
        return data
    cls = _find_mind_type(type_names)
    if cls is None:
        return dict(data)
    try:
        prepared = _prepare_dataclass_data(cls, data)
        if is_dataclass(cls):
            allowed = {field.name for field in fields(cls)}
            return cls(**{key: value for key, value in prepared.items() if key in allowed})
        return cls(**prepared)
    except Exception as e:  # noqa: BLE001
        logger.debug("mind.types 记录实例化失败，退回 dict: %s", e)
        return dict(data)


def _prepare_dataclass_data(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(data)
    name = cls.__name__
    if name == "PersonaState":
        if "action" in prepared and "current_action" not in prepared:
            prepared["current_action"] = prepared["action"]
    elif name == "Todo":
        prepared = {
            "id": _text_from(prepared, ("id", "todo_id"), default=""),
            "title": _text_from(prepared, ("title", "text", "name", "summary"), default=""),
            "reason": _text_from(prepared, ("reason", "source_detail", "detail"), default=""),
            "priority": _int_from(prepared, ("priority",), default=0),
            "scope": _text_from(prepared, ("scope",), default="persona"),
            "created_at": _time_from(prepared, ("created_at", "timestamp"), default=0.0),
            "expires_at": _optional_time_from(prepared, ("expires_at", "expire_at", "until", "end_at")),
            "status": _text_from(
                prepared,
                ("status",),
                default="completed" if _record_completed(prepared) else "open",
            ),
            "completed": _record_completed(prepared),
        }
    elif name == "Effect":
        prepared = {
            "id": _text_from(prepared, ("id", "effect_id"), default=""),
            "name": _text_from(prepared, ("name", "title", "summary"), default=""),
            "effect_type": _text_from(prepared, ("effect_type", "type", "kind"), default="general"),
            "intensity": _float_from(prepared, ("intensity", "value"), default=0.0),
            "prompt_hint": _text_from(prepared, ("prompt_hint", "hint", "description"), default=""),
            "source_detail": _text_from(prepared, ("source_detail", "source", "reason"), default=""),
            "created_at": _time_from(prepared, ("created_at", "timestamp"), default=0.0),
            "expires_at": _time_from(prepared, ("expires_at", "expire_at", "until", "end_at"), default=0.0),
        }
    elif name == "UserProfile":
        prepared = {
            "user_id": _text_from(prepared, ("user_id", "profile_id", "id"), default=""),
            "display_name": _text_from(prepared, ("display_name", "nickname", "name"), default=""),
            "affinity": _float_from(prepared, ("affinity",), default=0.0),
            "summary": _text_from(prepared, ("summary", "description"), default=""),
            "traits": _text_list_from(prepared, ("traits", "facts")),
            "interaction_count": _int_from(prepared, ("interaction_count",), default=0),
            "last_interaction_at": _time_from(prepared, ("last_interaction_at",), default=0.0),
        }
        attributes = data.get("attributes")
        if not prepared["traits"] and isinstance(attributes, Mapping):
            prepared["traits"] = _text_list_value(attributes.get("traits") or attributes.get("facts"))
    elif name == "Cue":
        prepared = {
            "id": _text_from(prepared, ("id", "cue_id"), default=""),
            "cue_type": _text_from(prepared, ("cue_type", "type", "kind"), default="general"),
            "summary": _text_from(prepared, ("summary", "text", "name"), default=""),
            "conversation_id": _text_from(prepared, ("conversation_id", "conversation", "scope"), default=""),
            "created_at": _time_from(prepared, ("created_at", "timestamp"), default=0.0),
            "expires_at": _time_from(prepared, ("expires_at", "expire_at", "until", "end_at"), default=0.0),
        }
    return prepared


def _first_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _text_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: str) -> str:
    value = _first_value(data, keys)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _float_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: float) -> float:
    value = _first_value(data, keys)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: int) -> int:
    value = _first_value(data, keys)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _time_from(data: Mapping[str, Any], keys: tuple[str, ...], *, default: float) -> float:
    value = _first_value(data, keys)
    if value in (None, ""):
        return default
    kind, normalized = _time_sort_value(value)
    if kind == 0 and isinstance(normalized, int | float):
        return float(normalized)
    return default


def _optional_time_from(data: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    value = _first_value(data, keys)
    if value in (None, ""):
        return None
    return _time_from(data, keys, default=0.0)


def _text_list_from(data: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    return _text_list_value(_first_value(data, keys))


def _text_list_value(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _find_mind_type(type_names: tuple[str, ...]) -> type | None:
    try:
        module = importlib.import_module("mind.types")
    except ModuleNotFoundError:
        return None
    for name in type_names:
        value = getattr(module, name, None)
        if isinstance(value, type):
            return value
    return None


def _ensure_record_id(data: dict[str, Any], keys: tuple[str, ...], prefix: str) -> str:
    record_id = _optional_text(data, keys)
    if not record_id:
        record_id = f"{prefix}_{uuid4().hex[:16]}"
    for key in keys:
        if key in data:
            data[key] = record_id
            break
    else:
        data["id"] = record_id
    return record_id


def _optional_text(data: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _record_active(data: Mapping[str, Any]) -> bool:
    for key in ("active", "enabled"):
        if key in data:
            return bool(data.get(key))
    return True


def _record_completed(data: Mapping[str, Any]) -> bool:
    for key in ("completed", "done", "finished"):
        if key in data:
            return _explicit_completed_value(data.get(key))
    status = str(data.get("status") or "").strip().lower()
    return status in _COMPLETED_STATUS_VALUES


def _todo_readable_sort_key(data: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        -_int_from(data, ("priority",), default=0),
        _time_sort_value(_first_value(data, ("expires_at", "expire_at", "until", "end_at"))),
        _time_sort_value(_first_value(data, ("created_at", "timestamp"))),
        str(_first_value(data, ("id", "todo_id")) or ""),
    )


def _explicit_completed_value(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return False
        if text in _COMPLETED_STATUS_VALUES:
            return True
        if text in _INCOMPLETE_STATUS_VALUES:
            return False
    return bool(value)


def _is_expired(data: Mapping[str, Any], now: Any) -> bool:
    expires_at = _optional_text(data, ("expires_at", "expire_at", "until", "end_at"))
    if not expires_at:
        return False
    expires_value = _time_value(expires_at)
    now_value = _time_value(now)
    if expires_value is None or now_value is None:
        return False
    return expires_value <= now_value


def _compare_time(left: Any, right: Any) -> int:
    left_value = _time_sort_value(left)
    right_value = _time_sort_value(right)
    if left_value < right_value:
        return -1
    if left_value > right_value:
        return 1
    return 0


def _time_sort_value(value: Any) -> tuple[int, float | str]:
    parsed = _time_value(value)
    if parsed is not None:
        return (0, parsed)
    if value is None:
        return (1, _now_text())
    text = str(value).strip()
    return (1, text)


def _time_value(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    normalized = _normalize_iso_time_text(text)
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        pass
    legacy = text.replace("T", " ")
    for candidate in (legacy[:19], legacy[:16], legacy[:10]):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                return datetime.strptime(candidate, fmt).timestamp()
            except ValueError:
                pass
    return None


def _normalize_iso_time_text(value: str) -> str:
    text = value.strip()
    if text.endswith("Z"):
        return f"{text[:-1]}+00:00"
    if text.endswith("z"):
        return f"{text[:-1]}+00:00"
    return text


def _now_value(now: Any) -> Any:
    return _now_text() if now is None else now


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: Any, *, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _clean_ids(ids: str | Iterable[str]) -> list[str]:
    if isinstance(ids, str):
        ids = [ids]
    return [text for item in ids if (text := str(item or "").strip())]


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)
