"""初始化向导组件回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

import pytest

QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
Qt = QtCore.Qt

from agents.persona_gen_agent import PersonaBrief
from ui.widgets.model_combo import ModelComboBox
from ui.wizard.components import ApiKeyInput
from ui.wizard.context import WizardContext
from ui.wizard.persona_creator import PersonaCreatorStepView
from ui.wizard.step_views.features import _TTSFeatureCard
from ui.wizard.step_views.welcome import WelcomeStepView


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


def test_persona_creator_age_input_flows_into_brief(qapp):
    context = WizardContext()
    view = PersonaCreatorStepView(context)
    try:
        view._name_edit.setText("Mika")
        view._age_edit.setText("18")

        brief = view._current_brief()
        assert brief.age == 18

        view._age_edit.setText("0")
        assert view._current_brief().age == 0

        context.persona.brief = PersonaBrief(name="Mika", age=21)
        view.refresh()
        assert view._age_edit.text() == "21"
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
