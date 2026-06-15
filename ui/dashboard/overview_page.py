"""总览页。

卡片：
    1. 主模型实时状态
    2. 模型健康（显示 Runtime 启动时的 provider 连通性检测）
    3. 渠道状态（NapCat 连接、地址、可重连）
    4. 用量统计（请求数 / token / KV 缓存）
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from .copy import DASHBOARD_COPY
from .layout import STATUS_BADGE_MAP

logger = logging.getLogger(__name__)


def _fmt_int(value: int) -> str:
    return f"{int(value):,}"


def _fmt_rate(value: float) -> str:
    return f"{value * 100:.1f}%"


def _provider_badge(
    ok_count: int,
    err_count: int,
    total: int,
    errors: list[str],
) -> tuple[str, str, str]:
    if total <= 0:
        return "未装配", "badge-idle", ""
    if err_count <= 0 and ok_count == total:
        return "全部可用", "badge-success", f"{ok_count}/{total} 可用"
    if ok_count > 0:
        reason = _dominant_error(errors)
        return f"{ok_count}/{total} 可用", "badge-warning", f"部分可用 {ok_count}/{total} · {reason}"
    reason = _dominant_error(errors)
    return "全部异常", "badge-error", f"全部异常 · {reason}"


def _dominant_error(errors: list[str]) -> str:
    if not errors:
        return "检测中"
    joined = " ".join(errors)
    if "鉴权" in joined or "API 密钥" in joined:
        return "鉴权错误"
    if "超时" in joined:
        return "请求超时"
    if "限流" in joined:
        return "被限流"
    if "余额" in joined or "计费" in joined:
        return "计费异常"
    if "网络不通" in joined:
        return "网络异常"
    if "模型" in joined or "接口不存在" in joined:
        return "模型异常"
    return "请求报错"


def _activity_face(state: str) -> str:
    if state == "thinking":
        return "思考中"
    if state == "tool":
        return "调用工具"
    if state == "error":
        return "出错"
    return "空闲"


class _StatCard(QFrame):
    """卡片：标题 + 内容区 + 可选的右上角小按钮。"""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SectionCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._head = head
        self._title_label = QLabel(title)
        self._title_label.setProperty("role", "title-3")
        head.addWidget(self._title_label)
        head.addStretch(1)
        self._right_label = QLabel("")
        self._right_label.setProperty("role", "badge-idle")
        self._right_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._right_label.setWordWrap(False)
        self._right_label.setMaximumWidth(112)
        self._right_label.setMaximumHeight(22)
        self._right_label.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        head.addWidget(self._right_label)
        outer.addLayout(head)

        self._body = QVBoxLayout()
        self._body.setSpacing(Spacing.SM)
        outer.addLayout(self._body)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_badge(self, text: str, role: str = "badge-idle", tooltip: str = "") -> None:
        if not text:
            self.clear_badge()
            return
        self._right_label.show()
        self._right_label.setText(text)
        self._right_label.setToolTip(tooltip or text)
        self._right_label.setProperty("role", role)
        self._right_label.style().unpolish(self._right_label)
        self._right_label.style().polish(self._right_label)

    def clear_badge(self) -> None:
        self._right_label.hide()
        self._right_label.clear()
        self._right_label.setToolTip("")

    def clear_body(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def add_line(self, key: str, value: str) -> None:
        row = QHBoxLayout()
        k = QLabel(key)
        k.setProperty("role", "secondary")
        k.setFixedWidth(96)
        row.addWidget(k)
        v = QLabel(value)
        v.setWordWrap(True)
        row.addWidget(v, 1)
        wrap = QWidget()
        wrap.setLayout(row)
        self._body.addWidget(wrap)

    def add_status_line(self, key: str, value: str) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Spacing.SM)
        k = QLabel(key)
        k.setProperty("role", "secondary")
        k.setWordWrap(False)
        row.addWidget(k, 1)
        v = QLabel(value)
        v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        v.setWordWrap(False)
        v.setMinimumWidth(132)
        row.addWidget(v, 0)
        wrap = QWidget()
        wrap.setLayout(row)
        self._body.addWidget(wrap)

    def add_metric_row(self, items: list[tuple[str, str]]) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Spacing.LG)
        for label, value in items:
            box = QVBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(Spacing.XS)
            k = QLabel(label)
            k.setProperty("role", "secondary")
            k.setAlignment(Qt.AlignmentFlag.AlignLeft)
            v = QLabel(value)
            v.setProperty("role", "title-3")
            v.setAlignment(Qt.AlignmentFlag.AlignLeft)
            box.addWidget(k)
            box.addWidget(v)
            wrap = QWidget()
            wrap.setLayout(box)
            wrap.setMinimumWidth(112)
            row.addWidget(wrap, 1)
        outer = QWidget()
        outer.setLayout(row)
        self._body.addWidget(outer)

    def add_widget(self, w: QWidget) -> None:
        self._body.addWidget(w)

    def set_header_control(self, widget: QWidget) -> None:
        self._right_label.hide()
        self._head.addWidget(widget)


class OverviewPage(QWidget):
    """4 张卡片的总览页。每 1 秒刷新一次。"""

    _NARROW_WIDTH = 760

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        outer.addWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(Spacing.MD)

        self._adapter_card = _StatCard(DASHBOARD_COPY["overview.adapter_title"])
        self._providers_card = _StatCard(DASHBOARD_COPY["overview.providers_title"])
        self._usage_card = _StatCard("用量统计")
        self._activity_card = _StatCard("主模型状态")
        self._activity_card.setMinimumHeight(150)
        self._providers_card.setMinimumHeight(190)
        self._adapter_card.setMinimumHeight(190)
        self._usage_card.setMinimumHeight(190)

        self._usage_range_combo = QComboBox()
        self._usage_range_combo.addItem("本日", "today")
        self._usage_range_combo.addItem("7 天", "7d")
        self._usage_range_combo.addItem("30 天", "30d")
        self._usage_range_combo.addItem("总计", "all")
        self._usage_range_combo.currentIndexChanged.connect(lambda *_: self.refresh())
        self._usage_card.set_header_control(self._usage_range_combo)

        self._middle_row = QWidget()
        self._middle_grid = QGridLayout(self._middle_row)
        self._middle_grid.setContentsMargins(0, 0, 0, 0)
        self._middle_grid.setSpacing(Spacing.MD)
        self._middle_narrow = False

        content_layout.addWidget(self._activity_card)
        content_layout.addWidget(self._middle_row)
        content_layout.addWidget(self._usage_card)
        content_layout.addStretch(1)

        self._apply_responsive_layout(self.width())

        # 1 秒刷新
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_responsive_layout(event.size().width())

    def _apply_responsive_layout(self, width: int) -> None:
        narrow = width < self._NARROW_WIDTH
        if narrow == self._middle_narrow and self._middle_grid.count():
            return
        self._middle_narrow = narrow
        self._middle_grid.removeWidget(self._providers_card)
        self._middle_grid.removeWidget(self._adapter_card)
        if narrow:
            self._middle_grid.addWidget(self._providers_card, 0, 0)
            self._middle_grid.addWidget(self._adapter_card, 1, 0)
            self._middle_grid.setColumnStretch(0, 1)
            self._middle_grid.setColumnStretch(1, 0)
            self._middle_grid.setRowStretch(0, 0)
            self._middle_grid.setRowStretch(1, 0)
        else:
            self._middle_grid.addWidget(self._providers_card, 0, 0)
            self._middle_grid.addWidget(self._adapter_card, 0, 1)
            self._middle_grid.setColumnStretch(0, 1)
            self._middle_grid.setColumnStretch(1, 1)
            self._middle_grid.setRowStretch(0, 0)
            self._middle_grid.setRowStretch(1, 0)

    def refresh(self) -> None:
        rt = self._runtime
        if rt is None:
            return

        # —— 渠道状态 ——
        self._adapter_card.clear_body()
        try:
            connected = bool(getattr(rt.adapter, "is_connected", False)) if rt.adapter else False
            if connected:
                self._adapter_card.set_badge(
                    DASHBOARD_COPY["topbar.adapter_connected"],
                    STATUS_BADGE_MAP["connected"],
                )
            else:
                self._adapter_card.set_badge(
                    DASHBOARD_COPY["topbar.adapter_disconnected"],
                    STATUS_BADGE_MAP["disconnected"],
                )
            cfg = next(iter(rt.config.adapters.values())) if rt.config else None
            if cfg:
                self._adapter_card.add_line(
                    DASHBOARD_COPY["overview.adapter_address_label"],
                    f"{cfg.mode} · {cfg.host}:{cfg.port}{cfg.path}",
                )
        except Exception as e:  # noqa: BLE001
            self._adapter_card.set_badge(DASHBOARD_COPY["topbar.adapter_error"],
                                          STATUS_BADGE_MAP["error"])
            self._adapter_card.add_line("出错", str(e)[:80])

        # —— 模型健康 ——
        self._providers_card.clear_body()
        try:
            health = getattr(rt, "provider_health", {}) or {}
            ok_count = 0
            err_count = 0
            errors: list[str] = []
            for name in (rt.providers or {}):
                item = health.get(name)
                if item is None:
                    self._providers_card.add_status_line(name, "检测中")
                    continue
                if getattr(item, "status", "") == "ok":
                    ok_count += 1
                    latency = getattr(item, "latency_ms", 0)
                    value = f"可用 · {latency}ms" if latency else "可用"
                else:
                    err_count += 1
                    message = getattr(item, "message", "无响应")
                    errors.append(message)
                    value = f"异常 · {_dominant_error([message])}"
                self._providers_card.add_status_line(name, value)
            if not rt.providers:
                self._providers_card.add_status_line("—", "未装配")
                self._providers_card.set_badge("未装配", "badge-idle")
            else:
                badge, role, tooltip = _provider_badge(
                    ok_count,
                    err_count,
                    len(rt.providers),
                    errors,
                )
                self._providers_card.set_badge(badge, role, tooltip=tooltip)
        except Exception as e:  # noqa: BLE001
            self._providers_card.add_line("出错", str(e)[:80])

        # —— 用量统计 ——
        self._usage_card.clear_body()
        self._usage_card.clear_badge()
        try:
            usage_store = getattr(rt, "usage_stats", None)
            range_name = self._usage_range_combo.currentData() or "today"
            if usage_store is None:
                self._usage_card.add_metric_row([("状态", "未就绪")])
            else:
                summary = usage_store.summarize(range_name)
                self._usage_card.add_metric_row(
                    [
                        ("请求数", _fmt_int(summary.request_count)),
                        ("总 token", _fmt_int(summary.total_tokens)),
                        ("KV 命中率", _fmt_rate(summary.cache_hit_rate)),
                    ]
                )
                self._usage_card.add_metric_row(
                    [
                        ("输入 token", _fmt_int(summary.prompt_tokens)),
                        ("输出 token", _fmt_int(summary.completion_tokens)),
                        ("思考 token", _fmt_int(summary.reasoning_tokens)),
                    ]
                )
                self._usage_card.add_metric_row(
                    [
                        ("KV 命中 token", _fmt_int(summary.cached_tokens)),
                        ("KV 写入 token", _fmt_int(summary.cache_creation_tokens)),
                    ]
                )
        except Exception as e:  # noqa: BLE001
            self._usage_card.add_line("出错", str(e)[:80])

        # —— 主模型实时状态 ——
        self._activity_card.clear_body()
        try:
            activity = getattr(rt, "model_activity", {}) or {}
            state = str(activity.get("state") or "idle")
            face = QLabel(_activity_face(state))
            face.setProperty("role", "title-2")
            self._activity_card.add_widget(face)
            status_text = "空闲" if state == "idle" else str(activity.get("text") or "空闲")
            self._activity_card.add_line("状态", status_text)
            model = str(activity.get("model") or "")
            if model:
                self._activity_card.add_line("模型", model)
            tools = activity.get("tool_names") or []
            if tools:
                self._activity_card.add_line("工具", "、".join(map(str, tools)))
            role = "badge-success" if state == "idle" else "badge-warning"
            if state == "error":
                role = "badge-error"
            self._activity_card.set_badge(str(activity.get("agent") or "主模型"), role)
        except Exception as e:  # noqa: BLE001
            self._activity_card.add_line("出错", str(e)[:80])
