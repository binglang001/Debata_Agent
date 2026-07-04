"""设置页人格管理配置保存测试。"""

# ruff: noqa: E402

from __future__ import annotations

from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from app_config.loader import load_config
from ui.dashboard.settings_page import SettingsPage

from .helpers import _minimal_root_config


def test_settings_persona_physiology_saves_modes_and_requires_age(
    qapp,
    tmp_paths,
    monkeypatch,
):
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
            "persona": SimpleNamespace(name="Debata"),
        },
    )()
    messages = []
    monkeypatch.setattr(
        "ui.dashboard.settings.persona_physiology.show_message",
        lambda _parent, title, text, **_kwargs: messages.append((title, text)) or True,
    )

    page = SettingsPage(runtime)
    try:
        labels = [page._settings_nav.item(i).text() for i in range(page._settings_nav.count())]
        assert "人格与生理" in labels
        text = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "兜底睡眠恢复" in text
        assert "兜底进食恢复" in text
        assert "模型失败或离线对账兜底" in text
        assert "睡眠恢复" not in text.replace("兜底睡眠恢复", "")
        assert "进食恢复" not in text.replace("兜底进食恢复", "")

        enabled = page.findChild(QtWidgets.QCheckBox, "personaManagementEnabledCheck")
        energy_mode = page.findChild(QtWidgets.QComboBox, "personaManagementEnergyModeCombo")
        satiety_mode = page.findChild(QtWidgets.QComboBox, "personaManagementSatietyModeCombo")
        age_override = page.findChild(QtWidgets.QLineEdit, "personaManagementAgeOverrideEdit")
        energy_decay = page.findChild(
            QtWidgets.QDoubleSpinBox,
            "personaManagementenergy_decay_per_hourSpin",
        )
        satiety_recovery = page.findChild(
            QtWidgets.QDoubleSpinBox,
            "personaManagementsatiety_recovery_per_minuteSpin",
        )

        assert enabled is not None
        assert energy_mode is not None
        assert satiety_mode is not None
        assert age_override is not None
        assert age_override.text() == ""
        assert energy_decay is not None
        assert satiety_recovery is not None

        age_override.editingFinished.emit()
        assert "debata" not in cfg.persona_management.age.overrides

        enabled.setChecked(True)
        assert cfg.persona_management.enabled is False
        assert enabled.isChecked() is False
        assert messages
        assert "年龄" in messages[-1][1]
        assert "debata" not in cfg.persona_management.age.overrides

        age_override.setText("19")
        age_override.editingFinished.emit()
        enabled.setChecked(True)
        energy_mode.setCurrentIndex(energy_mode.findData("tool"))
        satiety_mode.setCurrentIndex(satiety_mode.findData("tool"))
        energy_decay.setValue(2.25)
        energy_decay.editingFinished.emit()
        satiety_recovery.setValue(0.75)
        satiety_recovery.editingFinished.emit()

        saved = load_config(tmp_paths, set_global=False)
        assert saved.persona_management.enabled is True
        assert saved.persona_management.age.overrides["debata"] == 19
        assert saved.persona_management.physiology.energy.mode == "tool"
        assert saved.persona_management.physiology.satiety.mode == "tool"
        assert saved.persona_management.physiology.energy.decay_per_hour == 2.25
        assert saved.persona_management.physiology.satiety.recovery_per_minute == 0.75
    finally:
        page.deleteLater()

def test_settings_persona_management_background_agents_save_to_persona_management(
    qapp,
    tmp_paths,
):
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
            "persona": SimpleNamespace(name="Debata", get_age=lambda: 18),
        },
    )()

    page = SettingsPage(runtime)
    try:
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "后台 Agent" in labels
        assert "人格分析" in labels
        assert "社交决策" in labels
        assert "潜意识" in labels

        def line(name: str) -> QtWidgets.QLineEdit:
            widget = page.findChild(QtWidgets.QLineEdit, name)
            assert widget is not None, name
            return widget

        def spin(name: str) -> QtWidgets.QSpinBox:
            widget = page.findChild(QtWidgets.QSpinBox, name)
            assert widget is not None, name
            return widget

        def dspin(name: str) -> QtWidgets.QDoubleSpinBox:
            widget = page.findChild(QtWidgets.QDoubleSpinBox, name)
            assert widget is not None, name
            return widget

        def check(name: str) -> QtWidgets.QCheckBox:
            widget = page.findChild(QtWidgets.QCheckBox, name)
            assert widget is not None, name
            return widget

        def combo(name: str) -> QtWidgets.QComboBox:
            widget = page.findChild(QtWidgets.QComboBox, name)
            assert widget is not None, name
            return widget

        persona_provider = line("personaManagementpersona_agentProviderEdit")
        persona_model = line("personaManagementpersona_agentModelEdit")
        persona_reasoning = check("personaManagementpersona_agentReasoningEnabledCheck")
        persona_depth = combo("personaManagementpersona_agentReasoningDepthCombo")
        persona_max = spin("personaManagementpersona_agentmax_tokensSpin")
        persona_reasoning_max = spin("personaManagementpersona_agentReasoningMaxTokensSpin")
        persona_reasoning_auto = check(
            "personaManagementpersona_agentReasoningMaxTokensAutoCheck"
        )
        persona_timer = spin("personaManagementpersona_agenttimer_interval_minutesSpin")
        persona_min_interval = spin(
            "personaManagementpersona_agentmin_interval_secondsSpin"
        )

        persona_provider.setText("ds")
        persona_provider.editingFinished.emit()
        persona_model.setText("persona-model")
        persona_model.editingFinished.emit()
        persona_reasoning.setChecked(True)
        persona_depth.setCurrentIndex(persona_depth.findData("high"))
        persona_max.setValue(12_288)
        persona_max.editingFinished.emit()
        persona_reasoning_auto.setChecked(False)
        persona_reasoning_max.setValue(6144)
        persona_reasoning_max.editingFinished.emit()
        persona_timer.setValue(45)
        persona_timer.editingFinished.emit()
        persona_min_interval.setValue(90)
        persona_min_interval.editingFinished.emit()

        social_enabled = check("personaManagementsocial_agentEnabledCheck")
        social_provider = line("personaManagementsocial_agentProviderEdit")
        social_model = line("personaManagementsocial_agentModelEdit")
        social_interval = spin("personaManagementsocial_agentinterval_minutesSpin")
        social_enabled.setChecked(False)
        social_provider.setText("ds")
        social_provider.editingFinished.emit()
        social_model.setText("social-model")
        social_model.editingFinished.emit()
        social_interval.setValue(12)
        social_interval.editingFinished.emit()

        subconscious_enabled = check("personaManagementsubconsciousEnabledCheck")
        subconscious_provider = line("personaManagementsubconsciousProviderEdit")
        subconscious_model = line("personaManagementsubconsciousModelEdit")
        subconscious_interval = spin("personaManagementsubconsciousinterval_minutesSpin")
        merge_window = dspin("personaManagementsubconsciousmerge_window_secondsSpin")
        max_window = dspin("personaManagementsubconsciousmax_window_secondsSpin")
        min_score = dspin("personaManagementsubconsciousmin_wake_scoreSpin")
        subconscious_enabled.setChecked(False)
        subconscious_provider.setText("ds")
        subconscious_provider.editingFinished.emit()
        subconscious_model.setText("subconscious-model")
        subconscious_model.editingFinished.emit()
        subconscious_interval.setValue(8)
        subconscious_interval.editingFinished.emit()
        merge_window.setValue(10.5)
        merge_window.editingFinished.emit()
        max_window.setValue(120.0)
        max_window.editingFinished.emit()
        min_score.setValue(0.75)
        min_score.editingFinished.emit()

        social_provider.clear()
        social_provider.editingFinished.emit()
        social_model.clear()
        social_model.editingFinished.emit()

        saved = load_config(tmp_paths, set_global=False)
        persona_agent = saved.persona_management.persona_agent
        assert persona_agent.provider == "ds"
        assert persona_agent.model == "persona-model"
        assert persona_agent.reasoning is not None
        assert persona_agent.reasoning.enabled is True
        assert persona_agent.reasoning.budget == "high"
        assert persona_agent.reasoning.max_tokens == 6144
        assert persona_agent.max_tokens == 12_288
        assert persona_agent.timer_interval_minutes == 45
        assert persona_agent.min_interval_seconds == 90

        social_agent = saved.persona_management.social_agent
        assert social_agent.enabled is False
        assert social_agent.provider == ""
        assert social_agent.model == ""
        assert social_agent.interval_minutes == 12

        subconscious = saved.persona_management.subconscious
        assert subconscious.enabled is False
        assert subconscious.provider == "ds"
        assert subconscious.model == "subconscious-model"
        assert subconscious.interval_minutes == 8
        assert subconscious.merge_window_seconds == 10.5
        assert subconscious.max_window_seconds == 120.0
        assert subconscious.min_wake_score == 0.75

        assert "需重启" in page._status._info.text()
    finally:
        page.deleteLater()

def test_settings_persona_age_override_can_be_removed(qapp, tmp_paths, monkeypatch):
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
            "persona": SimpleNamespace(name="Debata"),
        },
    )()
    messages = []
    monkeypatch.setattr(
        "ui.dashboard.settings.persona_physiology.show_message",
        lambda _parent, title, text, **_kwargs: messages.append((title, text)) or True,
    )

    page = SettingsPage(runtime)
    try:
        enabled = page.findChild(QtWidgets.QCheckBox, "personaManagementEnabledCheck")
        age_override = page.findChild(QtWidgets.QLineEdit, "personaManagementAgeOverrideEdit")
        assert enabled is not None
        assert age_override is not None

        age_override.setText("19")
        age_override.editingFinished.emit()
        enabled.setChecked(True)
        assert cfg.persona_management.enabled is True
        assert cfg.persona_management.age.overrides["debata"] == 19

        age_override.clear()
        age_override.editingFinished.emit()
        saved = load_config(tmp_paths, set_global=False)
        assert "debata" not in cfg.persona_management.age.overrides
        assert "debata" not in saved.persona_management.age.overrides
        assert cfg.persona_management.enabled is False
        assert saved.persona_management.enabled is False
        assert enabled.isChecked() is False

        enabled.setChecked(True)
        assert cfg.persona_management.enabled is False
        assert enabled.isChecked() is False
        assert messages
        assert "年龄" in messages[-1][1]
    finally:
        page.deleteLater()

def test_settings_persona_physiology_refresh_restores_parameter_controls(qapp, tmp_paths):
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
            "persona": SimpleNamespace(name="Debata", get_age=lambda: 18),
        },
    )()

    page = SettingsPage(runtime)
    try:
        energy_decay = page.findChild(
            QtWidgets.QDoubleSpinBox,
            "personaManagementenergy_decay_per_hourSpin",
        )
        satiety_recovery = page.findChild(
            QtWidgets.QDoubleSpinBox,
            "personaManagementsatiety_recovery_per_minuteSpin",
        )
        collapse_grace = page.findChild(
            QtWidgets.QSpinBox,
            "personaManagementenergy_collapse_grace_minutesSpin",
        )
        assert energy_decay is not None
        assert satiety_recovery is not None
        assert collapse_grace is not None

        energy_decay.setValue(2.25)
        energy_decay.editingFinished.emit()
        satiety_recovery.setValue(0.75)
        satiety_recovery.editingFinished.emit()
        collapse_grace.setValue(30)
        collapse_grace.editingFinished.emit()
        assert cfg.persona_management.physiology.energy.decay_per_hour == 2.25
        assert cfg.persona_management.physiology.satiety.recovery_per_minute == 0.75
        assert cfg.persona_management.physiology.energy.collapse.grace_minutes == 30

        page._restore_opened_config()

        assert runtime.config.persona_management.physiology.energy.decay_per_hour == 1.5
        assert runtime.config.persona_management.physiology.satiety.recovery_per_minute == 0.5
        assert runtime.config.persona_management.physiology.energy.collapse.grace_minutes == 60
        assert energy_decay.value() == 1.5
        assert satiety_recovery.value() == 0.5
        assert collapse_grace.value() == 60
    finally:
        page.deleteLater()
