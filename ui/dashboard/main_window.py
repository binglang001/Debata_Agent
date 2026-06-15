"""仪表盘主窗口。

左 200px 侧边导航（7 项） + 顶部 56px 状态栏 + 主体 QStackedWidget。
顶栏右侧带一个切主题按钮（无需放在向导里）。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app_config.loader import save_config

from ..theme import Spacing, cached_qss, palette_for_theme, resolve_theme_name
from .chats_page import ChatsPage
from .copy import DASHBOARD_COPY
from .layout import DEFAULT_LAYOUT, NAV_ITEMS, STATUS_BADGE_MAP
from .logs_page import LogsPage
from .memory_page import MemoryPage
from .models_page import ModelsPage
from .overview_page import OverviewPage
from .persona_mind_page import PersonaMindPage
from .personas_page import PersonasPage
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
        self._apply_icon()

        # 无边框 + 自定义标题栏 + 透明 root 让 WindowFrame 的圆角 QSS 生效
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setProperty("frameless", True)

        self._runtime = runtime
        self._theme_choice = self._configured_theme()
        self._current_theme = resolve_theme_name(self._theme_choice)
        self._applied_theme: str | None = None
        self._current_page_key: str | None = None

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
        from ..widgets import AutoSizeStack
        self._stack = AutoSizeStack()
        self._stack.setMaximumWidth(DEFAULT_LAYOUT.page_max_width)
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.viewport().installEventFilter(self)
        scroll_content = QWidget()
        scroll_lay = QHBoxLayout(scroll_content)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(0)
        scroll_lay.addStretch(1)
        scroll_lay.addWidget(self._stack, 0)
        scroll_lay.addStretch(1)
        self._scroll.setWidget(scroll_content)
        content_lay.addWidget(self._scroll, 1)

        right_lay.addWidget(content_wrap, 1)

        root_lay.addWidget(right, 1)
        self.setCentralWidget(root)
        from ..widgets import attach_size_grip, install_window_drag, install_window_resize

        self._size_grip = attach_size_grip(self)
        self._window_resize_filter = install_window_resize(root, self)
        self._window_drag_filter = install_window_drag(root, self)

        # 实例化 7 页
        self._pages: dict[str, QWidget] = {
            "overview": OverviewPage(runtime),
            "chats": ChatsPage(runtime),
            "memory": MemoryPage(runtime),
            "persona_mind": PersonaMindPage(runtime),
            "logs": LogsPage(runtime),
            "personas": PersonasPage(runtime),
            "models": ModelsPage(runtime),
            "settings": SettingsPage(runtime),
        }
        for p in self._pages.values():
            self._stack.addWidget(p)

        # settings 的 theme / restart 请求
        self._pages["settings"].theme_changed.connect(self._on_theme_changed)
        self._pages["settings"].restart_runtime_requested.connect(self.restart_requested.emit)
        self._pages["personas"].restart_requested.connect(self.restart_requested.emit)

        for key, page in self._pages.items():
            self._notify_page_hidden(key, page)

        # 默认显示 overview
        self._switch_to("overview")

        # 顶栏状态定时刷新
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(3000)
        self._status_timer.timeout.connect(self._refresh_topbar)
        self._status_timer.start()
        self._refresh_topbar()
        self._apply_theme(self._theme_choice)
        QTimer.singleShot(0, self._sync_content_width)
        QTimer.singleShot(800, self._show_feature_failures)

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
        brand = QLabel("Debata_Agent")
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

    def eventFilter(self, obj: object, event: QEvent) -> bool:  # noqa: N802
        scroll = getattr(self, "_scroll", None)
        if (
            scroll is not None
            and obj is scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._sync_content_width()
        return super().eventFilter(obj, event)

    def _sync_content_width(self) -> None:
        """Keep dashboard pages centered without letting layout stretch squeeze them."""
        scroll = getattr(self, "_scroll", None)
        stack = getattr(self, "_stack", None)
        if scroll is None or stack is None:
            return
        viewport_width = scroll.viewport().width()
        if viewport_width <= 0:
            return
        width = min(viewport_width, DEFAULT_LAYOUT.page_max_width)
        if stack.minimumWidth() == width and stack.maximumWidth() == width:
            return
        stack.setMinimumWidth(width)
        stack.setMaximumWidth(width)
        stack.updateGeometry()

    def _switch_to(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        previous_page = self._pages.get(self._current_page_key or "")
        if previous_page is not None and previous_page is not page:
            self._notify_page_hidden(self._current_page_key or "", previous_page)
            self._clear_page_animation(previous_page)
        self._stack.setCurrentWidget(page)
        self._animate_current_page(page)
        self._sync_content_width()
        self._scroll.verticalScrollBar().setValue(0)  # 切页回顶
        self._current_page_key = key
        self._notify_page_shown(key, page)
        # 更新导航高亮
        for k, btn in self._nav_buttons.items():
            active = "true" if k == key else "false"
            btn.setProperty("active", active)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _notify_page_shown(self, key: str, page: QWidget) -> None:
        shown = getattr(page, "on_shown", None)
        try:
            if callable(shown):
                shown()
            elif hasattr(page, "refresh"):
                page.refresh()
        except Exception:
            logger.warning(f"页面 {key} 显示刷新失败", exc_info=True)

    def _notify_page_hidden(self, key: str, page: QWidget) -> None:
        hidden = getattr(page, "on_hidden", None)
        if not callable(hidden):
            return
        try:
            hidden()
        except Exception:
            logger.warning(f"页面 {key} 隐藏处理失败", exc_info=True)

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

    def _show_feature_failures(self) -> None:
        failures = getattr(self._runtime, "feature_failures", {}) or {}
        if not failures:
            return
        from ..widgets import show_message

        names = {
            "asr": "语音识别（ASR）",
            "tts": "语音合成（TTS）",
            "embedding": "RAG / Embedding",
        }
        lines = [
            f"{names.get(name, name)}：{msg}"
            for name, msg in failures.items()
        ]
        show_message(
            self,
            "本地/云端模型加载失败",
            "以下功能加载失败，已自动关闭对应配置以避免下次启动继续卡住：\n\n"
            + "\n".join(lines)
            + "\n\n请在设置页或模型管理页查看安装指引，重新放置或修复后再启用。",
            is_danger=True,
        )

    # ============================================================
    # 主题
    # ============================================================

    def _toggle_theme(self) -> None:
        target = "dark" if self._current_theme == "light" else "light"
        self._on_theme_changed(target)

    @staticmethod
    def _apply_icon() -> None:
        from pathlib import Path

        from PySide6.QtGui import QIcon
        icon_path = Path(__file__).parent.parent / "icon.png"
        if icon_path.exists():
            QApplication.instance().setWindowIcon(QIcon(str(icon_path)))

    def _configured_theme(self) -> str:
        try:
            return self._runtime.config.app.theme
        except Exception:
            return "auto"

    def _on_theme_changed(self, target: str) -> None:
        self._persist_theme(target)
        self._apply_theme(target)
        settings = getattr(self, "_pages", {}).get("settings")
        if (
            settings is not None
            and hasattr(settings, "refresh")
            and self._stack.currentWidget() is settings
        ):
            settings.refresh()

    def _apply_theme(self, target: str) -> None:
        palette = palette_for_theme(target)
        resolved = resolve_theme_name(target)
        app = QApplication.instance()
        if app and self._applied_theme != resolved:
            app.setStyleSheet(cached_qss(palette))
            self._applied_theme = resolved
        self._theme_choice = target
        self._current_theme = resolved
        self._theme_btn.setText("☀" if self._current_theme == "dark" else "☾")
        self._theme_btn.setToolTip(
            f"{DASHBOARD_COPY['topbar.theme_toggle']}（当前：{'跟随系统' if target == 'auto' else target}）"
        )

    def _persist_theme(self, target: str) -> None:
        try:
            cfg = self._runtime.config
            if cfg.app.theme == target:
                return
            cfg.app.theme = target
            save_config(self._runtime.paths, cfg)
        except Exception:
            logger.warning("保存主题设置失败", exc_info=True)

    def _animate_current_page(self, page: QWidget) -> None:
        try:
            from PySide6.QtCore import QEasingCurve, QPropertyAnimation
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            self._clear_page_animation(page)
            effect = QGraphicsOpacityEffect(page)
            page.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity")
            anim.setDuration(120)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            page._page_fade_effect = effect  # type: ignore[attr-defined]
            page._page_fade_animation = anim  # type: ignore[attr-defined]

            def finish_animation() -> None:
                if getattr(page, "_page_fade_animation", None) is anim:
                    page._page_fade_animation = None  # type: ignore[attr-defined]
                if getattr(page, "_page_fade_effect", None) is effect:
                    page._page_fade_effect = None  # type: ignore[attr-defined]
                try:
                    if page.graphicsEffect() is effect:
                        page.setGraphicsEffect(None)
                except RuntimeError:
                    pass
                try:
                    effect.deleteLater()
                except RuntimeError:
                    pass
                try:
                    anim.deleteLater()
                except RuntimeError:
                    pass

            anim.finished.connect(finish_animation)
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        except Exception:
            pass

    def _clear_page_animation(self, page: QWidget) -> None:
        anim = getattr(page, "_page_fade_animation", None)
        if anim is not None:
            page._page_fade_animation = None  # type: ignore[attr-defined]
            try:
                anim.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                anim.stop()
            except RuntimeError:
                pass

        effect = getattr(page, "_page_fade_effect", None)
        if effect is not None:
            page._page_fade_effect = None  # type: ignore[attr-defined]
            try:
                if page.graphicsEffect() is effect:
                    page.setGraphicsEffect(None)
            except RuntimeError:
                pass
            try:
                effect.deleteLater()
            except RuntimeError:
                pass

    def notify_runtime_restart_finished(self, ok: bool, message: str = "") -> None:
        settings = getattr(self, "_pages", {}).get("settings")
        if settings is not None and hasattr(settings, "on_runtime_restart_finished"):
            settings.on_runtime_restart_finished(ok, message)

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
        from ..widgets import apply_rounded_mask, position_size_grip
        apply_rounded_mask(self, radius=12)
        position_size_grip(self, getattr(self, "_size_grip", None))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        from ..widgets import fade_in_window

        fade_in_window(self)

    def nativeEvent(self, eventType, message):  # type: ignore[override]
        from ..widgets import native_resize_hit_test

        hit = native_resize_hit_test(self, eventType, message)
        if hit is not None:
            return hit
        return super().nativeEvent(eventType, message)


__all__ = ["DashboardWindow"]
