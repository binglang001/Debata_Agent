"""系统托盘 —— 左键打开仪表盘 + 右键菜单。

托盘图标按状态变色：
    未连接 灰
    已连接 绿
    出错   红
    工作中 蓝

不依赖外部图标文件 —— 用 QPainter 在 QPixmap 上画一个圆形。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .dashboard.copy import DASHBOARD_COPY
from .theme import LIGHT

logger = logging.getLogger(__name__)


_STATUS_COLORS: dict[str, str] = {
    "connected": LIGHT.success,
    "connecting": LIGHT.warning,
    "disconnected": "#9B9286",
    "error": LIGHT.error,
}


def _make_icon(color_hex: str, size: int = 32) -> QIcon:
    """画一个圆形托盘图标。"""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    color = QColor(color_hex)
    painter.setBrush(QBrush(color))
    painter.setPen(QPen(QColor("#2B2622"), 1))
    inset = 2
    painter.drawEllipse(inset, inset, size - inset * 2, size - inset * 2)
    painter.end()
    return QIcon(pix)


class Tray(QSystemTrayIcon):
    """系统托盘。把 callbacks 注入构造器即可。"""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_quit: Callable[[], None],
        on_restart: Callable[[], None] | None = None,
        runtime_provider: Callable[[], Any] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self._on_open = on_open
        self._on_quit = on_quit
        self._on_restart = on_restart
        self._get_runtime = runtime_provider

        self.setToolTip(DASHBOARD_COPY["window.title"])
        self.set_status("disconnected")

        # 菜单
        menu = QMenu()

        act_open = QAction(DASHBOARD_COPY["tray.menu_dashboard"], menu)
        act_open.triggered.connect(self._on_open)
        menu.addAction(act_open)

        if on_restart is not None:
            act_restart = QAction(DASHBOARD_COPY["tray.menu_restart"], menu)
            act_restart.triggered.connect(on_restart)
            menu.addAction(act_restart)

        menu.addSeparator()

        act_quit = QAction(DASHBOARD_COPY["tray.menu_quit"], menu)
        act_quit.triggered.connect(self._on_quit)
        menu.addAction(act_quit)

        self.setContextMenu(menu)

        # 左键打开仪表盘
        self.activated.connect(self._on_activated)

        # 状态刷新
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(4000)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_open()

    def set_status(self, status: str) -> None:
        color = _STATUS_COLORS.get(status, _STATUS_COLORS["disconnected"])
        self.setIcon(_make_icon(color))

    def _refresh_status(self) -> None:
        if not self._get_runtime:
            return
        try:
            rt = self._get_runtime()
            if rt is None or rt.adapter is None:
                self.set_status("disconnected")
                return
            self.set_status("connected" if rt.adapter.is_connected else "disconnected")
        except Exception:
            self.set_status("error")


__all__ = ["Tray"]
