"""总览页 —— 4 张卡片。

卡片：
    1. 渠道状态（NapCat 连接、地址、可重连）
    2. 模型健康（所有 provider 的连通性 + 大致延迟）
    3. 近 24 小时（消息/工具/token —— 当前简化为"已收/已发"两项）
    4. 当前人格（仅显示名称，提示去 personas 页切换）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from .copy import DASHBOARD_COPY
from .layout import STATUS_BADGE_MAP

logger = logging.getLogger(__name__)


class _StatCard(QFrame):
    """卡片：标题 + 内容区 + 可选的右上角小按钮。"""

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SectionCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._title_label = QLabel(title)
        self._title_label.setProperty("role", "title-3")
        head.addWidget(self._title_label)
        head.addStretch(1)
        self._right_label = QLabel("")
        self._right_label.setProperty("role", "badge-idle")
        head.addWidget(self._right_label)
        outer.addLayout(head)

        self._body = QVBoxLayout()
        self._body.setSpacing(Spacing.XS)
        outer.addLayout(self._body)
        outer.addStretch(1)

    def set_badge(self, text: str, role: str = "badge-idle") -> None:
        self._right_label.setText(text)
        self._right_label.setProperty("role", role)
        self._right_label.style().unpolish(self._right_label)
        self._right_label.style().polish(self._right_label)

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

    def add_widget(self, w: QWidget) -> None:
        self._body.addWidget(w)


class OverviewPage(QWidget):
    """4 张卡片的总览页。每 5 秒刷新一次。"""

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        grid = QGridLayout()
        grid.setSpacing(Spacing.MD)

        self._adapter_card = _StatCard(DASHBOARD_COPY["overview.adapter_title"])
        self._providers_card = _StatCard(DASHBOARD_COPY["overview.providers_title"])
        self._stats_card = _StatCard(DASHBOARD_COPY["overview.stats_title"])
        self._persona_card = _StatCard(DASHBOARD_COPY["topbar.persona_label"].rstrip("：:"))

        grid.addWidget(self._adapter_card, 0, 0)
        grid.addWidget(self._providers_card, 0, 1)
        grid.addWidget(self._stats_card, 1, 0)
        grid.addWidget(self._persona_card, 1, 1)
        outer.addLayout(grid)
        outer.addStretch(1)

        # 5 秒刷新
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

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
            for name in (rt.providers or {}):
                self._providers_card.add_line(name, DASHBOARD_COPY["overview.provider_status_ok"])
            if not rt.providers:
                self._providers_card.add_line("—", "未装配")
            self._providers_card.set_badge("", "badge-idle")
        except Exception as e:  # noqa: BLE001
            self._providers_card.add_line("出错", str(e)[:80])

        # —— 近 24 小时（简化：只显示历史长度）——
        self._stats_card.clear_body()
        try:
            # 这里走 best-effort：直接读 history 缓存长度（不发起 IO）
            length = rt._hist_len if hasattr(rt, "_hist_len") else 0
            self._stats_card.add_line("历史条数", str(length))
            self._stats_card.add_line(
                "重要记忆",
                str(len(rt.important.items())) if rt.important else "0",
            )
            self._stats_card.set_badge("", "badge-idle")
        except Exception as e:  # noqa: BLE001
            self._stats_card.add_line("出错", str(e)[:80])

        # —— 当前人格 ——
        self._persona_card.clear_body()
        try:
            name = rt.persona.name if rt.persona else "—"
            self._persona_card.add_line("名称", name)
            hint = QLabel("在「角色」页里切换或新建")
            hint.setProperty("role", "secondary")
            hint.setWordWrap(True)
            self._persona_card.add_widget(hint)
        except Exception as e:  # noqa: BLE001
            self._persona_card.add_line("出错", str(e)[:80])
