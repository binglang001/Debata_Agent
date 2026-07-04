"""设置页回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
QLabel = QtWidgets.QLabel

from app_config.loader import load_config
from app_config.schema import (
    EmbeddingFeatureConfig,
    FeaturesConfig,
    LongTermMemoryConfig,
    ProviderConfig,
    VisionFeatureConfig,
)
from ui.dashboard.settings_page import SettingsPage

from tests.dashboard.helpers import close_settings_page, dashboard_runtime, minimal_root_config


def test_settings_restore_opened_config_writes_snapshot(tmp_paths):
    cfg = minimal_root_config()
    opened = cfg.model_copy(deep=True)
    cfg.app.theme = "dark"

    class FakeStatus:
        def __init__(self):
            self.calls = []
            self.error = ""

        def set_changes(self, count: int, *, needs_restart: bool) -> None:
            self.calls.append((count, needs_restart))

        def mark_error(self, msg: str) -> None:
            self.error = msg

    class Dummy:
        def __init__(self):
            self._opened_snapshot = opened
            self._runtime = type("RuntimeStub", (), {"paths": tmp_paths, "config": cfg})()
            self._baseline = cfg.model_copy(deep=True)
            self._status = FakeStatus()
            self.refreshed = False

        def refresh(self) -> None:
            self.refreshed = True

    page = Dummy()
    SettingsPage._restore_opened_config(page)

    assert page._runtime.config.app.theme == "auto"
    assert page._status.calls[-1] == (0, True)
    assert page.refreshed is True


def test_settings_page_uses_navigation_sections(qapp, tmp_paths):
    cfg = minimal_root_config()

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        labels = [page._settings_nav.item(i).text() for i in range(page._settings_nav.count())]
    finally:
        close_settings_page(page, qapp)

    assert "软件行为" in labels
    assert "Token预算" in labels
    assert "日志与诊断" in labels
    assert "表情包" not in labels
    assert "角色" not in labels
    assert "外观" not in labels


def test_settings_page_tool_loop_reminder_replaces_legacy_max_loops(qapp, tmp_paths):
    from ui.dashboard.settings_page import CollapsibleSection

    cfg = minimal_root_config()
    cfg.agents.chat.tool_loop_reminder_interval = 11
    cfg.agents.chat.tool_loop_final_warning_count = 3
    cfg.agents.chat.tool_loop_final_grace_loops = 2
    runtime = dashboard_runtime(tmp_paths, cfg)
    page = SettingsPage(runtime)
    try:
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "工具循环提醒" in labels
        assert "工具轮数提醒间隔" in labels
        assert "最终警告前提醒次数" in labels
        assert "最终警告后宽限轮数" in labels
        assert "普通硬停止" in labels
        assert "工具轮数上限" not in labels
        assert "最大工具轮数" not in labels

        reminder_section = next(
            section
            for section in page.findChildren(CollapsibleSection)
            if section.findChildren(QtWidgets.QLabel)[0].text() == "工具循环提醒"
        )
        assert reminder_section._body.isHidden()

        interval = page.findChild(QtWidgets.QSpinBox, "toolLoopReminderIntervalSpin")
        warning = page.findChild(QtWidgets.QSpinBox, "toolLoopFinalWarningCountSpin")
        grace = page.findChild(QtWidgets.QSpinBox, "toolLoopFinalGraceLoopsSpin")
        assert interval is not None
        assert warning is not None
        assert grace is not None
        assert interval.value() == 11
        assert warning.value() == 3
        assert grace.value() == 2

        interval.setValue(12)
        interval.editingFinished.emit()
        warning.setValue(4)
        warning.editingFinished.emit()
        grace.setValue(5)
        grace.editingFinished.emit()

        saved = load_config(tmp_paths)
        assert saved.agents.chat.tool_loop_reminder_interval == 12
        assert saved.agents.chat.tool_loop_final_warning_count == 4
        assert saved.agents.chat.tool_loop_final_grace_loops == 5
        assert saved.agents.chat.max_loops == 25
    finally:
        close_settings_page(page, qapp)


def test_settings_provider_health_status_refreshes_without_manual_test(qapp, tmp_paths):
    from providers.health import ProviderHealth

    cfg = minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        status = page._provider_status_labels["ds"]
        assert status.text() == "尚未检测"

        runtime.provider_health["ds"] = ProviderHealth("checking", "检测中")
        page._refresh_provider_status_labels()
        assert status.text() == "启动自动检测中"

        runtime.provider_health["ds"] = ProviderHealth("ok", "可用", latency_ms=123)
        page._refresh_provider_status_labels()
        assert status.text() == "可用 · 123ms"
    finally:
        close_settings_page(page, qapp)


def test_settings_delete_provider_blocks_persona_management_agent_ref(qapp, tmp_paths, monkeypatch):
    cfg = minimal_root_config()
    cfg.providers["extra"] = ProviderConfig(preset="deepseek", api_key_id="extra_key")
    cfg.persona_management.persona_agent.provider = "extra"
    runtime = dashboard_runtime(tmp_paths, cfg)
    messages = []

    def fake_show_message(_parent, title, text, **kwargs):
        messages.append((title, text, kwargs))
        return True

    monkeypatch.setattr("ui.dashboard.settings.model.show_message", fake_show_message)
    page = SettingsPage(runtime)
    try:
        page._on_delete_provider("extra")

        assert "extra" in cfg.providers
        assert messages
        assert messages[0][0] == "无法删除"
        assert "人格分析" in messages[0][1]
    finally:
        close_settings_page(page, qapp)


def test_settings_page_collapses_advanced_budget_and_napcat_options(qapp, tmp_paths):
    from ui.dashboard.settings_page import CollapsibleSection

    cfg = minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        sections = page.findChildren(CollapsibleSection)
        titles = []
        collapsed = []
        for section in sections:
            labels = section.findChildren(QtWidgets.QLabel)
            buttons = section.findChildren(QtWidgets.QPushButton)
            if labels:
                titles.append(labels[0].text())
            if buttons:
                collapsed.append(section._body.isHidden())
                assert buttons[0].property("role") == "collapse-toggle"
                assert buttons[0].minimumWidth() == 30
                assert buttons[0].maximumWidth() == 30
                assert buttons[0].minimumHeight() == 30
                assert buttons[0].maximumHeight() == 30

        assert "NapCat 连接高级参数" in titles
        assert "上下文总预算" in titles
        assert "按工具结果预算" in titles
        assert any(collapsed)

        label_text = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "API 前等待连接" in label_text
        assert "托管进程预热" in label_text
    finally:
        close_settings_page(page, qapp)


def test_settings_page_scrolls_only_right_content(qapp, tmp_paths):
    cfg = minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        assert page._settings_scroll.widget() is page._settings_stack
        assert page._settings_scroll.parentWidget() is page
        assert page._settings_nav.parentWidget() is page
        assert page._status.parentWidget() is page
        assert page._settings_scroll.isAncestorOf(page._settings_stack)
        assert not page._settings_scroll.isAncestorOf(page._settings_nav)
        assert not page._settings_scroll.isAncestorOf(page._status)
    finally:
        close_settings_page(page, qapp)


def test_settings_page_short_sections_do_not_scroll_to_blank_space(qapp, tmp_paths):
    page = SettingsPage(dashboard_runtime(tmp_paths))
    try:
        page.resize(1014, 678)
        page.show()
        for _ in range(8):
            qapp.processEvents()

        labels = [page._settings_nav.item(i).text() for i in range(page._settings_nav.count())]

        page._settings_nav.setCurrentRow(labels.index("功能"))
        for _ in range(8):
            qapp.processEvents()
        assert page._settings_scroll.verticalScrollBar().maximum() > 0

        for section_name in ("记忆", "软件行为", "Token预算", "日志与诊断"):
            page._settings_nav.setCurrentRow(labels.index(section_name))
            for _ in range(8):
                qapp.processEvents()

            assert page._settings_scroll.verticalScrollBar().maximum() == 0
            assert page._settings_nav.verticalScrollBar().maximum() == 0
            assert page._status.parentWidget() is page
            assert not page._settings_scroll.isAncestorOf(page._status)
    finally:
        close_settings_page(page, qapp)


def test_settings_page_content_sync_reuses_single_timer(qapp, tmp_paths):
    page = SettingsPage(dashboard_runtime(tmp_paths))
    try:
        timers_before = page.findChildren(QtCore.QTimer)
        assert page._settings_content_sync_timer in timers_before

        for _ in range(100):
            page._schedule_settings_content_sync()

        timers_after = page.findChildren(QtCore.QTimer)
        assert timers_after == timers_before
        assert page._settings_content_sync_timer.isActive()
    finally:
        close_settings_page(page, qapp)


def test_settings_page_features_contains_emoji_without_extra_nav(qapp, tmp_paths):
    cfg = minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        labels = [page._settings_nav.item(i).text() for i in range(page._settings_nav.count())]
        features_row = labels.index("功能")
        page._settings_nav.setCurrentRow(features_row)
        text = "\n".join(
            label.text()
            for label in page._settings_stack.currentWidget().findChildren(QtWidgets.QLabel)
        )
        assert "表情包" in text
        assert "管理 Debata 在聊天中可用的表情包图片" in text
    finally:
        close_settings_page(page, qapp)


def test_settings_page_page_wrappers_do_not_add_trailing_stretch(qapp, tmp_paths):
    cfg = minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        for idx in range(page._settings_stack.count()):
            wrapper = page._settings_stack.widget(idx)
            layout = wrapper.layout()
            assert layout is not None
            assert layout.count() == 1
            assert layout.itemAt(0).widget() is not None
    finally:
        close_settings_page(page, qapp)


def test_settings_provider_test_model_can_use_vision_provider(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.providers["vision"] = ProviderConfig(preset="volcengine", api_key_id="vision_key")
    cfg.features.vision = VisionFeatureConfig(
        enabled=False,
        provider="vision",
        model="doubao-seed-1-6-vision-250815",
    )
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
        },
    )()
    page = SettingsPage(runtime)
    try:
        assert page._agent_model_for_provider("vision") == "doubao-seed-1-6-vision-250815"
    finally:
        close_settings_page(page, qapp)


@pytest.mark.asyncio
async def test_settings_adapter_test_uses_running_adapter_without_new_ws(
    qapp,
    tmp_paths,
    monkeypatch,
):
    cfg = minimal_root_config()
    running_adapter = SimpleNamespace(name="default", is_connected=True)
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "paths": tmp_paths,
            "secrets": type("Secrets", (), {"get": lambda self, _key: ""})(),
            "provider_registry": type("Registry", (), {"presets": {}})(),
            "providers": {},
            "provider_health": {},
            "adapter": running_adapter,
        },
    )()
    page = SettingsPage(runtime)
    try:
        async def fail_probe(*args, **kwargs):
            raise AssertionError("当前 Runtime 渠道已存在时不应探测端口")

        monkeypatch.setattr(SettingsPage, "_probe_tcp_port", fail_probe)
        page._on_test_adapter(cfg.adapters["default"])
        await asyncio.sleep(0)
        qapp.processEvents()

        assert page._adapter_test_status.text() == "✓ 当前 Runtime 渠道已连接"
    finally:
        close_settings_page(page, qapp)


def test_settings_longterm_memory_copy_describes_rag_as_enhancement(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.features = FeaturesConfig(long_term_memory=LongTermMemoryConfig(mode="file"))
    runtime = dashboard_runtime(tmp_paths, cfg)
    page = SettingsPage(runtime)
    calls = []
    page._save_now = lambda **kwargs: calls.append(kwargs)
    try:
        labels = [w.text() for w in page.findChildren(QtWidgets.QLabel)]
        buttons = [w.text() for w in page.findChildren(QtWidgets.QAbstractButton)]
        text = "\n".join(labels + buttons)

        assert "重要记忆始终启用" in text
        assert "RAG 历史召回增强" in text
        assert "文件模式（默认" not in text
        assert "RAG 向量检索" not in text

        rag_button = next(
            rb for rb in page.findChildren(QtWidgets.QRadioButton)
            if "启用 RAG 历史召回增强" in rb.text()
        )
        rag_button.setChecked(True)

        assert cfg.features.long_term_memory.mode == "rag"
        assert calls[-1]["change_desc"] == "long_term_memory.mode=rag"
    finally:
        close_settings_page(page, qapp)


def test_settings_embedding_dialog_enables_embedding(qapp, tmp_paths, monkeypatch):
    cfg = minimal_root_config()
    cfg.features = FeaturesConfig(
        embedding=EmbeddingFeatureConfig(
            enabled=False,
            type="api",
            provider="ds",
            api_model="old-model",
        ),
        long_term_memory=LongTermMemoryConfig(mode="rag"),
    )
    runtime = dashboard_runtime(tmp_paths, cfg)
    page = SettingsPage(runtime)

    class FakeEmbeddingDialog:
        def __init__(self, *_args, **_kwargs):
            self.result_data = {
                "type": "api",
                "provider": "ds",
                "model": "new-model",
                "api_key": "",
            }

        def exec(self):
            return True

    monkeypatch.setattr(
        "ui.dashboard.settings_page._EmbeddingEditDialog",
        FakeEmbeddingDialog,
    )
    monkeypatch.setattr(page, "_save_now", lambda **_kwargs: None)
    label = QLabel()
    try:
        page._open_embedding_dialog(label)

        assert cfg.features.embedding.enabled is True
        assert cfg.features.embedding.provider == "ds"
        assert cfg.features.embedding.api_model == "new-model"
    finally:
        close_settings_page(page, qapp)
