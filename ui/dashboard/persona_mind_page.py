"""人格后台页面。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from ..wizard.components import EmptyState
from .copy import DASHBOARD_COPY

logger = logging.getLogger(__name__)


_TIME_KEYS = {
    "created_at",
    "timestamp",
    "timestamp_unix",
    "time",
    "last_tick_at",
    "updated_at",
    "last_interaction_at",
}
_EVENT_LABELS = {
    "after_turn": "对话后状态",
    "tick": "心跳更新",
    "periodic_tick": "定时维护",
    "manual": "手动记录",
    "state": "状态更新",
    "state_update": "状态更新",
    "sleep_start": "开始睡眠",
    "wakeup": "醒来",
    "eat_start": "开始进食",
    "action_finished": "动作结束",
    "collapse": "精力耗尽",
}
_FIELD_LABELS = {
    "timestamp": "时间",
    "timestamp_unix": "时间",
    "created_at": "时间",
    "time": "时间",
    "last_tick_at": "最近心跳",
    "updated_at": "更新时间",
    "kind": "类型",
    "type": "类型",
    "title": "标题",
    "name": "名称",
    "user_id": "用户",
    "display_name": "显示名",
    "conversation_id": "对话",
    "content": "内容",
    "text": "文本",
    "summary": "摘要",
    "traits": "特征",
    "affinity": "亲近度",
    "interaction_count": "互动次数",
    "last_interaction_at": "最近互动",
    "message": "消息",
    "reason": "原因",
    "source": "来源",
    "action": "动作",
    "current_action": "动作",
    "previous_action": "上一动作",
    "meal_type": "餐食",
    "duration_minutes": "持续时间",
    "description": "描述",
    "sleep_type": "睡眠类型",
    "record_id": "记录",
    "mood": "心情",
    "social_need": "社交需求",
    "energy": "精力",
    "satiety": "饱腹",
    "latest_monologue": "最新独白",
}
_STATE_FIELDS = [
    ("心情", "mood"),
    ("社交需求", "social_need"),
    ("动作", "current_action"),
    ("精力", "energy"),
    ("饱腹", "satiety"),
    ("最新独白", "latest_monologue"),
    ("最近心跳", "last_tick_at"),
]


class _InfoCard(QFrame):
    """紧凑信息卡。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SectionCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.MD, Spacing.MD, Spacing.MD)
        outer.setSpacing(Spacing.SM)

        title_label = QLabel(title)
        title_label.setProperty("role", "title-3")
        title_label.setWordWrap(True)
        outer.addWidget(title_label)

        self._body = QVBoxLayout()
        self._body.setSpacing(Spacing.XS)
        outer.addLayout(self._body)

    def clear_body(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def add_line(self, label: str, value: Any) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Spacing.SM)

        key = QLabel(label)
        key.setProperty("role", "secondary")
        key.setFixedWidth(72)
        row.addWidget(key)

        text = QLabel(_format_value(value))
        text.setWordWrap(True)
        row.addWidget(text, 1)

        wrapper = QWidget()
        wrapper.setLayout(row)
        self._body.addWidget(wrapper)

    def add_field_grid(self, fields: Iterable[tuple[str, Any]], *, columns: int = 2) -> None:
        wrapper = QWidget()
        grid = QGridLayout(wrapper)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(Spacing.MD)
        grid.setVerticalSpacing(Spacing.SM)

        for index, (label, value) in enumerate(fields):
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)

            key = QLabel(label)
            key.setProperty("role", "secondary")
            key.setWordWrap(True)
            cell_layout.addWidget(key)

            text = QLabel(_format_value(value))
            text.setWordWrap(True)
            cell_layout.addWidget(text)

            row, column = divmod(index, columns)
            grid.addWidget(cell, row, column)

        for column in range(columns):
            grid.setColumnStretch(column, 1)
        self._body.addWidget(wrapper)

    def add_full_line(self, label: str, value: Any) -> None:
        key = QLabel(label)
        key.setProperty("role", "secondary")
        key.setWordWrap(True)
        self._body.addWidget(key)

        text = QLabel(_format_value(value))
        text.setWordWrap(True)
        self._body.addWidget(text)


class PersonaMindPage(QWidget):
    """展示人格管理后台的实时状态和近期记录。"""

    def __init__(self, runtime: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_pending = False
        self._refresh_generation = 0
        self._closed = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        head = QHBoxLayout()
        title = QLabel(DASHBOARD_COPY["persona_mind.title"])
        title.setProperty("role", "title-2")
        head.addWidget(title)
        head.addStretch(1)
        self._refresh_btn = QPushButton(DASHBOARD_COPY["button.refresh"])
        self._refresh_btn.setProperty("role", "text")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        outer.addLayout(head)

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(Spacing.MD)

        self._top_card = _InfoCard("运行概况")
        self._state_card = _InfoCard("实时状态")
        cards = QWidget()
        cards_grid = QGridLayout(cards)
        cards_grid.setContentsMargins(0, 0, 0, 0)
        cards_grid.setSpacing(Spacing.MD)
        cards_grid.addWidget(self._top_card, 0, 0)
        cards_grid.addWidget(self._state_card, 0, 1)
        cards_grid.setColumnStretch(0, 2)
        cards_grid.setColumnStretch(1, 5)
        content_layout.addWidget(cards)

        self._tabs = QTabWidget()
        self._state_logs = self._list_tab("暂无动向")
        self._effects = self._list_tab("暂无短期影响")
        self._todos_cues = self._list_tab("暂无待办或线索")
        self._profiles = self._list_tab("暂无用户画像")
        self._sleep_eat = self._list_tab("暂无睡眠或进食记录")
        self._arc = self._list_tab("暂无整理轨迹")
        self._tabs.addTab(self._state_logs, "动向")
        self._tabs.addTab(self._effects, "短期影响")
        self._tabs.addTab(self._todos_cues, "待办/线索")
        self._tabs.addTab(self._profiles, "用户画像")
        self._tabs.addTab(self._sleep_eat, "睡眠/进食")
        self._tabs.addTab(self._arc, "整理轨迹")
        content_layout.addWidget(self._tabs, 1)
        outer.addWidget(self._content, 1)

        self._empty = EmptyState(
            DASHBOARD_COPY["persona_mind.empty_title"],
            DASHBOARD_COPY["persona_mind.empty_subtitle"],
        )
        outer.addWidget(self._empty, 1)
        self._empty.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.destroyed.connect(self._on_destroyed)
        self.refresh()

    def refresh(self) -> None:
        if self._closed:
            return
        rt = self._runtime
        if not self._is_available(rt):
            self._refresh_generation += 1
            self._refresh_pending = False
            self._show_empty(True)
            return

        self._show_empty(False)
        self._render_sync_state()
        self._refresh_generation += 1
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_pending = True
            return

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        generation = self._refresh_generation
        self._refresh_task = loop.create_task(self._load_db_snapshot(generation))

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self._cancel_refresh_task()
        super().closeEvent(event)

    def _on_destroyed(self, *_args: Any) -> None:
        self._closed = True
        self._cancel_refresh_task()

    def _cancel_refresh_task(self) -> None:
        self._refresh_generation += 1
        self._refresh_pending = False
        task = self._refresh_task
        self._refresh_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _load_db_snapshot(self, generation: int) -> None:
        try:
            if self._closed:
                return
            rt = self._runtime
            db = getattr(rt, "persona_db", None)
            if db is None:
                return
            now = _utc_now()
            data = {
                "effects": await self._load_db_section(db, "get_active_effects", now),
                "todos": await self._load_db_section(
                    db,
                    "get_todos",
                    include_completed=False,
                ),
                "cues": await self._load_db_section(db, "get_cues", now),
                "profiles": await self._load_db_section(db, "all_profiles"),
                "monologues": await self._load_db_section(
                    db,
                    "recent_monologues",
                    limit=20,
                ),
                "trajectories": await self._load_db_section(
                    db,
                    "recent_trajectories",
                    limit=20,
                ),
                "state_logs": await self._load_db_section(
                    db,
                    "recent_state_logs",
                    limit=50,
                ),
                "sleep_records": await self._load_db_section(
                    db,
                    "recent_sleep_records",
                    limit=20,
                ),
                "eat_records": await self._load_db_section(
                    db,
                    "recent_eat_records",
                    limit=20,
                ),
                "arc_events": await self._load_db_section(
                    db,
                    "recent_arc_events",
                    limit=20,
                ),
            }
            if self._closed or generation != self._refresh_generation:
                return
            self._render_db_data(data)
        finally:
            if self._refresh_task is asyncio.current_task():
                self._refresh_task = None
                if self._refresh_pending and not self._closed:
                    self._refresh_pending = False
                    self.refresh()

    async def _load_db_section(
        self,
        db: Any,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        method = getattr(db, method_name, None)
        if not callable(method):
            return []
        try:
            return await _maybe_await(method(*args, **kwargs))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载人格后台数据失败: %s: %s", method_name, exc)
            return []

    def _render_sync_state(self) -> None:
        rt = self._runtime
        agent = getattr(rt, "persona_agent", None)
        state = _safe_call(getattr(agent, "get_state_snapshot", None)) or {}
        management = getattr(getattr(rt, "config", None), "persona_management", None)

        self._top_card.clear_body()
        self._top_card.add_line("当前人格", self._persona_name())
        enabled = bool(getattr(management, "enabled", False))
        self._top_card.add_line("人格管理", "已启用" if enabled else "未启用")
        self._top_card.add_line("年龄状态", self._age_text())
        self._top_card.add_line("后台活动", _activity_text(getattr(rt, "model_activity", {}) or {}))

        self._state_card.clear_body()
        fields = [
            ("心情", _get_value(state, "mood", "—")),
            ("社交需求", _get_value(state, "social_need", "—")),
            ("当前动作", _get_value(state, "current_action", "—")),
        ]
        if getattr(agent, "physiology_energy_mode", "disabled") == "tool":
            fields.append(("精力", _get_value(state, "energy", "—")))
        if getattr(agent, "physiology_satiety_mode", "disabled") == "tool":
            fields.append(("饱腹", _get_value(state, "satiety", "—")))
        last_tick = _get_value(state, "last_tick_at", "")
        if last_tick:
            fields.append(("最近心跳", _format_timestamp(last_tick) or last_tick))
        self._state_card.add_field_grid(fields, columns=2)
        self._state_card.add_full_line("最新独白", _get_value(state, "latest_monologue", "—"))

    def _render_db_data(self, data: dict[str, Any]) -> None:
        if self._closed:
            return

        self._fill_list(self._state_logs, data.get("state_logs"), empty="暂无动向")
        self._fill_list(self._effects, data.get("effects"), empty="暂无短期影响")
        self._fill_list(
            self._todos_cues,
            [
                *_tagged_items("待办", data.get("todos")),
                *_tagged_items("线索", data.get("cues")),
            ],
            empty="暂无待办或线索",
        )
        self._fill_list(
            self._profiles,
            data.get("profiles"),
            empty="暂无用户画像",
            formatter=_format_profile_item,
        )
        self._fill_list(
            self._sleep_eat,
            [
                *_tagged_items("睡眠", data.get("sleep_records")),
                *_tagged_items("进食", data.get("eat_records")),
            ],
            empty="暂无睡眠或进食记录",
        )
        self._fill_list(
            self._arc,
            [
                *_tagged_items("独白", data.get("monologues")),
                *_tagged_items("轨迹", data.get("trajectories")),
                *_tagged_items("事件", data.get("arc_events")),
            ],
            empty="暂无整理轨迹",
        )

    def _is_available(self, rt: Any) -> bool:
        if rt is None:
            return False
        management = getattr(getattr(rt, "config", None), "persona_management", None)
        if not bool(getattr(management, "enabled", False)):
            return False
        return getattr(rt, "persona_agent", None) is not None and getattr(rt, "persona_db", None) is not None

    def _persona_name(self) -> str:
        persona = getattr(self._runtime, "persona", None)
        display = getattr(persona, "display_name", None)
        if callable(display):
            try:
                value = display()
            except Exception:  # noqa: BLE001
                value = ""
            if value:
                return str(value)
        return str(getattr(persona, "name", None) or "—")

    def _age_text(self) -> str:
        cfg = getattr(getattr(self._runtime, "config", None), "persona_management", None)
        persona_cfg = getattr(getattr(self._runtime, "config", None), "persona", None)
        persona_key = str(getattr(persona_cfg, "active", "") or getattr(getattr(self._runtime, "persona", None), "name", "") or "")
        age_cfg = getattr(cfg, "age", None)
        overrides = getattr(age_cfg, "overrides", {}) or {}
        if persona_key in overrides:
            return f"覆盖年龄 {overrides[persona_key]} 岁"
        declared = _safe_call(getattr(getattr(self._runtime, "persona", None), "get_age", None))
        if declared is not None:
            return f"人格档案年龄 {declared} 岁"
        default_age = getattr(age_cfg, "default_age", None)
        if default_age is not None:
            return f"默认年龄 {default_age} 岁"
        return "未设置年龄"

    def _show_empty(self, on: bool) -> None:
        self._content.setVisible(not on)
        self._empty.setVisible(on)

    @staticmethod
    def _list_tab(empty: str) -> QListWidget:
        widget = QListWidget()
        widget.setWordWrap(True)
        widget.addItem(QListWidgetItem(empty))
        return widget

    @staticmethod
    def _fill_list(
        widget: QListWidget,
        items: Any,
        *,
        empty: str = "暂无记录",
        formatter: Any = None,
    ) -> None:
        widget.clear()
        rows = list(_iter_items(items))
        if not rows:
            widget.addItem(QListWidgetItem(empty))
            return
        format_row = formatter or _format_item
        for item in rows:
            widget.addItem(QListWidgetItem(format_row(item)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _safe_call(func: Any) -> Any:
    if not callable(func):
        return None
    try:
        return func()
    except Exception:  # noqa: BLE001
        return None


def _get_value(obj: Any, key: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _iter_items(items: Any) -> Iterable[Any]:
    if items is None:
        return []
    if isinstance(items, dict):
        return [items]
    if isinstance(items, str):
        return [items]
    try:
        return list(items)
    except TypeError:
        return [items]


def _tagged_items(label: str, items: Any) -> list[dict[str, Any]]:
    return [{"类别": label, "内容": item} for item in _iter_items(items)]


def _format_value(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _format_field_value(key: str, value: Any) -> str:
    if key in _TIME_KEYS:
        return _format_timestamp(value) or _format_value(value)
    if isinstance(value, dict):
        return _format_compact_mapping(value)
    if isinstance(value, list):
        return "、".join(_format_value(item) for item in value[:3])
    return _format_value(value)


def _format_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if number <= 1_000_000_000:
            return _format_value(value)
        dt = datetime.fromtimestamp(number, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            number = float(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return text
        else:
            if number > 1_000_000_000:
                dt = datetime.fromtimestamp(number, tz=timezone.utc)
            else:
                return text
    else:
        return ""

    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _format_item(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        data = {
            name: getattr(item, name)
            for name in dir(item)
            if not name.startswith("_") and not callable(getattr(item, name))
        }
    else:
        data = dict(item)

    if "类别" in data and "内容" in data:
        return f"[{data['类别']}] {_format_item(data['内容'])}"

    state_log = _format_state_log_item(data)
    if state_log:
        return state_log

    preferred = [
        "kind",
        "type",
        "title",
        "name",
        "user_id",
        "conversation_id",
        "content",
        "text",
        "summary",
        "message",
        "reason",
        "action",
        "current_action",
        "mood",
        "social_need",
        "energy",
        "satiety",
        "latest_monologue",
        "timestamp",
        "created_at",
        "time",
        "last_tick_at",
    ]
    parts = []
    for key in preferred:
        value = data.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{_field_label(key)}: {_format_field_value(key, value)}")
    if not parts:
        parts = [
            f"{_field_label(str(key))}: {_format_field_value(str(key), value)}"
            for key, value in data.items()
            if value not in (None, "", [], {})
        ]
    return "  ·  ".join(parts) if parts else "—"


def _format_profile_item(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        data = {
            name: getattr(item, name)
            for name in dir(item)
            if not name.startswith("_") and not callable(getattr(item, name))
        }
    else:
        data = dict(item)

    preferred = [
        "user_id",
        "display_name",
        "summary",
        "traits",
        "affinity",
        "interaction_count",
        "last_interaction_at",
    ]
    parts = []
    for key in preferred:
        value = data.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{_field_label(key)}: {_format_field_value(key, value)}")
    return "  ·  ".join(parts) if parts else _format_item(data)


def _format_state_log_item(data: dict[str, Any]) -> str:
    if "state" not in data and "event" not in data:
        return ""

    event = str(data.get("event") or data.get("type") or data.get("kind") or "state_update")
    title = _EVENT_LABELS.get(event, event)
    time_text = _first_timestamp_text(data, data.get("state"))
    head = title if not time_text else f"{title} · {time_text}"

    parts: list[str] = []
    for key in (
        "conversation_id",
        "reason",
        "action",
        "current_action",
        "previous_action",
        "meal_type",
        "duration_minutes",
        "description",
        "sleep_type",
        "record_id",
    ):
        value = data.get(key)
        if key == "reason" and isinstance(value, dict):
            value = value.get("reason") or value.get("text") or value.get("message") or value.get("detail")
        if value not in (None, "", [], {}):
            parts.append(f"{_field_label(key)}: {_format_field_value(key, value)}")

    source = _state_log_source(data)
    if source not in (None, "", [], {}):
        parts.append(f"{_field_label('source')}: {_format_field_value('source', source)}")

    state = data.get("state")
    if isinstance(state, dict):
        state_summary = _format_state_summary(state)
        if state_summary:
            parts.append(state_summary)

    for key in ("mood", "social_need", "energy", "satiety", "latest_monologue"):
        value = data.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{_field_label(key)}: {_format_field_value(key, value)}")

    return "\n".join([head, *parts]) if parts else head


def _first_timestamp_text(*items: Any) -> str:
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("created_at", "timestamp", "timestamp_unix", "time", "last_tick_at"):
            value = item.get(key)
            if value not in (None, ""):
                text = _format_timestamp(value)
                if text:
                    return text
    return ""


def _state_log_source(data: dict[str, Any]) -> Any:
    for container in (
        data.get("metadata"),
        data.get("reason"),
    ):
        if isinstance(container, dict):
            value = container.get("source")
            if value not in (None, "", [], {}):
                return value
    return None


def _format_state_summary(state: dict[str, Any]) -> str:
    parts = []
    for label, key in _STATE_FIELDS:
        value = state.get(key)
        if value in (None, "", [], {}):
            continue
        parts.append(f"{label}: {_format_field_value(key, value)}")
    return "状态: " + "  ·  ".join(parts) if parts else ""


def _format_compact_mapping(data: dict[str, Any]) -> str:
    state_summary = _format_state_summary(data)
    if state_summary:
        return state_summary.removeprefix("状态: ")
    parts = [
        f"{_field_label(str(key))}: {_format_field_value(str(key), value)}"
        for key, value in data.items()
        if value not in (None, "", [], {})
    ]
    return "  ·  ".join(parts[:4]) if parts else "—"


def _field_label(key: str) -> str:
    return _FIELD_LABELS.get(key, key)


def _activity_text(activity: dict[str, Any]) -> str:
    text = str(activity.get("text") or activity.get("state") or "空闲")
    agent = str(activity.get("agent") or "")
    model = str(activity.get("model") or "")
    suffix = " · ".join(part for part in (agent, model) if part)
    return f"{text}（{suffix}）" if suffix else text


__all__ = ["PersonaMindPage"]
