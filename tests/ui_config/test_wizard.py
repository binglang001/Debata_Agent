"""首次配置向导持久化和 Embedding 流程测试。"""

# ruff: noqa: E402

from __future__ import annotations

from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from app_config import SecretsManager
from app_config.loader import load_config
from ui.dashboard.layout import DEFAULT_LAYOUT
from ui.wizard.flow import next_step
from ui.wizard.step_views.embedding import EmbeddingStepView
from ui.wizard.step_views.summary import SummaryStepView
from ui.wizard.steps import STEPS, StepId
from ui.wizard.window import WizardWindow


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
