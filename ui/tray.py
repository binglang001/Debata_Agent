"""系统托盘 —— 左键打开仪表盘 + 右键菜单。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .dashboard.copy import DASHBOARD_COPY

logger = logging.getLogger(__name__)


def _load_tray_icon() -> QIcon:
    """加载 Debata 头像作为托盘图标。"""
    from pathlib import Path
    icon_path = Path(__file__).parent / "icon.png"
    if icon_path.exists():
        return QIcon(str(icon_path))
    # fallback：画一个灰色圆点
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QBrush(QColor("#9B9286")))
    painter.setPen(QPen(QColor("#2B2622"), 1))
    painter.drawEllipse(2, 2, 28, 28)
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
        self.setIcon(_load_tray_icon())

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

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_open()


__all__ = ["Tray"]
