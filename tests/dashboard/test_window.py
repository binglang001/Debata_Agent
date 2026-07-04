"""Dashboard 主窗口和窗口组件回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

from types import SimpleNamespace

import pytest

QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
Qt = QtCore.Qt
QWidget = QtWidgets.QWidget

from ui.dashboard.layout import DEFAULT_LAYOUT
from ui.dashboard.main_window import DashboardWindow
from ui.widgets.window_chrome import _resize_edges_for_local_pos

from tests.dashboard.helpers import dashboard_runtime, remove_wheel_freeze_filters


def _close_dashboard_window(window: DashboardWindow, qapp) -> None:
    try:
        if getattr(window, "_current_page_key", None) != "overview":
            window._switch_to("overview")
            for _ in range(3):
                qapp.processEvents()
        remove_wheel_freeze_filters(window)
        for page in getattr(window, "_pages", {}).values():
            remove_wheel_freeze_filters(page)
            hidden = getattr(page, "on_hidden", None)
            if callable(hidden):
                hidden()
    finally:
        window.close()
        for _ in range(3):
            qapp.processEvents()
        window.deleteLater()
        for _ in range(3):
            qapp.processEvents()


def test_dashboard_content_width_uses_viewport_not_layout_stretch(qapp):
    page = SimpleNamespace()
    page._scroll = SimpleNamespace(
        viewport=lambda: SimpleNamespace(width=lambda: 960),
    )
    page._stack = SimpleNamespace(
        _min=0,
        _max=0,
        minimumWidth=lambda: page._stack._min,
        maximumWidth=lambda: page._stack._max,
        setMinimumWidth=lambda value: setattr(page._stack, "_min", value),
        setMaximumWidth=lambda value: setattr(page._stack, "_max", value),
        updateGeometry=lambda: None,
    )

    DashboardWindow._sync_content_width(page)

    assert page._stack._min == 960
    assert page._stack._max == 960

    page._scroll = SimpleNamespace(
        viewport=lambda: SimpleNamespace(width=lambda: DEFAULT_LAYOUT.page_max_width + 600),
    )
    DashboardWindow._sync_content_width(page)

    assert page._stack._min == DEFAULT_LAYOUT.page_max_width
    assert page._stack._max == DEFAULT_LAYOUT.page_max_width


def test_dashboard_theme_apply_short_circuits_same_resolved_theme(qapp, monkeypatch):
    import ui.dashboard.main_window as main_window_module

    class FakeApp:
        def __init__(self) -> None:
            self.stylesheets: list[str] = []

        def setStyleSheet(self, qss: str) -> None:
            self.stylesheets.append(qss)

    page = SimpleNamespace(
        _theme_choice="light",
        _current_theme="light",
        _applied_theme=None,
        _theme_btn=SimpleNamespace(
            text="",
            tooltip="",
            setText=lambda value: setattr(page._theme_btn, "text", value),
            setToolTip=lambda value: setattr(page._theme_btn, "tooltip", value),
        ),
    )
    fake_app = FakeApp()
    monkeypatch.setattr(main_window_module.QApplication, "instance", lambda: fake_app)
    monkeypatch.setattr(main_window_module, "cached_qss", lambda palette: f"qss:{palette.name}")

    DashboardWindow._apply_theme(page, "light")
    DashboardWindow._apply_theme(page, "light")
    DashboardWindow._apply_theme(page, "dark")

    assert fake_app.stylesheets == ["qss:light", "qss:dark"]
    assert page._theme_choice == "dark"
    assert page._current_theme == "dark"


def test_dashboard_settings_page_does_not_use_outer_scroll(qapp, tmp_paths):
    window = DashboardWindow(dashboard_runtime(tmp_paths))
    try:
        window.resize(DEFAULT_LAYOUT.default_width, DEFAULT_LAYOUT.default_height)
        window.show()
        qapp.processEvents()
        window._switch_to("settings")
        for _ in range(8):
            qapp.processEvents()

        settings = window._pages["settings"]
        assert window._scroll.verticalScrollBar().maximum() == 0
        assert settings._settings_nav.verticalScrollBar().maximum() == 0
        assert settings._status.parentWidget() is settings
        assert not settings._settings_scroll.isAncestorOf(settings._status)
    finally:
        _close_dashboard_window(window, qapp)


def test_dashboard_navigation_contains_persona_mind_page(qapp, tmp_paths):
    window = DashboardWindow(dashboard_runtime(tmp_paths))
    try:
        labels = [button.text() for button in window._nav_buttons.values()]

        assert "人格后台" in labels
        assert "persona_mind" in window._pages

        window._switch_to("persona_mind")

        assert window._stack.currentWidget() is window._pages["persona_mind"]
        assert window._nav_buttons["persona_mind"].property("active") == "true"
    finally:
        _close_dashboard_window(window, qapp)


def test_dashboard_switching_stops_hidden_timers_and_releases_animations(qapp, tmp_paths):
    window = DashboardWindow(dashboard_runtime(tmp_paths))
    try:
        for _ in range(20):
            qapp.processEvents()

        overview = window._pages["overview"]
        settings = window._pages["settings"]
        calls: list[str] = []
        original_overview_hidden = overview.on_hidden
        original_settings_shown = settings.on_shown

        def overview_hidden() -> None:
            calls.append("overview:hidden")
            original_overview_hidden()

        def settings_shown() -> None:
            calls.append("settings:shown")
            original_settings_shown()

        overview.on_hidden = overview_hidden
        settings.on_shown = settings_shown

        timer_count = len(window.findChildren(QtCore.QTimer))
        window._switch_to("settings")
        qapp.processEvents()

        assert calls == ["overview:hidden", "settings:shown"]
        assert not overview._timer.isActive()
        assert settings._provider_status_timer.isActive()

        for key in ("overview", "persona_mind", "chats", "settings") * 12:
            window._switch_to(key)
            qapp.processEvents()

        window._switch_to("overview")
        for _ in range(30):
            qapp.processEvents()

        assert len(window.findChildren(QtCore.QTimer)) == timer_count
        assert window._pages["overview"]._timer.isActive()
        assert not window._pages["persona_mind"]._timer.isActive()
        assert not window._pages["settings"]._provider_status_timer.isActive()
        assert not window._pages["settings"]._settings_content_sync_timer.isActive()
        assert not window._pages["chats"]._timer.isActive()
        assert not window._pages["chats"]._refresh_debounce_timer.isActive()
        assert not window._pages["chats"]._search_debounce_timer.isActive()
        assert len(window.findChildren(QtCore.QPropertyAnimation)) <= 1
        assert len(window.findChildren(QtWidgets.QGraphicsOpacityEffect)) <= 1
    finally:
        _close_dashboard_window(window, qapp)


def test_dashboard_close_pauses_and_show_resumes_live_timers(qapp, tmp_paths):
    window = DashboardWindow(dashboard_runtime(tmp_paths))
    try:
        window.show()
        qapp.processEvents()
        window._switch_to("settings")
        qapp.processEvents()
        timer_count = len(window.findChildren(QtCore.QTimer))

        assert window._status_timer.isActive()
        assert window._pages["settings"]._provider_status_timer.isActive()

        window.close()
        for _ in range(3):
            qapp.processEvents()

        assert len(window.findChildren(QtCore.QTimer)) == timer_count
        assert not window._status_timer.isActive()
        assert not window._pages["settings"]._provider_status_timer.isActive()

        window.show()
        for _ in range(3):
            qapp.processEvents()

        assert len(window.findChildren(QtCore.QTimer)) == timer_count
        assert window._status_timer.isActive()
        assert window._pages["settings"]._provider_status_timer.isActive()
    finally:
        _close_dashboard_window(window, qapp)


def test_window_resize_edges_cover_all_sides(qapp):
    window = QWidget()
    try:
        window.resize(300, 200)

        assert _resize_edges_for_local_pos(window, QtCore.QPoint(0, 100)) & Qt.Edge.LeftEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(299, 100)) & Qt.Edge.RightEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(150, 0)) & Qt.Edge.TopEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(150, 199)) & Qt.Edge.BottomEdge
        top_left = _resize_edges_for_local_pos(window, QtCore.QPoint(0, 0))
        assert top_left & Qt.Edge.LeftEdge
        assert top_left & Qt.Edge.TopEdge
    finally:
        window.deleteLater()
