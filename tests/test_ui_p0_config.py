"""P0 UI 配置保存路径回归测试。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication
QDialog = QtWidgets.QDialog

from app_config import SecretsManager
from app_config.loader import load_config
from app_config.schema import (
    ASRFeatureConfig,
    AgentConfig,
    AgentsConfig,
    EmbeddingFeatureConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
    TTSFeatureConfig,
)
from ui.dashboard.settings_page import (
    _AddProviderDialog,
    _ASREditDialog,
    _EmbeddingEditDialog,
    _TTSEditDialog,
    _load_provider_presets_for_dialog,
    SettingsPage,
)
from ui.wizard.window import WizardWindow
from ui.wizard.flow import next_step
from ui.wizard.steps import StepId
from ui.wizard.step_views.embedding import EmbeddingStepView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


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
        provider="baidu",
        extra_credentials={"secret_key": "secret"},
    )
    tts_dlg = _TTSEditDialog(tts)
    tts_dlg._type_combo.setCurrentIndex(tts_dlg._type_combo.findData("api"))
    tts_dlg._on_ok()
    assert tts_dlg.result() == QDialog.DialogCode.Accepted
    assert tts_dlg.result_data["provider"] == "baidu"

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
            "provider": "baidu",
            "api_key": "tts-key",
            "extra_credentials": {
                "secret_key": "tts-secret",
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
        assert cfg.features.tts.provider == "baidu"
        assert cfg.features.tts.api_key_id == "tts_baidu"
        assert cfg.features.tts.extra_credentials == {"secret_key": "tts-secret"}
        assert secrets.get("tts_baidu") == "tts-key"
    finally:
        win._completed_emitted = True
        win.close()
        win.deleteLater()


def test_wizard_embedding_step_is_fixed_in_flow():
    assert next_step(
        StepId.FEATURES,
        "recommended",
        {"long_term_memory_mode": "file", "persona_source": "builtin"},
    ) == StepId.EMBEDDING


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


def test_wizard_embedding_endpoint_recovers_independent_provider_preset(qapp):
    from ui.wizard.context import WizardContext

    ctx = WizardContext()
    ctx.long_term_memory_mode = "rag"
    ctx.embedding_type = "api"
    ctx.embedding_provider = "embedding_volcengine"
    ctx.embedding_model = "doubao-embedding-text-240715"
    ctx.embedding_api_key = "embed-key"

    view = EmbeddingStepView(ctx)
    try:
        base_url, api_key = view._embedding_endpoint("embedding_volcengine")
    finally:
        view.deleteLater()

    assert base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert api_key == "embed-key"


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
