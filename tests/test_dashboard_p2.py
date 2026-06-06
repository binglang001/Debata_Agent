"""P2/体验修复的轻量回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
QApplication = QtWidgets.QApplication
QLabel = QtWidgets.QLabel
QWidget = QtWidgets.QWidget
Qt = QtCore.Qt

from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    EmbeddingFeatureConfig,
    FeaturesConfig,
    LongTermMemoryConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
    VisionFeatureConfig,
)
from memory.important import ImportantMemoryManager
from memory.rag_store import RagEntry
from ui.dashboard.chats_page import (
    _conversation_list_signature,
    _format_tool_call_for_display,
    _group_records_by_conversation,
    _scrollbar_near_bottom,
)
from ui.dashboard.layout import DEFAULT_LAYOUT
from ui.dashboard.logs_page import _format_record
from ui.dashboard.main_window import DashboardWindow
from ui.dashboard.memory_page import MemoryPage
from ui.dashboard.overview_page import OverviewPage
from ui.dashboard.personas_page import PersonasPage, _PersonaCreatorDialog
from ui.dashboard.settings_page import SettingsPage
from ui.widgets.model_combo import ModelComboBox
from ui.widgets.window_chrome import _resize_edges_for_local_pos
from ui.wizard.components import ApiKeyInput
from ui.wizard.context import WizardContext
from ui.wizard.persona_creator import PersonaCreatorStepView
from ui.wizard.step_views.features import _TTSFeatureCard
from ui.wizard.step_views.welcome import WelcomeStepView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _minimal_root_config() -> RootConfig:
    return RootConfig(
        providers={"ds": ProviderConfig(preset="deepseek", api_key_id="ds_key")},
        adapters={"default": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=AgentConfig(provider="ds", model="deepseek-chat")),
    )


class _EmptyHistory:
    async def records(self):
        return []


class _EmptyImportant:
    def items(self):
        return []


def _dashboard_runtime(tmp_paths, cfg: RootConfig | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        adapter=None,
        config=cfg or _minimal_root_config(),
        embedding_service=None,
        history=_EmptyHistory(),
        important=_EmptyImportant(),
        model_activity={},
        paths=tmp_paths,
        persona=SimpleNamespace(name="Debata"),
        provider_health={},
        provider_registry=SimpleNamespace(presets={}),
        providers={},
        rag_store=None,
        secrets=SimpleNamespace(get=lambda _key: ""),
        usage_stats=None,
    )


def test_chats_group_records_by_metadata_and_legacy_header():
    records = [
        {
            "role": "user",
            "content": "hello",
            "metadata": {
                "messages": [
                    {
                        "scope": "private",
                        "target_id": "10001",
                        "user_id": "10001",
                        "nickname": "Alice",
                    }
                ]
            },
        },
        {"role": "assistant", "content": "hi"},
        {
            "role": "user",
            "content": "【2026-05-27 12:00:00 群聊 20002 Bob(30003) msg_id=9】群消息",
        },
    ]

    grouped = _group_records_by_conversation(records)

    assert grouped[0]["key"] == "group:20002"
    assert grouped[0]["label"] == "群聊 20002"
    assert grouped[1]["key"] == "private:10001"
    assert len(grouped[1]["records"]) == 2


def test_chats_group_records_prefers_explicit_conversation_id():
    records = [
        {"role": "user", "content": "u", "conversation_id": "private:10001"},
        {"role": "assistant", "content": "a", "conversation_id": "private:10001"},
        {"role": "system", "content": "主动思考：本次跳过"},
        {
            "role": "tool",
            "content": "{}",
            "tool_call_id": "tc",
            "conversation_id": "group:20002",
        },
    ]

    grouped = _group_records_by_conversation(records)
    by_key = {item["key"]: item for item in grouped}

    assert by_key["private:10001"]["label"] == "私聊 10001"
    assert len(by_key["private:10001"]["records"]) == 2
    assert by_key["system:global"]["records"][0]["content"] == "主动思考：本次跳过"
    assert by_key["group:20002"]["records"][0]["tool_call_id"] == "tc"


def test_chats_groups_proactive_records_as_system():
    records = [
        {"role": "user", "content": "群消息", "conversation_id": "group:20002"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            "conversation_id": "system:proactive",
        },
        {
            "role": "tool",
            "content": '{"ok":true}',
            "tool_call_id": "tc-proactive",
            "conversation_id": "system:proactive",
        },
    ]

    grouped = _group_records_by_conversation(records)
    by_key = {item["key"]: item for item in grouped}

    assert "system:proactive" in by_key
    assert by_key["system:proactive"]["label"] == "系统记录 · 主动思考"
    assert by_key["system:proactive"]["records"][1]["tool_call_id"] == "tc-proactive"


def test_chats_unknown_assistant_without_context_does_not_attach_to_previous_chat():
    records = [
        {"role": "system", "content": "全局事件"},
        {"role": "assistant", "content": "后台旧记录"},
        {"role": "user", "content": "hi", "conversation_id": "private:10001"},
    ]

    grouped = _group_records_by_conversation(records)
    by_key = {item["key"]: item for item in grouped}

    assert by_key["unknown:history"]["records"][0]["content"] == "后台旧记录"
    assert by_key["private:10001"]["records"][0]["content"] == "hi"


def test_chats_conversation_signature_tracks_visible_list_changes():
    conversations = [
        {"key": "group:1", "records": [{"content": "a"}], "preview": "a"},
        {"key": "system:global", "records": [{"content": "s"}], "preview": "s"},
    ]

    assert _conversation_list_signature(conversations) == [
        ("group:1", 1, "a"),
        ("system:global", 1, "s"),
    ]


def test_chats_formats_send_tool_call_readably():
    text = _format_tool_call_for_display(
        {
            "function": {
                "name": "send_group_message",
                "arguments": (
                    '{"group_id":1039163467,"targets":['
                    '{"content":"好好好 不说了","delay":0.5},'
                    '{"content":"那我先待机","delay":0.6}]}'
                ),
            }
        }
    )

    assert text == "在群 1039163467 发送消息：好好好 不说了（0.5s）；那我先待机（0.6s）"


def test_chats_scrollbar_bottom_threshold():
    class FakeBar:
        def __init__(self, value: int, maximum: int) -> None:
            self._value = value
            self._maximum = maximum

        def value(self) -> int:
            return self._value

        def maximum(self) -> int:
            return self._maximum

    assert _scrollbar_near_bottom(FakeBar(980, 1000), threshold=24) is True
    assert _scrollbar_near_bottom(FakeBar(900, 1000), threshold=24) is False


@pytest.mark.asyncio
async def test_important_memory_delete_by_id_is_exact(tmp_path):
    mgr = ImportantMemoryManager(tmp_path / "important.json")
    await mgr.load()
    await mgr.replace_all(
        [
            {"timestamp": "t1", "content": "喜欢红茶"},
            {"timestamp": "t2", "content": "也喜欢红茶蛋糕"},
        ]
    )

    deleted = await mgr.delete_by_id("t1")

    assert deleted is True
    assert [item["timestamp"] for item in mgr.items()] == ["t2"]


def test_log_detail_format_includes_exception():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("tests.demo").makeRecord(
            "tests.demo",
            logging.ERROR,
            __file__,
            1,
            "failed: %s",
            ("x",),
            exc_info=sys.exc_info(),
        )

    text = _format_record(record, single_line=False)

    assert "模块：tests.demo" in text
    assert "RuntimeError: boom" in text


def test_welcome_requires_explicit_path_choice(qapp):
    view = WelcomeStepView(WizardContext())
    errors: list[str] = []
    view.invalid_input.connect(errors.append)

    assert view.save() is False
    assert errors == ["请先选择推荐路径或自定义路径。"]


def test_tts_feature_card_keeps_configured_model_dir(qapp):
    card = _TTSFeatureCard()
    card._model_dir_edit.setText("F:/models/custom-voxcpm2")

    state = card.state()

    assert state["model_dir"] == "F:/models/custom-voxcpm2"


def test_tts_feature_card_local_reference_audio_is_optional(qapp, monkeypatch):
    import ui.wizard.step_views.features as features_module

    card = _TTSFeatureCard()
    card._check.setChecked(True)
    card._type_combo.setCurrentIndex(card._type_combo.findData("local"))
    card._ref_audio_edit.clear()
    card._prompt_edit.setText("年轻女性，温柔语气")
    monkeypatch.setattr(features_module, "_directory_has_files", lambda _path: True)

    assert card.ensure_ready(card) is True
    assert card.state()["reference_audio"] == ""


def test_model_combo_focus_does_not_reopen_popup(qapp, monkeypatch):
    combo = ModelComboBox()
    try:
        combo.add_model("deepseek-chat")
        calls = []
        monkeypatch.setattr(combo, "showPopup", lambda: calls.append("popup"))

        combo.setFocus(Qt.FocusReason.MouseFocusReason)
        qapp.processEvents()

        assert calls == []
    finally:
        combo.deleteLater()


def test_persona_creator_admin_row_buttons_are_visible_and_spaced(qapp):
    view = PersonaCreatorStepView(WizardContext())
    try:
        assert len(view._admin_rows) == 1
        first = view._admin_rows[0]
        assert first.remove_btn.isHidden()

        view._add_admin_row()
        second = view._admin_rows[1]

        assert first.remove_btn.isHidden()
        assert second.remove_btn.text() == "删除"
        assert not second.remove_btn.isHidden()
        assert second.remove_btn.width() >= 48
        assert view._admins_layout.spacing() >= 8
    finally:
        view.deleteLater()


def test_api_key_input_progress_slot_keeps_layout_height(qapp):
    widget = ApiKeyInput(allow_empty_test=True)
    try:
        widget.ensurePolished()
        widget.adjustSize()
        idle_hint = widget.sizeHint().height()

        widget.set_test_state("testing")
        widget.adjustSize()
        testing_hint = widget.sizeHint().height()

        widget.set_test_state("success", "ok")
        widget.adjustSize()
        success_hint = widget.sizeHint().height()
    finally:
        widget.deleteLater()

    assert testing_hint == idle_hint
    assert success_hint == idle_hint


def test_settings_restore_opened_config_writes_snapshot(tmp_paths):
    cfg = _minimal_root_config()
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
    cfg = _minimal_root_config()

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
        page.deleteLater()

    assert "软件行为" in labels
    assert "Token预算" in labels
    assert "日志与诊断" in labels
    assert "表情包" not in labels
    assert "角色" not in labels
    assert "外观" not in labels


def test_settings_provider_health_status_refreshes_without_manual_test(qapp, tmp_paths):
    from providers.health import ProviderHealth

    cfg = _minimal_root_config()
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
        page.deleteLater()


def test_settings_page_collapses_advanced_budget_and_napcat_options(qapp, tmp_paths):
    from ui.dashboard.settings_page import CollapsibleSection

    cfg = _minimal_root_config()
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
        page.deleteLater()


def test_settings_page_scrolls_only_right_content(qapp, tmp_paths):
    cfg = _minimal_root_config()
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
        page.deleteLater()


def test_settings_page_short_sections_do_not_scroll_to_blank_space(qapp, tmp_paths):
    page = SettingsPage(_dashboard_runtime(tmp_paths))
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
        page.close()
        page.deleteLater()


def test_settings_page_content_sync_reuses_single_timer(qapp, tmp_paths):
    page = SettingsPage(_dashboard_runtime(tmp_paths))
    try:
        timers_before = page.findChildren(QtCore.QTimer)
        assert page._settings_content_sync_timer in timers_before

        for _ in range(100):
            page._schedule_settings_content_sync()

        timers_after = page.findChildren(QtCore.QTimer)
        assert timers_after == timers_before
        assert page._settings_content_sync_timer.isActive()
    finally:
        page.close()
        page.deleteLater()


def test_dashboard_settings_page_does_not_use_outer_scroll(qapp, tmp_paths):
    window = DashboardWindow(_dashboard_runtime(tmp_paths))
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
        window.close()
        window.deleteLater()


def test_settings_page_features_contains_emoji_without_extra_nav(qapp, tmp_paths):
    cfg = _minimal_root_config()
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
        text = "\n".join(label.text() for label in page._settings_stack.currentWidget().findChildren(QtWidgets.QLabel))
        assert "表情包" in text
        assert "管理 Debata 在聊天中可用的表情包图片" in text
    finally:
        page.deleteLater()


def test_settings_page_page_wrappers_do_not_add_trailing_stretch(qapp, tmp_paths):
    cfg = _minimal_root_config()
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
        page.deleteLater()


def test_settings_provider_test_model_can_use_vision_provider(qapp, tmp_paths):
    cfg = _minimal_root_config()
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
        page.deleteLater()


@pytest.mark.asyncio
async def test_settings_adapter_test_uses_running_adapter_without_new_ws(
    qapp,
    tmp_paths,
    monkeypatch,
):
    cfg = _minimal_root_config()
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
        page.deleteLater()


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


def test_overview_page_shows_usage_activity_and_provider_counts(qapp):
    class FakeUsageStore:
        def summarize(self, range_name):
            assert range_name == "today"
            return SimpleNamespace(
                request_count=3,
                prompt_tokens=1000,
                completion_tokens=200,
                reasoning_tokens=50,
                cached_tokens=800,
                cache_creation_tokens=120,
                total_tokens=1250,
                cache_hit_rate=0.8,
            )

    cfg = _minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "adapter": None,
            "config": cfg,
            "providers": {"ok": object(), "bad": object()},
            "provider_health": {
                "ok": SimpleNamespace(status="ok", latency_ms=123, message="可用"),
                "bad": SimpleNamespace(status="error", message="请求超时"),
            },
            "_hist_len": 0,
            "important": None,
            "usage_stats": FakeUsageStore(),
            "model_activity": {
                "state": "tool",
                "text": "调用工具：get_weather",
                "model": "deepseek-chat",
                "agent": "主模型",
                "tool_names": ["get_weather"],
            },
            "persona": type("Persona", (), {"name": "Mika"})(),
        },
    )()
    page = OverviewPage(runtime)
    try:
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))

        assert page._providers_card._right_label.text() == "1/2 可用"
        assert page._providers_card._right_label.maximumWidth() == 112
        assert page._providers_card._right_label.maximumHeight() == 22
        assert page._providers_card._right_label.toolTip() == "部分可用 1/2 · 请求超时"
        assert "部分可用 1/2 · 请求超时" not in labels
        assert "ok" in labels
        assert "可用 · 123ms" in labels
        assert "bad" in labels
        assert "异常 · 请求超时" in labels
        assert "渠道状态" in labels
        assert "用量统计" in labels
        assert "请求数" in labels
        assert "3" in labels
        assert "输入 token" in labels
        assert "输出 token" in labels
        assert "总 token" in labels
        assert "1,250" in labels
        assert "KV 命中 token" in labels
        assert "800" in labels
        assert "KV 写入 token" in labels
        assert "120" in labels
        assert "80.0%" in labels
        assert "主模型状态" in labels
        assert "调用工具" in labels
        assert "get_weather" in labels
        assert "累计概况" not in labels
        assert "当前角色" not in labels
        assert page._usage_card._right_label.isHidden()
    finally:
        page.deleteLater()


def test_overview_cards_keep_content_driven_height(qapp):
    page = OverviewPage(
        type(
            "RuntimeStub",
            (),
            {
                "adapter": None,
                "config": _minimal_root_config(),
                "providers": {},
                "provider_health": {},
                "_hist_len": 0,
                "important": None,
                "usage_stats": None,
                "model_activity": {},
            },
        )()
    )
    try:
        cards = [
            page._activity_card,
            page._providers_card,
            page._adapter_card,
            page._usage_card,
        ]
        for card in cards:
            assert card.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Policy.Preferred
    finally:
        page.deleteLater()


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


def test_memory_page_rag_mode_shows_index_view(qapp):
    class FakeImportant:
        def items(self):
            return [{"timestamp": "t1", "content": "用户喜欢红茶"}]

    class FakeRagStore:
        def all_entries(self):
            return [
                RagEntry(
                    id="t1",
                    text="用户喜欢红茶",
                    vector=[0.1, 0.2],
                    meta={"timestamp": "t1"},
                )
            ]

    cfg = _minimal_root_config()
    cfg.features = FeaturesConfig(
        long_term_memory=LongTermMemoryConfig(mode="rag")
    )
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "important": FakeImportant(),
            "rag_store": FakeRagStore(),
            "embedding_service": object(),
        },
    )()
    page = MemoryPage(runtime)
    try:
        page.refresh()

        assert page._title.text() == "RAG 历史向量索引"
        assert "索引 1 条" in page._rag_status.text()
        assert page._list.count() == 1
        assert page._add_row_widget.isHidden()
        assert page._action_row_widget.isHidden()
        assert page._metadata_row_widget.isHidden()
    finally:
        page.deleteLater()


def test_settings_embedding_dialog_enables_embedding(qapp, tmp_paths, monkeypatch):
    cfg = _minimal_root_config()
    cfg.features = FeaturesConfig(
        embedding=EmbeddingFeatureConfig(
            enabled=False,
            type="api",
            provider="ds",
            api_model="old-model",
        ),
        long_term_memory=LongTermMemoryConfig(mode="rag"),
    )
    runtime = _dashboard_runtime(tmp_paths, cfg)
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
        page.deleteLater()


def test_personas_page_can_build_and_save_generated_persona(qapp, tmp_path):
    from agents.persona_gen_agent import PersonaBrief

    cfg = _minimal_root_config()
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    debata_dir = personas_dir / "debata"
    debata_dir.mkdir()
    (debata_dir / "persona_prompt.py").write_text("PERSONA_PROMPT = 'x'\n", encoding="utf-8")

    class FakeSecrets:
        def get(self, key_id: str) -> str | None:
            return "sk-test" if key_id == "ds_key" else None

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "secrets": FakeSecrets(),
            "paths": type("Paths", (), {"PERSONAS_DIR": personas_dir})(),
        },
    )()
    page = PersonasPage(runtime)
    try:
        assert page._create_btn.text() == "新建角色"
        context = page._build_creator_context()
        assert context is not None
        assert context.main.api_key == "sk-test"
        assert context.main.model == "deepseek-chat"

        context.persona.source = "create"
        context.persona.active = "Mika"
        context.persona.generated_xml = "<identity>你是 Mika</identity>"
        context.persona.brief = PersonaBrief(
            name="Mika",
            gender="female",
            admins=[
                {"name": "Lily", "qq": "123456", "relation": "创作者"},
                {"name": "Robin", "qq": "654321", "relation": "朋友"},
            ],
        )
        context.admin_name = "Lily"
        context.admin_qq = "123456"

        assert page._save_generated_persona(context) == "Mika"
        saved = personas_dir / "Mika" / "persona_prompt.py"
        assert saved.exists()
        text = saved.read_text(encoding="utf-8")
        assert "<identity>你是 Mika</identity>" in text
        assert "'qq': 123456" in text
        assert "'qq': 654321" in text
        assert "'gender': 'female'" in text
    finally:
        page.deleteLater()


def test_persona_creator_dialog_wires_runtime_usage_callbacks(qapp):
    context = WizardContext()

    class RuntimeStub:
        async def _record_model_usage(self, usage, metadata):
            return None

        def _update_model_activity(self, payload):
            return None

    runtime = RuntimeStub()
    dlg = _PersonaCreatorDialog(context, runtime=runtime)
    try:
        assert dlg._creator.usage_recorder == runtime._record_model_usage
        assert dlg._creator.status_callback == runtime._update_model_activity
    finally:
        dlg.deleteLater()


def test_personas_page_selects_active_persona_on_refresh(qapp, tmp_path):
    cfg = _minimal_root_config()
    cfg.persona.active = "Mika"
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    for name in ("debata", "Mika"):
        d = personas_dir / name
        d.mkdir()
        (d / "persona_prompt.py").write_text("PERSONA_PROMPT = 'x'\n", encoding="utf-8")

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "secrets": None,
            "paths": type("Paths", (), {"PERSONAS_DIR": personas_dir})(),
        },
    )()
    page = PersonasPage(runtime)
    try:
        page.refresh()
        assert page._selected() == "Mika"
    finally:
        page.deleteLater()


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
