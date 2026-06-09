"""P0 UI 配置保存路径回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QApplication = QtWidgets.QApplication
QDialog = QtWidgets.QDialog

from app_config import SecretsManager
from app_config.loader import load_config
from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    ASRFeatureConfig,
    EmbeddingFeatureConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
    TTSFeatureConfig,
)
from ui.dashboard.layout import DEFAULT_LAYOUT
from ui.dashboard.settings_page import (
    SettingsPage,
    _AddProviderDialog,
    _ASREditDialog,
    _EmbeddingEditDialog,
    _load_provider_presets_for_dialog,
    _TTSEditDialog,
)
from ui.wizard.flow import next_step
from ui.wizard.step_views.embedding import EmbeddingStepView
from ui.wizard.step_views.summary import SummaryStepView
from ui.wizard.steps import STEPS, StepId
from ui.wizard.window import WizardWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def test_unit_spinbox_uses_external_unit_label(qapp):
    from ui.widgets.unit_fields import unit_spinbox

    spin = QtWidgets.QSpinBox()
    row = unit_spinbox(spin, "token")
    try:
        assert spin.suffix() == ""
        assert [label.text() for label in row.findChildren(QtWidgets.QLabel)] == ["token"]
    finally:
        row.deleteLater()


def test_settings_feature_dialogs_accept_on_ok(qapp):
    asr = ASRFeatureConfig(
        enabled=True,
        type="api",
        provider="xfyun",
        extra_credentials={"app_id": "app", "api_secret": "secret"},
    )
    asr_dlg = _ASREditDialog(asr)
    asr_dlg._type_combo.setCurrentIndex(asr_dlg._type_combo.findData("api"))
    asr_dlg._on_ok()
    assert asr_dlg.result() == QDialog.DialogCode.Accepted
    assert asr_dlg.result_data["provider"] == "xfyun"

    tts = TTSFeatureConfig(
        enabled=True,
        type="api",
        provider="xfyun",
        api_key_id="tts_xfyun",
        extra_credentials={"app_id": "app", "api_secret": "secret", "voice": "x4_xiaoyan"},
    )
    tts_dlg = _TTSEditDialog(tts)
    tts_dlg._type_combo.setCurrentIndex(tts_dlg._type_combo.findData("api"))
    tts_dlg._on_ok()
    assert tts_dlg.result() == QDialog.DialogCode.Accepted
    assert tts_dlg.result_data["provider"] == "xfyun"

    edge_tts = TTSFeatureConfig(enabled=True, type="api", provider="edge")
    edge_dlg = _TTSEditDialog(edge_tts)
    edge_dlg._type_combo.setCurrentIndex(edge_dlg._type_combo.findData("api"))
    edge_dlg._prov.setCurrentIndex(edge_dlg._prov.findData("edge"))
    edge_dlg._api_voice.setText("zh-CN-XiaoyiNeural")
    edge_dlg._on_ok()
    assert edge_dlg.result() == QDialog.DialogCode.Accepted
    assert edge_dlg.result_data["provider"] == "edge"
    assert edge_dlg.result_data["extra_credentials"] == {"voice": "zh-CN-XiaoyiNeural"}

    emb = EmbeddingFeatureConfig(
        enabled=True,
        type="api",
        provider="deepseek_main",
        api_model="text-embedding-v1",
    )
    emb_dlg = _EmbeddingEditDialog(["deepseek_main"], emb)
    emb_dlg._on_ok()
    assert emb_dlg.result() == QDialog.DialogCode.Accepted
    assert emb_dlg.result_data["provider"] == "deepseek_main"


def test_add_provider_dialog_uses_preset_loader(qapp):
    from pathlib import Path

    presets_dir = Path(__file__).resolve().parent.parent / "providers" / "presets"
    presets = _load_provider_presets_for_dialog(presets_dir)
    preset_ids = {p[0] for p in presets}

    assert {"groq", "together", "xai"} <= preset_ids

    dlg = _AddProviderDialog(set(), presets)
    combo_ids = {dlg._preset_combo.itemData(i) for i in range(dlg._preset_combo.count())}
    assert {"groq", "together", "xai", "custom"} <= combo_ids


def test_wizard_persist_keeps_asr_tts_api_config(qapp, tmp_paths, fake_keyring):
    secrets = SecretsManager(tmp_paths)
    secrets.initialize()
    win = WizardWindow(tmp_paths, secrets)
    try:
        ctx = win._context
        ctx.main.api_key = "main-key"
        ctx.asr.enabled = True
        ctx.asr.extra = {
            "enabled": True,
            "type": "api",
            "provider": "xfyun",
            "api_key": "asr-key",
            "extra_credentials": {
                "app_id": "asr-app",
                "api_secret": "asr-secret",
            },
        }
        ctx.tts.enabled = True
        ctx.tts.extra = {
            "enabled": True,
            "type": "api",
            "provider": "xfyun",
            "api_key": "tts-key",
            "extra_credentials": {
                "app_id": "tts-app",
                "api_secret": "tts-secret",
                "voice": "x4_xiaoyan",
            },
        }

        win._persist()

        cfg = load_config(tmp_paths, set_global=False)
        assert cfg.features.asr.type == "api"
        assert cfg.features.asr.provider == "xfyun"
        assert cfg.features.asr.api_key_id == "asr_xfyun"
        assert cfg.features.asr.extra_credentials == {
            "app_id": "asr-app",
            "api_secret": "asr-secret",
        }
        assert secrets.get("asr_xfyun") == "asr-key"

        assert cfg.features.tts.type == "api"
        assert cfg.features.tts.provider == "xfyun"
        assert cfg.features.tts.api_key_id == "tts_xfyun"
        assert cfg.features.tts.extra_credentials == {
            "app_id": "tts-app",
            "api_secret": "tts-secret",
            "voice": "x4_xiaoyan",
        }
        assert secrets.get("tts_xfyun") == "tts-key"
    finally:
        win._completed_emitted = True
        win.close()
        win.deleteLater()


def test_wizard_persist_edge_tts_needs_no_secret(qapp, tmp_paths, fake_keyring):
    secrets = SecretsManager(tmp_paths)
    secrets.initialize()
    win = WizardWindow(tmp_paths, secrets)
    try:
        ctx = win._context
        ctx.main.api_key = "main-key"
        ctx.tts.enabled = True
        ctx.tts.extra = {
            "enabled": True,
            "type": "api",
            "provider": "edge",
            "api_key": "",
            "extra_credentials": {},
        }

        win._persist()

        cfg = load_config(tmp_paths, set_global=False)
        assert cfg.features.tts.type == "api"
        assert cfg.features.tts.provider == "edge"
        assert cfg.features.tts.api_key_id is None
    finally:
        win._completed_emitted = True
        win.close()
        win.deleteLater()


def test_wizard_content_width_uses_viewport_not_layout_stretch(qapp):
    win = SimpleNamespace()
    win._wizard_scroll = SimpleNamespace(
        viewport=lambda: SimpleNamespace(width=lambda: 900),
    )
    win._page_host = SimpleNamespace(
        _min=0,
        _max=0,
        minimumWidth=lambda: win._page_host._min,
        maximumWidth=lambda: win._page_host._max,
        setMinimumWidth=lambda value: setattr(win._page_host, "_min", value),
        setMaximumWidth=lambda value: setattr(win._page_host, "_max", value),
        updateGeometry=lambda: None,
    )

    WizardWindow._sync_page_width(win)

    assert win._page_host._min == 900
    assert win._page_host._max == 900

    win._wizard_scroll = SimpleNamespace(
        viewport=lambda: SimpleNamespace(width=lambda: DEFAULT_LAYOUT.page_max_width + 500),
    )
    WizardWindow._sync_page_width(win)

    assert win._page_host._min == DEFAULT_LAYOUT.page_max_width
    assert win._page_host._max == DEFAULT_LAYOUT.page_max_width


def test_wizard_embedding_step_is_fixed_in_flow():
    assert next_step(
        StepId.FEATURES,
        "recommended",
        {"long_term_memory_mode": "file", "persona_source": "builtin"},
    ) == StepId.EMBEDDING


def test_wizard_embedding_step_metadata_uses_rag_enhancement_copy():
    text = STEPS[StepId.EMBEDDING].subtitle

    assert "重要记忆始终启用" in text
    assert "RAG 历史向量召回增强" in text
    assert "普通文件模式更轻" not in text
    assert "RAG 模式会用 embedding" not in text


def test_wizard_summary_uses_rag_enhancement_copy(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    ctx.long_term_memory_mode = "rag"
    view = SummaryStepView(ctx)
    try:
        view.refresh()
        text = "\n".join(label.text() for label in view.findChildren(QtWidgets.QLabel))

        assert "重要记忆始终启用；已启用 RAG 历史召回增强" in text
        assert "文件模式" not in text
        assert "向量模式" not in text
    finally:
        view.deleteLater()


def test_wizard_embedding_local_requires_ready_model(qapp, monkeypatch):
    from ui.wizard.context import WizardContext

    view = EmbeddingStepView(WizardContext())
    try:
        view._rb_rag.setChecked(True)
        idx = view._type_combo.findData("local")
        view._type_combo.setCurrentIndex(idx)
        view._local_dir.setText("data/models/embedding/missing")
        monkeypatch.setattr(
            "ui.wizard.step_views.embedding._directory_has_files",
            lambda _path: False,
        )
        monkeypatch.setattr(
            "ui.wizard.step_views.embedding._prompt_download_model",
            lambda *args, **kwargs: None,
        )

        assert view.save() is False
    finally:
        view.deleteLater()


def test_wizard_embedding_copy_keeps_important_memory_enabled(qapp):
    from ui.wizard.context import WizardContext

    view = EmbeddingStepView(WizardContext())
    try:
        labels = [w.text() for w in view.findChildren(QtWidgets.QLabel)]
        buttons = [w.text() for w in view.findChildren(QtWidgets.QAbstractButton)]
        text = "\n".join(labels + buttons)

        assert "重要记忆始终启用" in text
        assert "RAG 是可选的历史向量召回增强" in text
        assert "文件模式更轻" not in text
        assert "文件模式（默认" not in text
        assert "RAG 模式会用 embedding" not in text
    finally:
        view.deleteLater()


def test_wizard_embedding_api_can_use_independent_provider(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    view = EmbeddingStepView(ctx)
    try:
        view._rb_rag.setChecked(True)
        view._type_combo.setCurrentIndex(view._type_combo.findData("api"))
        idx = view._api_provider.findData("new:custom")
        assert idx >= 0
        view._api_provider.setCurrentIndex(idx)
        view._api_base_url.setText("https://embed.example.com/v1")
        view._api_model.setText("embed-model")
        view._api_key.set_text("embed-key")

        assert view.save() is True
        assert ctx.embedding_type == "api"
        assert ctx.embedding_provider == "embedding_custom"
        assert ctx.embedding_provider_preset == "custom"
        assert ctx.embedding_provider_base_url == "https://embed.example.com/v1"
        assert ctx.embedding_api_key == "embed-key"
    finally:
        view.deleteLater()


def test_wizard_embedding_default_does_not_reuse_deepseek_main(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    ctx.main.preset = "deepseek"
    ctx.main.model = "deepseek-v4-flash"
    ctx.embedding_provider = ""
    view = EmbeddingStepView(ctx)
    try:
        view._rb_rag.setChecked(True)
        view._refresh_provider_choices()

        assert view._api_provider.currentData() == "new:volcengine"
        assert view._api_model.text() == "doubao-embedding-vision-251215"
        assert view._api_provider.findData("existing:deepseek_main") < 0
    finally:
        view.deleteLater()


def test_wizard_embedding_endpoint_recovers_independent_provider_preset(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    ctx.long_term_memory_mode = "rag"
    ctx.embedding_type = "api"
    ctx.embedding_provider = "embedding_volcengine"
    ctx.embedding_model = "doubao-embedding-vision-251215"
    ctx.embedding_api_key = "embed-key"

    view = EmbeddingStepView(ctx)
    try:
        base_url, api_key = view._embedding_endpoint("embedding_volcengine")
    finally:
        view.deleteLater()

    assert base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert api_key == "embed-key"


def test_wizard_embedding_volcengine_uses_current_model(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    view = EmbeddingStepView(ctx)
    try:
        view._rb_rag.setChecked(True)
        view._type_combo.setCurrentIndex(view._type_combo.findData("api"))
        view._api_model.clear()
        idx = view._api_provider.findData("new:volcengine")
        assert idx >= 0
        view._api_provider.setCurrentIndex(idx)
        view._on_provider_changed()

        assert view._api_model.text() == "doubao-embedding-vision-251215"
    finally:
        view.deleteLater()


def test_wizard_embedding_refresh_fills_missing_api_model_default(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    ctx.long_term_memory_mode = "rag"
    ctx.embedding_type = "api"
    ctx.embedding_provider = ""
    ctx.embedding_model = ""
    view = EmbeddingStepView(ctx)
    try:
        view.refresh()

        assert view._api_provider.currentData() == "new:volcengine"
        assert view._api_model.text() == "doubao-embedding-vision-251215"
    finally:
        view.deleteLater()


def test_wizard_embedding_refresh_preserves_custom_api_model(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    ctx.long_term_memory_mode = "rag"
    ctx.embedding_type = "api"
    ctx.embedding_provider = "embedding_volcengine"
    ctx.embedding_provider_preset = "volcengine"
    ctx.embedding_model = "custom-embedding-model"
    view = EmbeddingStepView(ctx)
    try:
        view.refresh()

        assert view._api_model.text() == "custom-embedding-model"
    finally:
        view.deleteLater()


def _minimal_root_config() -> RootConfig:
    return RootConfig(
        providers={
            "ds": ProviderConfig(
                preset="deepseek",
                display_name="DeepSeek",
                api_key_id="ds_key",
            ),
        },
        adapters={"default": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=AgentConfig(provider="ds", model="deepseek-chat")),
    )


def test_settings_change_count_counts_leaf_fields_only():
    cfg = _minimal_root_config()

    class Dummy:
        def __init__(self):
            self.config = cfg
            self._baseline = cfg.model_copy(deep=True)

        def _cfg(self):
            return self.config

        def _count_changes(self) -> int:
            return SettingsPage._count_changes(self)

    page = Dummy()
    cfg.providers["ds"].display_name = "DeepSeek 主号"

    assert SettingsPage._count_changes(page) == 1


def test_settings_save_updates_baseline(tmp_paths):
    cfg = _minimal_root_config()

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
            self.config = cfg
            self._baseline = cfg.model_copy(deep=True)
            self._runtime = type("RuntimeStub", (), {"paths": tmp_paths})()
            self._status = FakeStatus()

        def _cfg(self):
            return self.config

        def _count_changes(self) -> int:
            return SettingsPage._count_changes(self)

    page = Dummy()
    cfg.providers["ds"].display_name = "DeepSeek 主号"
    assert SettingsPage._count_changes(page) == 1

    SettingsPage._save_now(page, needs_restart=True, change_desc="test")

    assert page._status.calls[-1] == (0, True)
    assert SettingsPage._count_changes(page) == 0
    assert page._status.error == ""


def test_settings_behavior_save_uses_current_runtime_config_after_restart(tmp_paths):
    old_cfg = _minimal_root_config()
    new_cfg = old_cfg.model_copy(deep=True)
    old_cfg.behavior.proactive_think_interval_seconds = 600.0
    new_cfg.behavior.proactive_think_interval_seconds = 600.0

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
            self._suppress_signals = False
            self._HOT_FIELDS = SettingsPage._HOT_FIELDS
            self._runtime = type("RuntimeStub", (), {"paths": tmp_paths, "config": old_cfg})()
            self._baseline = old_cfg.model_copy(deep=True)
            self._status = FakeStatus()

        def _cfg(self):
            return self._runtime.config

        def _count_changes(self) -> int:
            return SettingsPage._count_changes(self)

        def _save_now(self, *, needs_restart: bool, change_desc: str = "") -> None:
            SettingsPage._save_now(self, needs_restart=needs_restart, change_desc=change_desc)

    page = Dummy()
    page._runtime.config = new_cfg
    page._baseline = new_cfg.model_copy(deep=True)

    SettingsPage._on_behavior_field(
        page,
        "proactive_think_interval_seconds",
        60.0,
    )

    saved = load_config(tmp_paths)
    assert old_cfg.behavior.proactive_think_interval_seconds == 600.0
    assert new_cfg.behavior.proactive_think_interval_seconds == 60.0
    assert saved.behavior.proactive_think_interval_seconds == 60.0
    assert page._status.calls[-1] == (0, True)


def test_settings_tool_result_budget_save_requires_restart(tmp_paths):
    cfg = _minimal_root_config()

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
            self._suppress_signals = False
            self._runtime = type("RuntimeStub", (), {"paths": tmp_paths, "config": cfg})()
            self._baseline = cfg.model_copy(deep=True)
            self._status = FakeStatus()

        def _cfg(self):
            return self._runtime.config

        def _count_changes(self) -> int:
            return SettingsPage._count_changes(self)

        def _save_now(self, *, needs_restart: bool, change_desc: str = "") -> None:
            SettingsPage._save_now(self, needs_restart=needs_restart, change_desc=change_desc)

    page = Dummy()

    SettingsPage._on_tool_result_budget_field(
        page,
        "read_file",
        "inline_budget_tokens",
        3200,
    )

    saved = load_config(tmp_paths)
    assert saved.behavior.context.tool_result_budgets["read_file"].inline_budget_tokens == 3200
    assert page._status.calls[-1] == (0, True)


def test_settings_tool_loop_reminder_fields_save_require_restart(tmp_paths):
    cfg = _minimal_root_config()

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
            self._suppress_signals = False
            self._runtime = type("RuntimeStub", (), {"paths": tmp_paths, "config": cfg})()
            self._baseline = cfg.model_copy(deep=True)
            self._status = FakeStatus()

        def _cfg(self):
            return self._runtime.config

        def _count_changes(self) -> int:
            return SettingsPage._count_changes(self)

        def _save_now(self, *, needs_restart: bool, change_desc: str = "") -> None:
            SettingsPage._save_now(self, needs_restart=needs_restart, change_desc=change_desc)

    page = Dummy()

    SettingsPage._on_agent_tool_loop_field_changed(
        page,
        "chat",
        "tool_loop_reminder_interval",
        6,
    )
    SettingsPage._on_agent_tool_loop_field_changed(
        page,
        "chat",
        "tool_loop_final_warning_count",
        2,
    )
    SettingsPage._on_agent_tool_loop_field_changed(
        page,
        "chat",
        "tool_loop_final_grace_loops",
        0,
    )

    saved = load_config(tmp_paths)
    assert saved.agents.chat.tool_loop_reminder_interval == 6
    assert saved.agents.chat.tool_loop_final_warning_count == 2
    assert saved.agents.chat.tool_loop_final_grace_loops == 0
    assert saved.agents.chat.max_loops == 25
    assert page._status.calls
    assert all(needs_restart is True for _count, needs_restart in page._status.calls)


def test_settings_budget_panels_save_new_fields_from_ui(qapp, tmp_paths):
    cfg = _minimal_root_config()
    cfg.agents.persona_gen = AgentConfig(provider="ds", model="persona-gen")
    cfg.behavior.context.recommended_context_budget.fallback_budget_tokens = 98_765
    cfg.behavior.context.tool_result_default_hard_cap_tokens = 4_321

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
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "Agent 输出预算" in labels
        assert "人格生成回复上限" in labels
        assert "主聊天工具收尾上限" in labels
        assert "人格生成工具收尾上限" not in labels
        assert "滚动摘要压缩" in labels
        assert "上下文窗口高级" in labels
        assert "自动预算兜底" in labels
        assert "主动思考预算" in labels
        assert "人格编辑历史" not in labels

        def spin(name: str) -> QtWidgets.QSpinBox:
            widget = page.findChild(QtWidgets.QSpinBox, name)
            assert widget is not None, name
            return widget

        def check(name: str) -> QtWidgets.QCheckBox:
            widget = page.findChild(QtWidgets.QCheckBox, name)
            assert widget is not None, name
            return widget

        def assert_restart_required() -> None:
            assert "需重启" in page._status._info.text()
            assert page._status._restart_btn.isEnabled()

        ctx_auto = check("contextMaxContextTokensAutoCheck")
        ctx_max = spin("contextMaxContextTokensSpin")
        assert ctx_auto.isChecked()
        assert not ctx_max.isEnabled()
        assert ctx_max.value() == 98_765
        assert spin("toolBudgetread_fileHardCapSpin").value() == 4_321
        ctx_auto.setChecked(False)
        ctx_max.setValue(4_000_000)
        ctx_max.editingFinished.emit()
        assert_restart_required()
        page._status.mark_restart_done()

        trigger_pct = spin("summarizeTriggerContextPercentSpin")
        trigger_pct.setValue(80)
        trigger_pct.editingFinished.emit()
        target_pct = spin("summarizeTargetContextPercentSpin")
        target_pct.setValue(60)
        target_pct.editingFinished.emit()
        page._status.mark_restart_done()

        prompt_overhead = spin("context_prompt_overhead_estimate_tokens_spin")
        prompt_overhead.setValue(16_000)
        prompt_overhead.editingFinished.emit()
        assert_restart_required()
        page._status.mark_restart_done()

        model_budget = spin("contextRecommendedModelBudgetdeepseek_v4_proSpin")
        model_budget.setValue(351_000)
        model_budget.editingFinished.emit()
        assert_restart_required()
        page._status.mark_restart_done()

        length_rule_threshold = spin("contextRecommendedLengthRule0ThresholdSpin")
        length_rule_threshold.setValue(900_000)
        length_rule_threshold.editingFinished.emit()
        length_rule_budget = spin("contextRecommendedLengthRule0BudgetSpin")
        length_rule_budget.setValue(280_000)
        length_rule_budget.editingFinished.emit()
        scale_percent = spin("context_recommended_context_length_scale_percent_spin")
        scale_percent.setValue(70)
        scale_percent.editingFinished.emit()
        fallback_budget = spin("context_recommended_fallback_budget_tokens_spin")
        fallback_budget.setValue(88_000)
        fallback_budget.editingFinished.emit()
        page._status.mark_restart_done()

        router_text = spin("behavior_proactive_router_text_limit_tokens_spin")
        router_text.setValue(384)
        router_text.editingFinished.emit()
        assert_restart_required()
        page._status.mark_restart_done()

        assert page.findChild(
            QtWidgets.QSpinBox,
            "behavior_persona_refine_history_turns_spin",
        ) is None

        persona_max = spin("agentBudgetpersona_genMaxTokensSpin")
        persona_max.setValue(32_768)
        persona_max.editingFinished.emit()

        reasoning_unspecified = check("agentBudgetpersona_genReasoningUnspecifiedCheck")
        reasoning_tokens = spin("agentBudgetpersona_genReasoningMaxTokensSpin")
        assert reasoning_unspecified.isChecked()
        reasoning_unspecified.setChecked(False)
        reasoning_tokens.setValue(8192)
        reasoning_tokens.editingFinished.emit()

        assert page.findChild(
            QtWidgets.QSpinBox,
            "agentBudgetpersona_genToolLoopFinalMaxTokensSpin",
        ) is None
        page._status.mark_restart_done()
        final_tokens = spin("agentBudgetchatToolLoopFinalMaxTokensSpin")
        final_tokens.setValue(2048)
        final_tokens.editingFinished.emit()
        assert_restart_required()

        saved = load_config(tmp_paths)
        assert saved.behavior.context.max_context_tokens == 4_000_000
        assert saved.behavior.summarize.trigger_at_context_percent == 80
        assert saved.behavior.summarize.target_after_context_percent == 60
        assert saved.behavior.context.prompt_overhead_estimate_tokens == 16_000
        rec = saved.behavior.context.recommended_context_budget
        assert rec.model_name_budget_tokens["deepseek-v4-pro"] == 351_000
        assert rec.context_length_rules[0].min_context_length_tokens == 900_000
        assert rec.context_length_rules[0].budget_tokens == 280_000
        assert rec.context_length_scale_percent == 70
        assert rec.fallback_budget_tokens == 88_000
        assert saved.behavior.context.tool_result_default_hard_cap_tokens == 4_321
        assert saved.behavior.proactive_router_text_limit_tokens == 384
        assert saved.agents.persona_gen is not None
        assert saved.agents.persona_gen.max_tokens == 32_768
        assert saved.agents.persona_gen.reasoning is not None
        assert saved.agents.persona_gen.reasoning.max_tokens == 8192
        assert saved.agents.persona_gen.tool_loop_final_max_tokens == 4096
        assert saved.agents.chat.tool_loop_final_max_tokens == 2048
    finally:
        page.deleteLater()
