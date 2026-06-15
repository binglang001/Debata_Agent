"""PersonaAgent 专用的紧凑上下文视图。

这个模块只负责把运行时 Python 对象整理成可读的中文结构化文本，不做模型摘要，
也不依赖 PersonaAgent 的内部实现，方便后续以独立工具接入。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from html import escape
from typing import Any

_REST_ACTIONS = {"eating", "sleeping", "collapsing"}
_MISSING = object()


@dataclass(slots=True)
class PersonaEpisodeFragment:
    """聊天现场里的 append-only 片段。"""

    seq: int
    text: str
    conversation_id: str = ""
    trigger_type: str = ""
    current_action: str = ""
    created_at: float | int | str | None = None


@dataclass(slots=True)
class PersonaEpisodeState:
    """当前 active episode 状态，不保存旧 episode，避免上下文无限增长。"""

    episode_id: str
    conversation_id: str = ""
    current_action: str = ""
    started_at: float | int | str | None = None
    updated_at: float | int | str | None = None
    fragments: list[PersonaEpisodeFragment] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(fragment.text) for fragment in self.fragments)


class PersonaEpisodeBuffer:
    """管理 PersonaAgent 聊天现场片段的确定性边界。

    边界触发时打开新 episode：会话切换、idle 超时、进入或离开吃饭/睡觉/崩溃动作、
    超过最大片段数、超过最大字符数。旧片段不会被摘要或改写。
    """

    def __init__(
        self,
        *,
        max_fragments: int = 12,
        max_chars: int = 1600,
        idle_seconds: float = 900.0,
        state: PersonaEpisodeState | None = None,
    ) -> None:
        self.max_fragments = max(1, int(max_fragments))
        self.max_chars = max(1, int(max_chars))
        self.idle_seconds = max(0.0, float(idle_seconds))
        self._episode_no = 0
        if state is None:
            self.state = self._new_state()
        else:
            self.state = state
            self._episode_no = _episode_no_from_id(state.episode_id)

    def append_event(
        self,
        event: Any,
        *,
        conversation_id: str | None = None,
        current_action: str | None = None,
        now: float | int | str | None = None,
    ) -> PersonaEpisodeState:
        """把一次事件压成一个现场片段并追加到 active episode。"""

        resolved_conversation_id = _clean_text(
            conversation_id if conversation_id is not None else _first(event, "conversation_id")
        )
        resolved_action = _clean_text(
            current_action if current_action is not None else _first(event, "current_action")
        )
        return self.append_fragment(
            _event_fragment_text(event),
            conversation_id=resolved_conversation_id,
            current_action=resolved_action,
            trigger_type=_clean_text(_first(event, "trigger_type", "type", "event_type")),
            now=now,
        )

    def append_fragment(
        self,
        text: Any,
        *,
        conversation_id: str = "",
        current_action: str = "",
        trigger_type: str = "",
        now: float | int | str | None = None,
    ) -> PersonaEpisodeState:
        """追加一个片段；如果命中边界，先重开 episode。"""

        timestamp = _timestamp_or_now(now)
        fragment_text = _truncate(_clean_text(text), self.max_chars)
        if not fragment_text:
            return self.state
        if self._should_reopen(fragment_text, conversation_id, current_action, timestamp):
            self.state = self._new_state(
                conversation_id=conversation_id,
                current_action=current_action,
                now=timestamp,
            )
        elif not self.state.fragments:
            self.state.conversation_id = conversation_id
            self.state.current_action = current_action
            self.state.started_at = timestamp

        seq = len(self.state.fragments) + 1
        self.state.fragments.append(
            PersonaEpisodeFragment(
                seq=seq,
                text=fragment_text,
                conversation_id=conversation_id,
                trigger_type=trigger_type,
                current_action=current_action,
                created_at=timestamp,
            )
        )
        self.state.updated_at = timestamp
        if conversation_id:
            self.state.conversation_id = conversation_id
        if current_action:
            self.state.current_action = current_action
        return self.state

    def _new_state(
        self,
        *,
        conversation_id: str = "",
        current_action: str = "",
        now: float | int | str | None = None,
    ) -> PersonaEpisodeState:
        self._episode_no += 1
        return PersonaEpisodeState(
            episode_id=f"episode_{self._episode_no:06d}",
            conversation_id=conversation_id,
            current_action=current_action,
            started_at=now,
            updated_at=now,
        )

    def _should_reopen(
        self,
        next_text: str,
        conversation_id: str,
        current_action: str,
        now: float | int | str | None,
    ) -> bool:
        state = self.state
        if not state.fragments:
            return False
        if state.conversation_id and conversation_id and state.conversation_id != conversation_id:
            return True
        if _action_crosses_rest_boundary(state.current_action, current_action):
            return True
        if _seconds_between(state.updated_at, now) > self.idle_seconds:
            return True
        if len(state.fragments) >= self.max_fragments:
            return True
        return state.char_count + len(next_text) > self.max_chars


class PersonaContextView:
    """把 PersonaAgent 可见对象整理成紧凑中文上下文。"""

    def __init__(self, episode_buffer: PersonaEpisodeBuffer | None = None) -> None:
        self.episode_buffer = episode_buffer or PersonaEpisodeBuffer()

    def build_text(
        self,
        context: Any,
        *,
        long_term_memory_text: Any = None,
        now: float | int | str | None = None,
        append_episode: bool = True,
    ) -> str:
        event = _event_source(context)
        state = _state_source(context)
        conversation_id = _clean_text(
            _first(event, "conversation_id") or _first(context, "conversation_id")
        )
        current_action = _clean_text(
            _first(state, "current_action") or _first(event, "current_action")
        )
        if append_episode and _event_has_context(event):
            self.episode_buffer.append_event(
                event,
                conversation_id=conversation_id,
                current_action=current_action,
                now=now,
            )

        sections = [
            _event_section(event),
            _episode_section(self.episode_buffer.state, self.episode_buffer),
            _state_section(state),
            _profile_section(context),
            _records_section(
                "短期影响",
                _records_from(context, "effects", "active_effects", "short_term_effects"),
                _EFFECT_FIELDS,
            ),
            _records_section(
                "线索",
                _records_from(context, "cues", "active_cues", "clues"),
                _CUE_FIELDS,
            ),
            _todos_section(_records_from(context, "todos", "todo_items", "pending_todos")),
            _long_term_memory_section(
                long_term_memory_text
                if long_term_memory_text is not None
                else _first(
                    context,
                    "long_term_memory_text",
                    "relevant_long_term_memory",
                    "long_term_memory",
                    "memory_text",
                )
            ),
        ]
        return "\n\n".join(section for section in sections if section)


def build_persona_context_text(
    context: Any,
    *,
    episode_buffer: PersonaEpisodeBuffer | None = None,
    long_term_memory_text: Any = None,
    now: float | int | str | None = None,
    append_episode: bool = True,
) -> str:
    """便捷函数：构造一次 PersonaAgent 动态上下文文本。"""

    return PersonaContextView(episode_buffer).build_text(
        context,
        long_term_memory_text=long_term_memory_text,
        now=now,
        append_episode=append_episode,
    )


_EVENT_RECORD_FIELDS = (
    ("ID", ("id", "event_id", "message_id")),
    ("发送者", ("sender", "sender_id", "user_id", "author_id")),
    ("名称", ("display_name", "nickname", "name")),
    ("内容", ("text", "content", "message", "summary")),
    ("时间", ("created_at", "timestamp", "time")),
)

_PARTICIPANT_FIELDS = (
    ("用户ID", ("user_id", "id", "qq", "uid")),
    ("名称", ("display_name", "nickname", "name", "card")),
    ("角色", ("role", "member_role")),
)

_EAT_FIELDS = (
    ("ID", ("id", "event_id", "record_id")),
    ("食物", ("food", "food_name", "name")),
    ("状态", ("status", "result")),
    ("摘要", ("summary", "detail", "description")),
    ("时间", ("created_at", "timestamp", "time")),
)

_TOOL_FIELDS = (
    ("ID", ("id", "tool_call_id", "call_id")),
    ("工具", ("name", "tool_name", "function.name")),
    ("状态", ("status", "state")),
    ("摘要", ("summary", "result", "error")),
)

_ACTION_FIELDS = (
    ("ID", ("id", "action_id")),
    ("动作", ("name", "action", "type")),
    ("状态", ("status", "state")),
    ("直到", ("action_until", "until", "ends_at")),
    ("摘要", ("summary", "reason", "detail")),
)

_STATE_FIELDS = (
    ("心情", ("mood",)),
    ("社交需求", ("social_need",)),
    ("精力", ("energy",)),
    ("饱腹", ("satiety",)),
    ("当前动作", ("current_action",)),
    ("动作持续到", ("action_until",)),
    ("最近内心独白", ("latest_monologue",)),
    ("上次吃饭", ("last_eat_at", "last_eat_time")),
    ("上次睡觉", ("last_sleep_at", "last_sleep_time")),
    ("上次互动", ("last_interaction_at", "latest_interaction_at", "last_message_at")),
)

_PROFILE_FIELDS = (
    ("用户ID", ("user_id", "id", "qq", "uid")),
    ("名称", ("display_name", "nickname", "name", "card")),
    ("好感", ("affinity",)),
    ("摘要", ("summary",)),
    ("特征", ("traits",)),
    ("互动次数", ("interaction_count",)),
    ("最后互动", ("last_interaction_at",)),
)

_PROFILE_AUDIT_FIELDS = (
    ("ID", ("id", "audit_id", "record_id")),
    ("时间", ("created_at", "timestamp", "time")),
    ("用户ID", ("user_id", "profile_user_id")),
    ("变动", ("field", "change_type", "action", "event")),
    ("旧值", ("old_value", "before")),
    ("新值", ("new_value", "after")),
    ("增量", ("delta", "affinity_delta")),
    ("摘要", ("summary", "reason", "detail")),
)

_EFFECT_FIELDS = (
    ("ID", ("id",)),
    ("名称", ("name",)),
    ("类型", ("effect_type", "type")),
    ("强度", ("intensity",)),
    ("提示", ("prompt_hint", "hint")),
    ("来源", ("source_detail", "source")),
    ("创建", ("created_at",)),
    ("过期", ("expires_at",)),
)

_CUE_FIELDS = (
    ("ID", ("id",)),
    ("类型", ("cue_type", "type")),
    ("摘要", ("summary", "text")),
    ("会话ID", ("conversation_id",)),
    ("创建", ("created_at",)),
    ("过期", ("expires_at",)),
)

_TODO_FIELDS = (
    ("ID", ("id",)),
    ("标题", ("title", "name")),
    ("原因", ("reason", "summary")),
    ("优先级", ("priority",)),
    ("范围", ("scope",)),
    ("状态", ("status",)),
    ("创建", ("created_at",)),
    ("过期", ("expires_at", "due_at")),
)

_GENERIC_FIELDS = (
    ("ID", ("id", "event_id", "record_id", "tool_call_id")),
    ("名称", ("name", "title", "display_name")),
    ("类型", ("type", "kind")),
    ("状态", ("status", "state")),
    ("摘要", ("summary", "text", "content", "reason", "detail")),
    ("时间", ("created_at", "timestamp", "time")),
)


def _event_source(context: Any) -> Any:
    event = _first(context, "event", "runtime_event", "trigger_event")
    return event if event is not None else context


def _state_source(context: Any) -> Any:
    state = _first(context, "state", "persona_state", "current_state")
    return state if state is not None else context


def _event_has_context(event: Any) -> bool:
    return any(
        _has_value(_first(event, *names))
        for names in (
            ("trigger_type", "type", "event_type"),
            ("conversation_id",),
            ("turn_new", "new_messages", "messages", "current_input", "text", "content", "message"),
            ("turn_summary", "summary"),
        )
    )


def _event_section(event: Any) -> str:
    lines: list[str] = []
    trigger_type = _first(event, "trigger_type", "type", "event_type")
    conversation_id = _first(event, "conversation_id")
    turn_new = _first(
        event,
        "turn_new",
        "new_messages",
        "messages",
        "new_entries",
        "current_input",
        "input",
        "text",
        "content",
        "message",
    )
    summary = _first(event, "turn_summary", "summary", "conversation_summary", "new_summary")
    participants = _first(event, "participants", "members")
    eat_event = _first(event, "eat_event", "eat_events")
    tools = _first(event, "tools", "tool_calls", "tool_results")
    actions = _first(event, "actions", "action_events")

    if _has_value(trigger_type):
        lines.append(f"- 触发类型: {_format_scalar(trigger_type)}")
    if _has_value(conversation_id):
        lines.append(f"- 会话ID: {_format_scalar(conversation_id)}")
    lines.extend(_format_named_block("本轮新增", turn_new, _EVENT_RECORD_FIELDS))
    if _has_value(summary):
        lines.append(f"- 本轮摘要: {_format_scalar(summary)}")
    lines.extend(_format_named_block("参与者", participants, _PARTICIPANT_FIELDS))
    lines.extend(_format_named_block("进食事件", eat_event, _EAT_FIELDS))
    lines.extend(_format_named_block("工具", tools, _TOOL_FIELDS))
    lines.extend(_format_named_block("行为", actions, _ACTION_FIELDS))
    if not lines:
        return ""
    return _wrap_section("事件", lines)


def _episode_section(state: PersonaEpisodeState, buffer: PersonaEpisodeBuffer) -> str:
    if not state.fragments:
        return ""
    lines = [
        f"- episode: {state.episode_id}",
        f"- 片段数: {len(state.fragments)}/{buffer.max_fragments}",
        f"- 字符数: {state.char_count}/{buffer.max_chars}",
    ]
    if state.conversation_id:
        lines.append(f"- 会话ID: {_format_scalar(state.conversation_id)}")
    if state.current_action:
        lines.append(f"- 当前动作: {_format_scalar(state.current_action)}")
    if _has_value(state.started_at):
        lines.append(f"- 开始: {_format_scalar(state.started_at)}")
    if _has_value(state.updated_at):
        lines.append(f"- 更新: {_format_scalar(state.updated_at)}")
    lines.append("- 片段:")
    for fragment in state.fragments:
        meta = [
            f"#{fragment.seq}",
            _format_scalar(fragment.created_at) if _has_value(fragment.created_at) else "",
            _format_scalar(fragment.trigger_type) if fragment.trigger_type else "",
            _format_scalar(fragment.current_action) if fragment.current_action else "",
        ]
        meta_text = " / ".join(item for item in meta if item)
        lines.append(f"  - {meta_text}: {_format_scalar(fragment.text)}")
    return _wrap_section("聊天现场", lines)


def _state_section(state: Any) -> str:
    lines = _format_record_lines([state], _STATE_FIELDS, sort=False)
    if not lines:
        return ""
    return _wrap_section("当前状态", lines)


def _profile_section(context: Any) -> str:
    profile_records = _profile_records(context)
    audit_records = _records_from(
        context,
        "profile_audits",
        "profile_audit_records",
        "recent_profile_audits",
        "profile_changes",
        "profile_change_records",
    )
    lines: list[str] = []
    lines.extend(_format_record_lines(profile_records, _PROFILE_FIELDS, sort=False))
    audit_lines = _format_record_lines(audit_records, _PROFILE_AUDIT_FIELDS)
    if audit_lines:
        lines.append("- 最近画像变动:")
        lines.extend(f"  {line}" for line in audit_lines)
    if not lines:
        return ""
    return _wrap_section("当前对象画像", lines)


def _records_section(title: str, records: list[Any], field_specs: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    lines = _format_record_lines(records, field_specs)
    if not lines:
        return ""
    return _wrap_section(title, lines)


def _todos_section(records: list[Any]) -> str:
    lines = []
    for record in _stable_records(records):
        fields = list(_TODO_FIELDS)
        line = _format_record_line(record, tuple(fields), extra_status=_todo_status(record))
        if line:
            lines.append(line)
    if not lines:
        return ""
    return _wrap_section("待办", lines)


def _long_term_memory_section(value: Any) -> str:
    if not _has_value(value):
        return ""
    lines = _format_value_lines(value)
    if not lines:
        return ""
    return _wrap_section("相关长期记忆", [f"- {line}" for line in lines])


def _format_named_block(
    title: str,
    value: Any,
    field_specs: tuple[tuple[str, tuple[str, ...]], ...],
) -> list[str]:
    if not _has_value(value):
        return []
    items = _as_list(value)
    if not items:
        text = (
            _format_record_inline(value, field_specs)
            if isinstance(value, Mapping) or is_dataclass(value)
            else _format_scalar(value)
        )
        return [f"- {title}: {text}"] if text else []
    if len(items) == 1:
        text = _format_record_inline(items[0], field_specs)
        return [f"- {title}: {text}"] if text else []
    lines = [f"- {title}:"]
    for item in items:
        text = _format_record_inline(item, field_specs)
        if text:
            lines.append(f"  - {text}")
    return lines


def _format_record_lines(
    records: list[Any],
    field_specs: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    sort: bool = True,
) -> list[str]:
    iterable = _stable_records(records) if sort else records
    lines = []
    for record in iterable:
        line = _format_record_line(record, field_specs)
        if line:
            lines.append(line)
    return lines


def _format_record_line(
    record: Any,
    field_specs: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    extra_status: str = "",
) -> str:
    parts = []
    seen_labels: set[str] = set()
    for label, aliases in field_specs:
        value = _first(record, *aliases)
        if not _has_value(value):
            continue
        if label == "状态" and extra_status:
            value = extra_status
        seen_labels.add(label)
        parts.append(f"{label}: {_format_scalar(value)}")
    if extra_status and "状态" not in seen_labels:
        parts.append(f"状态: {_format_scalar(extra_status)}")
    return f"- {'；'.join(parts)}" if parts else ""


def _format_record_inline(
    record: Any,
    field_specs: tuple[tuple[str, tuple[str, ...]], ...],
) -> str:
    if _is_scalar(record):
        return _format_scalar(record)
    line = _format_record_line(record, field_specs)
    return line[2:] if line.startswith("- ") else line


def _format_value_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        text = _clean_text(value)
        return [text] if text else []
    items = _as_list(value)
    if items:
        return [text for item in items if (text := _format_record_inline(item, _GENERIC_FIELDS))]
    text = _format_scalar(value)
    return [text] if text else []


def _profile_records(context: Any) -> list[Any]:
    profile = _first(context, "profile", "current_profile", "user_profile", "object_profile")
    if _has_value(profile):
        return _as_list(profile) or [profile]
    profiles = _first(context, "profiles", "user_profiles")
    return _as_list(profiles)


def _records_from(context: Any, *names: str) -> list[Any]:
    value = _first(context, *names)
    return _as_list(value)


def _stable_records(records: list[Any]) -> list[Any]:
    return [
        item
        for _, item in sorted(
            enumerate(records),
            key=lambda pair: (_sort_created_at(pair[1]), _sort_id(pair[1]), pair[0]),
        )
    ]


def _sort_created_at(record: Any) -> tuple[int, float | str]:
    value = _first(record, "created_at", "timestamp", "time")
    if not _has_value(value):
        return (1, "")
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (0, str(value))


def _sort_id(record: Any) -> str:
    value = _first(record, "id", "user_id", "audit_id", "record_id", "conversation_id")
    return _clean_text(value)


def _todo_status(record: Any) -> str:
    status = _first(record, "status")
    if _has_value(status):
        return _clean_text(status)
    completed = _first(record, "completed", "done", "finished", "closed", "cancelled", "canceled")
    if completed is True:
        return "completed"
    return ""


def _event_fragment_text(event: Any) -> str:
    turn_new = _first(
        event,
        "turn_new",
        "new_messages",
        "messages",
        "new_entries",
        "current_input",
        "input",
        "text",
        "content",
        "message",
    )
    summary = _first(event, "turn_summary", "summary", "conversation_summary", "new_summary")
    parts = []
    for item in _format_value_lines(turn_new):
        parts.append(item)
    if _has_value(summary):
        parts.append(f"摘要: {_format_scalar(summary)}")
    if not parts:
        trigger_type = _first(event, "trigger_type", "type", "event_type")
        conversation_id = _first(event, "conversation_id")
        parts.append(" / ".join(_format_scalar(item) for item in (trigger_type, conversation_id) if _has_value(item)))
    return "；".join(part for part in parts if part)


def _first(source: Any, *names: str) -> Any:
    for name in names:
        value = _read_field(source, name, default=_MISSING)
        if value is not _MISSING and _has_value(value):
            return value
    return None


def _read_field(source: Any, name: str, *, default: Any = None) -> Any:
    current = source
    for part in name.split("."):
        if current is None:
            return default
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
            continue
        value = getattr(current, part, _MISSING)
        if value is _MISSING:
            return default
        current = value
    return current


def _as_list(value: Any) -> list[Any]:
    if not _has_value(value) or isinstance(value, str | bytes):
        return []
    if isinstance(value, Mapping):
        return []
    if isinstance(value, Iterable):
        return list(value)
    return []


def _has_value(value: Any) -> bool:
    if value is None or value is _MISSING:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        return bool(list(value))
    return True


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, str | bytes | int | float | bool)


def _format_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return escape(_clean_text(value), quote=False)
    if isinstance(value, Mapping):
        return _format_record_inline(value, _GENERIC_FIELDS)
    if isinstance(value, Iterable) and not isinstance(value, str | bytes):
        return "、".join(item for item in (_format_scalar(item) for item in value) if item)
    if is_dataclass(value):
        return _format_record_inline(value, _GENERIC_FIELDS)
    return escape(_clean_text(value), quote=False)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return " ".join(text.split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _wrap_section(title: str, lines: list[str]) -> str:
    return "\n".join([f"<{title}>", *lines, f"</{title}>"])


def _timestamp_or_now(value: float | int | str | None) -> float | int | str:
    return time.time() if value is None else value


def _seconds_between(previous: Any, current: Any) -> float:
    if not _has_value(previous) or not _has_value(current):
        return 0.0
    try:
        return max(0.0, float(current) - float(previous))
    except (TypeError, ValueError):
        return 0.0


def _action_crosses_rest_boundary(previous: str, current: str) -> bool:
    previous_action = _clean_text(previous)
    current_action = _clean_text(current)
    if not previous_action or not current_action or previous_action == current_action:
        return False
    return previous_action in _REST_ACTIONS or current_action in _REST_ACTIONS


def _episode_no_from_id(episode_id: str) -> int:
    try:
        return int(str(episode_id).rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0


def _dataclass_field_names(value: Any) -> set[str]:
    if not is_dataclass(value):
        return set()
    return {field_info.name for field_info in fields(value)}


__all__ = [
    "PersonaContextView",
    "PersonaEpisodeBuffer",
    "PersonaEpisodeFragment",
    "PersonaEpisodeState",
    "build_persona_context_text",
]
