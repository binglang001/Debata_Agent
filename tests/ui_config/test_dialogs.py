"""UI 配置基础控件和设置弹窗测试。"""

# ruff: noqa: E402

from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QDialog = QtWidgets.QDialog

from app_config.schema import ASRFeatureConfig, EmbeddingFeatureConfig, TTSFeatureConfig
from ui.dashboard.settings_page import (
    _AddProviderDialog,
    _ASREditDialog,
    _EmbeddingEditDialog,
    _load_provider_presets_for_dialog,
    _TTSEditDialog,
)


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

    presets_dir = Path(__file__).resolve().parent.parent.parent / "providers" / "presets"
    presets = _load_provider_presets_for_dialog(presets_dir)
    preset_ids = {p[0] for p in presets}

    assert {"groq", "together", "xai"} <= preset_ids

    dlg = _AddProviderDialog(set(), presets)
    combo_ids = {dlg._preset_combo.itemData(i) for i in range(dlg._preset_combo.count())}
    assert {"groq", "together", "xai", "custom"} <= combo_ids
