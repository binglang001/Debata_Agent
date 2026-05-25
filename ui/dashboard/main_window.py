"""仪表盘主窗口。

左 200px 侧边导航（7 项） + 顶部 56px 状态栏 + 主体 QStackedWidget。
顶栏右侧带一个切主题按钮（无需放在向导里）。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..theme import DARK, LIGHT, Spacing, build_qss
from .copy import DASHBOARD_COPY
from .layout import DEFAULT_LAYOUT, NAV_ITEMS, STATUS_BADGE_MAP
from .chats_page import ChatsPage
from .logs_page import LogsPage
from .memory_page import MemoryPage
from .overview_page import OverviewPage
from .personas_page import PersonasPage
from .plugins_page import PluginsPage
from .settings_page import SettingsPage

logger = logging.getLogger(__name__)


class DashboardWindow(QMainWindow):
    """仪表盘主窗口。"""

    quit_requested = Signal()
    restart_requested = Signal()

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(DASHBOARD_COPY["window.title"])
        self.setMinimumSize(DEFAULT_LAYOUT.min_width, DEFAULT_LAYOUT.min_height)
        self.resize(DEFAULT_LAYOUT.default_width, DEFAULT_LAYOUT.default_height)

        # 无边框 + 自定义标题栏 + 透明 root 让 WindowFrame 的圆角 QSS 生效
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._runtime = runtime
        self._current_theme = "light"

        # 整窗用 WindowFrame 包一层：QSS 里 QFrame#WindowFrame 设了 border-radius
        root = QFrame()
        root.setObjectName("WindowFrame")
        root_lay = QHBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # 左侧导航
        sidebar = self._build_sidebar()
        root_lay.addWidget(sidebar)

        # 右侧：顶栏 + 内容
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)

        topbar = self._build_topbar()
        right_lay.addWidget(topbar)

        # 内容栈
        content_wrap = QWidget()
        content_lay = QVBoxLayout(content_wrap)
        content_lay.setContentsMargins(
            DEFAULT_LAYOUT.content_padding,
            DEFAULT_LAYOUT.content_padding,
            DEFAULT_LAYOUT.content_padding,
            DEFAULT_LAYOUT.content_padding,
        )
        content_lay.setSpacing(0)

        # stack 包 ScrollArea，仅在当前页溢出时滚动（AutoSizeStack 让 sizeHint 跟当前页走）
        from PySide6.QtWidgets import QScrollArea
        from ..widgets import AutoSizeStack
        self._stack = AutoSizeStack()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(self._stack)
        content_lay.addWidget(scroll, 1)

        right_lay.addWidget(content_wrap, 1)

        root_lay.addWidget(right, 1)
        self.setCentralWidget(root)

        # 实例化 7 页
        self._pages: dict[str, QWidget] = {
            "overview": OverviewPage(runtime),
            "chats": ChatsPage(runtime),
            "memory": MemoryPage(runtime),
            "logs": LogsPage(runtime),
            "personas": PersonasPage(runtime),
            "plugins": PluginsPage(runtime),
            "settings": SettingsPage(runtime),
        }
        for p in self._pages.values():
            self._stack.addWidget(p)

        # settings 的 theme / restart 请求
        self._pages["settings"].theme_changed.connect(self._on_theme_changed)
        self._pages["settings"].restart_runtime_requested.connect(self.restart_requested.emit)

        # 默认显示 overview
        self._switch_to("overview")

        # 顶栏状态定时刷新
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(3000)
        self._status_timer.timeout.connect(self._refresh_topbar)
        self._status_timer.start()
        self._refresh_topbar()

    # ============================================================
    # UI 构造
    # ============================================================

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(DEFAULT_LAYOUT.sidebar_width)

        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, Spacing.MD, 0, Spacing.MD)
        lay.setSpacing(Spacing.XS)

        # 顶部 Logo / 标题
        brand = QLabel("Diana_Agent")
        brand.setProperty("role", "title-3")
        brand.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.MD)
        lay.addWidget(brand)

        # 导航按钮
        self._nav_buttons: dict[str, QPushButton] = {}
        for item in NAV_ITEMS:
            btn = QPushButton(DASHBOARD_COPY[f"nav.{item.key}"])
            btn.setProperty("role", "nav")
            btn.setProperty("active", "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked, k=item.key: self._switch_to(k))
            self._nav_buttons[item.key] = btn
            lay.addWidget(btn)

        lay.addStretch(1)

        # 底部：退出
        quit_btn = QPushButton(DASHBOARD_COPY["tray.menu_quit"])
        quit_btn.setProperty("role", "text")
        quit_btn.clicked.connect(self._on_quit_clicked)
        lay.addWidget(quit_btn)

        return bar

    def _build_topbar(self) -> QWidget:
        from ..widgets import DragBar, make_window_controls

        bar = DragBar(self)
        bar.setObjectName("Topbar")
        bar.setFixedHeight(DEFAULT_LAYOUT.topbar_height)

        lay = QHBoxLayout(bar)
        lay.setContentsMargins(Spacing.XL, 0, 0, 0)
        lay.setSpacing(Spacing.LG)

        self._adapter_badge = QLabel("…")
        self._adapter_badge.setProperty("role", "badge-idle")
        lay.addWidget(self._adapter_badge)

        self._persona_label = QLabel("—")
        self._persona_label.setProperty("role", "secondary")
        lay.addWidget(self._persona_label)

        lay.addStretch(1)

        self._theme_btn = QPushButton("☾")  # 不依赖图标资源
        self._theme_btn.setProperty("role", "icon")
        self._theme_btn.setToolTip(DASHBOARD_COPY["topbar.theme_toggle"])
        self._theme_btn.clicked.connect(self._toggle_theme)
        lay.addWidget(self._theme_btn)

        # 窗口控制按钮
        lay.addSpacing(Spacing.SM)
        lay.addWidget(make_window_controls(self))

        return bar

    # ============================================================
    # 导航 / 状态
    # ============================================================

    def _switch_to(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        self._stack.setCurrentWidget(page)
        if hasattr(page, "refresh"):
            try:
                page.refresh()
            except Exception:
                logger.warning(f"页面 {key} 刷新失败", exc_info=True)
        # 更新导航高亮
        for k, btn in self._nav_buttons.items():
            active = "true" if k == key else "false"
            btn.setProperty("active", active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _refresh_topbar(self) -> None:
        rt = self._runtime
        if rt is None:
            return
        try:
            connected = bool(getattr(rt.adapter, "is_connected", False)) if rt.adapter else False
            if connected:
                self._set_badge(
                    DASHBOARD_COPY["topbar.adapter_connected"],
                    STATUS_BADGE_MAP["connected"],
                )
            else:
                self._set_badge(
                    DASHBOARD_COPY["topbar.adapter_disconnected"],
                    STATUS_BADGE_MAP["disconnected"],
                )
            name = rt.persona.name if rt.persona else "—"
            self._persona_label.setText(
                DASHBOARD_COPY["topbar.persona_label"] + name
            )
        except Exception as e:  # noqa: BLE001
            self._set_badge(DASHBOARD_COPY["topbar.adapter_error"], STATUS_BADGE_MAP["error"])
            self._persona_label.setText(f"出错：{e}")

    def _set_badge(self, text: str, role: str) -> None:
        self._adapter_badge.setText(text)
        self._adapter_badge.setProperty("role", role)
        self._adapter_badge.style().unpolish(self._adapter_badge)
        self._adapter_badge.style().polish(self._adapter_badge)

    # ============================================================
    # 主题
    # ============================================================

    def _toggle_theme(self) -> None:
        target = "dark" if self._current_theme == "light" else "light"
        self._on_theme_changed(target)

    def _on_theme_changed(self, target: str) -> None:
        palette = DARK if target == "dark" else LIGHT
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_qss(palette))
        self._current_theme = target
        self._theme_btn.setText("☀" if target == "dark" else "☾")

    # ============================================================
    # 关闭
    # ============================================================

    def _on_quit_clicked(self) -> None:
        self.quit_requested.emit()
        self.close()

    # ============================================================
    # 圆角 mask（frameless 窗口 OS 层面真圆角）
    # ============================================================

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        from ..widgets import apply_rounded_mask
        apply_rounded_mask(self, radius=12)


__all__ = ["DashboardWindow"]
