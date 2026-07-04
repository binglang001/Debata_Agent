"""设置页的人格管理与生理状态配置。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from agents.persona_loader import load_persona
from app_config.schema import ReasoningConfig

from ...theme import Spacing
from ...widgets import show_message
from ...widgets.unit_fields import unit_spinbox
from ...wizard.components import SectionCard
from .widgets import CollapsibleSection


class SettingsPersonaPhysiologyMixin:
    """人格管理、生理状态和当前人格年龄覆盖设置。"""

    def _build_persona_physiology_section(self) -> SectionCard:
        cfg = self._cfg().persona_management
        card = SectionCard(
            title="人格与生理",
            subtitle="人格管理后台、精力/饱腹工具模拟和当前人格年龄覆盖。改动后重启 Debata 生效。",
        )

        overview = QFormLayout()
        overview.setSpacing(Spacing.SM)

        enabled = QCheckBox("启用人格管理")
        enabled.setObjectName("personaManagementEnabledCheck")
        enabled.setChecked(cfg.enabled)
        enabled.toggled.connect(self._on_persona_management_enabled_toggled)
        self._persona_management_enabled_chk = enabled
        overview.addRow(QLabel("人格管理"), enabled)

        persona_label = QLabel(self._persona_age_label_text())
        persona_label.setObjectName("personaManagementCurrentPersonaLabel")
        persona_label.setProperty("role", "secondary")
        persona_label.setWordWrap(True)
        self._persona_management_persona_label = persona_label
        overview.addRow(QLabel("当前人格"), persona_label)

        age_edit = QLineEdit()
        age_edit.setObjectName("personaManagementAgeOverrideEdit")
        age_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"\d*"), age_edit)
        )
        age_edit.setText(self._current_age_override_text())
        age_edit.setPlaceholderText("不覆盖")
        age_edit.setToolTip("写入当前人格的年龄覆盖；清空表示删除覆盖。")
        age_edit.editingFinished.connect(
            lambda: self._on_persona_age_override_changed(age_edit.text())
        )
        self._persona_management_age_override_edit = age_edit
        overview.addRow(QLabel("年龄覆盖"), unit_spinbox(age_edit, "岁"))

        card.add_layout(overview)

        physiology = cfg.physiology
        mode_form = QFormLayout()
        mode_form.setSpacing(Spacing.SM)

        energy_mode = self._persona_mode_combo(physiology.energy.mode)
        energy_mode.setObjectName("personaManagementEnergyModeCombo")
        energy_mode.currentIndexChanged.connect(
            lambda *_: self._on_persona_energy_mode_changed(energy_mode.currentData())
        )
        self._persona_management_energy_mode_combo = energy_mode
        mode_form.addRow(QLabel("精力模式"), energy_mode)

        satiety_mode = self._persona_mode_combo(physiology.satiety.mode)
        satiety_mode.setObjectName("personaManagementSatietyModeCombo")
        satiety_mode.currentIndexChanged.connect(
            lambda *_: self._on_persona_satiety_mode_changed(satiety_mode.currentData())
        )
        self._persona_management_satiety_mode_combo = satiety_mode
        mode_form.addRow(QLabel("饱腹模式"), satiety_mode)
        card.add_layout(mode_form)

        card.add_content(self._build_persona_background_agent_section())

        energy_section = CollapsibleSection(
            "精力参数",
            "sleep 工具只由精力模式是否为工具模拟决定；恢复数值用于模型失败或离线对账兜底。",
            expanded=True,
        )
        energy_form = QFormLayout()
        energy_form.setSpacing(Spacing.SM)
        energy = physiology.energy
        self._add_persona_double_spin_row(
            energy_form,
            "清醒衰减",
            "energy",
            "decay_per_hour",
            energy.decay_per_hour,
            "点/小时",
        )
        self._add_persona_double_spin_row(
            energy_form,
            "兜底睡眠恢复",
            "energy",
            "recovery_per_hour_sleep",
            energy.recovery_per_hour_sleep,
            "点/小时",
        )
        self._add_persona_int_spin_row(
            energy_form,
            "长睡眠阈值",
            "energy",
            "long_sleep_threshold_minutes",
            energy.long_sleep_threshold_minutes,
            "分钟",
            maximum=24 * 60,
        )
        self._add_persona_int_spin_row(
            energy_form,
            "单次睡眠上限",
            "energy",
            "max_sleep_minutes",
            energy.max_sleep_minutes,
            "分钟",
            maximum=7 * 24 * 60,
        )
        self._add_persona_int_spin_row(
            energy_form,
            "耗尽宽限",
            "energy.collapse",
            "grace_minutes",
            energy.collapse.grace_minutes,
            "分钟",
            maximum=24 * 60,
        )
        self._add_persona_double_spin_row(
            energy_form,
            "昏睡时长",
            "energy.collapse",
            "sleep_hours",
            energy.collapse.sleep_hours,
            "小时",
            maximum=72.0,
        )
        self._add_persona_double_spin_row(
            energy_form,
            "昏睡心情惩罚",
            "energy.collapse",
            "mood_penalty",
            energy.collapse.mood_penalty,
            "点",
            maximum=100.0,
        )
        energy_section.add_layout(energy_form)
        card.add_content(energy_section)

        satiety_section = CollapsibleSection(
            "饱腹参数",
            "eat 工具只由饱腹模式是否为工具模拟决定；恢复数值用于模型失败或离线对账兜底。",
            expanded=True,
        )
        satiety_form = QFormLayout()
        satiety_form.setSpacing(Spacing.SM)
        satiety = physiology.satiety
        self._add_persona_double_spin_row(
            satiety_form,
            "饱腹衰减",
            "satiety",
            "decay_per_hour",
            satiety.decay_per_hour,
            "点/小时",
        )
        self._add_persona_double_spin_row(
            satiety_form,
            "兜底进食恢复",
            "satiety",
            "recovery_per_minute",
            satiety.recovery_per_minute,
            "点/分钟",
        )
        self._add_persona_int_spin_row(
            satiety_form,
            "单次进食上限",
            "satiety",
            "max_eat_minutes",
            satiety.max_eat_minutes,
            "分钟",
            maximum=24 * 60,
        )
        satiety_section.add_layout(satiety_form)
        card.add_content(satiety_section)

        return card

    def _persona_physiology_controls(self) -> dict[tuple[str, str], QSpinBox | QDoubleSpinBox]:
        controls = getattr(self, "_persona_management_physiology_controls", None)
        if controls is None:
            controls = {}
            self._persona_management_physiology_controls = controls
        return controls

    def _persona_agent_controls(self) -> dict[tuple[str, str], QWidget]:
        controls = getattr(self, "_persona_management_agent_controls", None)
        if controls is None:
            controls = {}
            self._persona_management_agent_controls = controls
        return controls

    def _build_persona_background_agent_section(self) -> CollapsibleSection:
        section = CollapsibleSection(
            "后台 Agent",
            "provider 和模型留空时继承主聊天配置；修改立即保存，重启后生效。",
            expanded=True,
        )
        section.add_content(
            self._build_persona_agent_form(
                "persona_agent",
                "人格分析",
                include_enabled=False,
                int_fields=(
                    ("timer_interval_minutes", "维护间隔", "分钟", 1, 10080),
                    ("min_interval_seconds", "最小间隔", "秒", 0, 86400),
                ),
            )
        )
        section.add_content(
            self._build_persona_agent_form(
                "social_agent",
                "社交决策",
                include_enabled=True,
                int_fields=(("interval_minutes", "决策间隔", "分钟", 1, 10080),),
            )
        )
        section.add_content(
            self._build_persona_agent_form(
                "subconscious",
                "潜意识",
                include_enabled=True,
                int_fields=(("interval_minutes", "处理间隔", "分钟", 1, 10080),),
                double_fields=(
                    ("merge_window_seconds", "合并窗口", "秒", 0.0, 86400.0, 1.0),
                    ("max_window_seconds", "最长窗口", "秒", 0.0, 86400.0, 1.0),
                    ("min_wake_score", "唤醒分数", "", 0.0, 1.0, 0.05),
                ),
            )
        )
        return section

    def _build_persona_agent_form(
        self,
        agent_name: str,
        title: str,
        *,
        include_enabled: bool,
        int_fields: tuple[tuple[str, str, str, int, int], ...],
        double_fields: tuple[tuple[str, str, str, float, float, float], ...] = (),
    ) -> QWidget:
        agent_cfg = self._persona_agent_target(agent_name)
        box = QWidget()
        layout = QFormLayout(box)
        layout.setSpacing(Spacing.SM)

        title_label = QLabel(title)
        title_label.setProperty("role", "title-3")
        layout.addRow(title_label)

        if include_enabled:
            enabled = QCheckBox(f"启用{title}")
            enabled.setObjectName(f"personaManagement{agent_name}EnabledCheck")
            enabled.setChecked(bool(agent_cfg.enabled))
            enabled.toggled.connect(
                lambda on, an=agent_name: self._on_persona_agent_field(an, "enabled", on)
            )
            self._persona_agent_controls()[(agent_name, "enabled")] = enabled
            layout.addRow(QLabel("开关"), enabled)

        provider = QLineEdit(agent_cfg.provider)
        provider.setObjectName(f"personaManagement{agent_name}ProviderEdit")
        provider.setPlaceholderText("留空继承主聊天 provider")
        provider.editingFinished.connect(
            lambda an=agent_name, e=provider: self._on_persona_agent_text_field(
                an,
                "provider",
                e.text().strip(),
            )
        )
        self._persona_agent_controls()[(agent_name, "provider")] = provider
        layout.addRow(QLabel("Provider"), provider)

        model = QLineEdit(agent_cfg.model)
        model.setObjectName(f"personaManagement{agent_name}ModelEdit")
        model.setPlaceholderText("留空继承主聊天模型")
        model.editingFinished.connect(
            lambda an=agent_name, e=model: self._on_persona_agent_text_field(
                an,
                "model",
                e.text().strip(),
            )
        )
        self._persona_agent_controls()[(agent_name, "model")] = model
        layout.addRow(QLabel("模型 ID"), model)

        self._add_persona_agent_reasoning_row(layout, agent_name, agent_cfg.reasoning)
        self._add_persona_agent_int_row(
            layout,
            agent_name,
            "max_tokens",
            "回复上限",
            agent_cfg.max_tokens,
            "token",
            1,
            4_000_000,
            step=1024,
        )
        self._add_persona_agent_reasoning_tokens_row(layout, agent_name, agent_cfg.reasoning)
        for field, label, unit, minimum, maximum in int_fields:
            self._add_persona_agent_int_row(
                layout,
                agent_name,
                field,
                label,
                getattr(agent_cfg, field),
                unit,
                minimum,
                maximum,
            )
        for field, label, unit, minimum, maximum, step in double_fields:
            self._add_persona_agent_double_row(
                layout,
                agent_name,
                field,
                label,
                getattr(agent_cfg, field),
                unit,
                minimum,
                maximum,
                step,
            )
        return box

    def _add_persona_agent_reasoning_row(
        self,
        form: QFormLayout,
        agent_name: str,
        reasoning: ReasoningConfig | None,
    ) -> None:
        enabled = QCheckBox("启用")
        enabled.setObjectName(f"personaManagement{agent_name}ReasoningEnabledCheck")
        is_on = bool(reasoning and reasoning.enabled)
        enabled.setChecked(is_on)
        self._persona_agent_controls()[(agent_name, "reasoning.enabled")] = enabled

        depth = QComboBox()
        depth.setObjectName(f"personaManagement{agent_name}ReasoningDepthCombo")
        depth.addItem("默认", None)
        depth.addItem("低", "low")
        depth.addItem("中", "medium")
        depth.addItem("高", "high")
        current_budget = reasoning.budget if reasoning else None
        idx = depth.findData(current_budget)
        depth.setCurrentIndex(idx if idx >= 0 else 0)
        depth.setEnabled(is_on)
        self._persona_agent_controls()[(agent_name, "reasoning.budget")] = depth

        def _changed(_=None, an=agent_name, c=enabled, d=depth) -> None:
            d.setEnabled(c.isChecked())
            self._on_persona_agent_reasoning_changed(an, c.isChecked(), d.currentData())

        enabled.toggled.connect(_changed)
        depth.currentIndexChanged.connect(_changed)

        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        row.addWidget(enabled)
        row.addWidget(QLabel("深度"))
        row.addWidget(depth)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        form.addRow(QLabel("思考"), wrap)

    def _add_persona_agent_reasoning_tokens_row(
        self,
        form: QFormLayout,
        agent_name: str,
        reasoning: ReasoningConfig | None,
    ) -> None:
        value = reasoning.max_tokens if reasoning and reasoning.max_tokens is not None else None
        spin = QSpinBox()
        spin.setObjectName(f"personaManagement{agent_name}ReasoningMaxTokensSpin")
        spin.setRange(512, 4_000_000)
        spin.setSingleStep(1024)
        spin.setValue(value or 4096)
        unspecified = QCheckBox("不指定")
        unspecified.setObjectName(f"personaManagement{agent_name}ReasoningMaxTokensAutoCheck")
        unspecified.setChecked(value is None)
        spin.setEnabled(not unspecified.isChecked())
        self._persona_agent_controls()[(agent_name, "reasoning.max_tokens")] = spin
        self._persona_agent_controls()[(agent_name, "reasoning.max_tokens_unspecified")] = unspecified

        def _on_unspecified(on: bool, an=agent_name, s=spin) -> None:
            s.setEnabled(not on)
            self._on_persona_agent_reasoning_max_tokens_changed(
                an,
                None if on else s.value(),
            )

        unspecified.toggled.connect(_on_unspecified)
        spin.editingFinished.connect(
            lambda an=agent_name, s=spin, c=unspecified: None
            if c.isChecked()
            else self._on_persona_agent_reasoning_max_tokens_changed(an, s.value())
        )

        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        row.addWidget(unit_spinbox(spin, "token", add_stretch=False))
        row.addWidget(unspecified)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        form.addRow(QLabel("思考上限"), wrap)

    def _add_persona_agent_int_row(
        self,
        form: QFormLayout,
        agent_name: str,
        field: str,
        label: str,
        value: int,
        unit: str,
        minimum: int,
        maximum: int,
        *,
        step: int = 1,
    ) -> None:
        spin = QSpinBox()
        spin.setObjectName(f"personaManagement{agent_name}{field}Spin")
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        self._persona_agent_controls()[(agent_name, field)] = spin
        spin.editingFinished.connect(
            lambda an=agent_name, f=field, s=spin: self._on_persona_agent_field(
                an,
                f,
                s.value(),
            )
        )
        form.addRow(QLabel(label), unit_spinbox(spin, unit))

    def _add_persona_agent_double_row(
        self,
        form: QFormLayout,
        agent_name: str,
        field: str,
        label: str,
        value: float,
        unit: str,
        minimum: float,
        maximum: float,
        step: float,
    ) -> None:
        spin = QDoubleSpinBox()
        spin.setObjectName(f"personaManagement{agent_name}{field}Spin")
        spin.setRange(minimum, maximum)
        spin.setDecimals(3)
        spin.setSingleStep(step)
        spin.setValue(value)
        self._persona_agent_controls()[(agent_name, field)] = spin
        spin.editingFinished.connect(
            lambda an=agent_name, f=field, s=spin: self._on_persona_agent_field(
                an,
                f,
                s.value(),
            )
        )
        form.addRow(QLabel(label), unit_spinbox(spin, unit))

    def _persona_mode_combo(self, current: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem("关闭", "disabled")
        combo.addItem("工具模拟", "tool")
        idx = combo.findData(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        return combo

    def _on_persona_management_enabled_toggled(self, on: bool) -> None:
        if self._suppress_signals:
            return
        cfg = self._cfg().persona_management
        if on and not self._persona_has_configured_age():
            self._suppress_signals = True
            self._persona_management_enabled_chk.setChecked(False)
            self._suppress_signals = False
            show_message(
                self,
                "需要填写年龄",
                "当前人格没有年龄。请先在“年龄覆盖”里填写年龄，再启用人格管理。",
                is_danger=True,
            )
            return
        if cfg.enabled == on:
            return
        cfg.enabled = on
        self._save_now(
            needs_restart=True,
            change_desc=f"persona_management.enabled={on}",
        )

    def _on_persona_energy_mode_changed(self, mode: str) -> None:
        self._on_persona_physiology_field("energy", "mode", mode)

    def _on_persona_satiety_mode_changed(self, mode: str) -> None:
        self._on_persona_physiology_field("satiety", "mode", mode)

    def _on_persona_physiology_field(self, section: str, field: str, value: Any) -> None:
        if self._suppress_signals:
            return
        target = self._persona_physiology_target(section)
        if getattr(target, field) == value:
            return
        setattr(target, field, value)
        self._save_now(
            needs_restart=True,
            change_desc=f"persona_management.physiology.{section}.{field}",
        )

    def _on_persona_agent_text_field(
        self,
        agent_name: str,
        field: str,
        value: str,
    ) -> None:
        self._on_persona_agent_field(agent_name, field, value)

    def _on_persona_agent_field(
        self,
        agent_name: str,
        field: str,
        value: Any,
    ) -> None:
        if self._suppress_signals:
            return
        target = self._persona_agent_target(agent_name)
        if getattr(target, field) == value:
            return
        setattr(target, field, value)
        self._save_now(
            needs_restart=True,
            change_desc=f"persona_management.{agent_name}.{field}",
        )

    def _on_persona_agent_reasoning_changed(
        self,
        agent_name: str,
        enabled: bool,
        budget: str | None,
    ) -> None:
        if self._suppress_signals:
            return
        agent_cfg = self._persona_agent_target(agent_name)
        current = agent_cfg.reasoning
        if enabled:
            max_tokens = current.max_tokens if current else None
            new_reasoning = ReasoningConfig(
                enabled=True,
                budget=budget,
                max_tokens=max_tokens,
            )
        else:
            max_tokens = current.max_tokens if current else None
            new_reasoning = (
                ReasoningConfig(enabled=False, budget=None, max_tokens=max_tokens)
                if max_tokens is not None
                else None
            )
        if current == new_reasoning:
            return
        agent_cfg.reasoning = new_reasoning
        self._save_now(
            needs_restart=True,
            change_desc=f"persona_management.{agent_name}.reasoning",
        )

    def _on_persona_agent_reasoning_max_tokens_changed(
        self,
        agent_name: str,
        value: int | None,
    ) -> None:
        if self._suppress_signals:
            return
        agent_cfg = self._persona_agent_target(agent_name)
        if agent_cfg.reasoning is None:
            if value is None:
                return
            agent_cfg.reasoning = ReasoningConfig(
                enabled=False,
                budget=None,
                max_tokens=int(value),
            )
        elif agent_cfg.reasoning.max_tokens != value:
            agent_cfg.reasoning.max_tokens = value
        else:
            return
        self._save_now(
            needs_restart=True,
            change_desc=f"persona_management.{agent_name}.reasoning.max_tokens",
        )

    def _on_persona_age_override_changed(self, text: str) -> None:
        if self._suppress_signals:
            return
        persona_name = self._current_persona_key()
        overrides = self._cfg().persona_management.age.overrides
        age_text = text.strip()
        if age_text == "":
            if persona_name not in overrides:
                return
            overrides.pop(persona_name, None)
            change_desc = f"persona_management.age.overrides.{persona_name}.deleted"
            cfg = self._cfg().persona_management
            if cfg.enabled and self._current_persona_declared_age() is None:
                cfg.enabled = False
                if hasattr(self, "_persona_management_enabled_chk"):
                    old_suppress = self._suppress_signals
                    self._suppress_signals = True
                    try:
                        self._persona_management_enabled_chk.setChecked(False)
                    finally:
                        self._suppress_signals = old_suppress
                change_desc += ".disabled_persona_management"
        else:
            age = int(age_text)
            if overrides.get(persona_name) == age:
                return
            overrides[persona_name] = age
            change_desc = f"persona_management.age.overrides.{persona_name}"
        if hasattr(self, "_persona_management_persona_label"):
            self._persona_management_persona_label.setText(self._persona_age_label_text())
        self._save_now(
            needs_restart=True,
            change_desc=change_desc,
        )

    def _set_persona_age_override_text(self) -> None:
        if not hasattr(self, "_persona_management_age_override_edit"):
            return
        self._persona_management_age_override_edit.setText(
            self._current_age_override_text()
        )

    def _persona_physiology_target(self, section: str):
        obj = self._cfg().persona_management.physiology
        for part in section.split("."):
            obj = getattr(obj, part)
        return obj

    def _persona_agent_target(self, agent_name: str):
        return getattr(self._cfg().persona_management, agent_name)

    def _add_persona_int_spin_row(
        self,
        form: QFormLayout,
        label: str,
        section: str,
        field: str,
        value: int,
        unit: str,
        *,
        maximum: int = 100_000,
    ) -> None:
        spin = QSpinBox()
        spin.setObjectName(f"personaManagement{section.replace('.', '_')}_{field}Spin")
        spin.setRange(0, maximum)
        spin.setValue(value)
        self._persona_physiology_controls()[(section, field)] = spin
        spin.editingFinished.connect(
            lambda s=spin, sec=section, f=field: self._on_persona_physiology_field(
                sec,
                f,
                s.value(),
            )
        )
        form.addRow(QLabel(label), unit_spinbox(spin, unit))

    def _add_persona_double_spin_row(
        self,
        form: QFormLayout,
        label: str,
        section: str,
        field: str,
        value: float,
        unit: str,
        *,
        maximum: float = 10_000.0,
    ) -> None:
        spin = QDoubleSpinBox()
        spin.setObjectName(f"personaManagement{section.replace('.', '_')}_{field}Spin")
        spin.setRange(0.0, maximum)
        spin.setDecimals(3)
        spin.setSingleStep(0.1)
        spin.setValue(value)
        self._persona_physiology_controls()[(section, field)] = spin
        spin.editingFinished.connect(
            lambda s=spin, sec=section, f=field: self._on_persona_physiology_field(
                sec,
                f,
                s.value(),
            )
        )
        form.addRow(QLabel(label), unit_spinbox(spin, unit))

    def _current_persona_key(self) -> str:
        return self._cfg().persona.active

    def _current_persona_display_name(self) -> str:
        persona = getattr(self._runtime, "persona", None)
        display = getattr(persona, "display_name", None)
        if callable(display):
            name = display()
            if name:
                return str(name)
        name = getattr(persona, "name", None)
        return str(name or self._current_persona_key())

    def _current_persona_declared_age(self) -> int | None:
        persona = getattr(self._runtime, "persona", None)
        get_age = getattr(persona, "get_age", None)
        if callable(get_age):
            return get_age()
        paths = getattr(self._runtime, "paths", None)
        if paths is None:
            return None
        try:
            loaded = load_persona(paths, self._current_persona_key())
        except Exception:  # noqa: BLE001
            return None
        return loaded.get_age()

    def _current_age_override_text(self) -> str:
        persona_name = self._current_persona_key()
        overrides = self._cfg().persona_management.age.overrides
        override = overrides.get(persona_name)
        if override is not None:
            return str(int(override))
        return ""

    def _persona_has_configured_age(self) -> bool:
        persona_name = self._current_persona_key()
        if persona_name in self._cfg().persona_management.age.overrides:
            return True
        return self._current_persona_declared_age() is not None

    def _persona_age_label_text(self) -> str:
        persona_name = self._current_persona_key()
        display_name = self._current_persona_display_name()
        override = self._cfg().persona_management.age.overrides.get(persona_name)
        declared = self._current_persona_declared_age()
        if override is not None:
            age_text = f"覆盖年龄 {override} 岁"
        elif declared is not None:
            age_text = f"人格档案年龄 {declared} 岁"
        else:
            age_text = "未设置年龄"
        return f"{display_name}（{persona_name}） · {age_text}"

    def _refresh_persona_physiology_controls(self) -> None:
        cfg = self._cfg().persona_management
        old_suppress = self._suppress_signals
        self._suppress_signals = True
        try:
            if hasattr(self, "_persona_management_enabled_chk"):
                self._persona_management_enabled_chk.setChecked(cfg.enabled)
            if hasattr(self, "_persona_management_energy_mode_combo"):
                idx = self._persona_management_energy_mode_combo.findData(
                    cfg.physiology.energy.mode
                )
                self._persona_management_energy_mode_combo.setCurrentIndex(
                    idx if idx >= 0 else 0
                )
            if hasattr(self, "_persona_management_satiety_mode_combo"):
                idx = self._persona_management_satiety_mode_combo.findData(
                    cfg.physiology.satiety.mode
                )
                self._persona_management_satiety_mode_combo.setCurrentIndex(
                    idx if idx >= 0 else 0
                )
            if hasattr(self, "_persona_management_age_override_edit"):
                self._set_persona_age_override_text()
            for (section, field), widget in self._persona_physiology_controls().items():
                widget.setValue(getattr(self._persona_physiology_target(section), field))
            if hasattr(self, "_persona_management_persona_label"):
                self._persona_management_persona_label.setText(self._persona_age_label_text())
            for (agent_name, field), widget in self._persona_agent_controls().items():
                agent_cfg = self._persona_agent_target(agent_name)
                if field == "reasoning.enabled":
                    widget.setChecked(bool(agent_cfg.reasoning and agent_cfg.reasoning.enabled))
                elif field == "reasoning.budget":
                    value = agent_cfg.reasoning.budget if agent_cfg.reasoning else None
                    idx = widget.findData(value)
                    widget.setCurrentIndex(idx if idx >= 0 else 0)
                    widget.setEnabled(bool(agent_cfg.reasoning and agent_cfg.reasoning.enabled))
                elif field == "reasoning.max_tokens":
                    value = (
                        agent_cfg.reasoning.max_tokens
                        if agent_cfg.reasoning and agent_cfg.reasoning.max_tokens is not None
                        else 4096
                    )
                    widget.setValue(value)
                elif field == "reasoning.max_tokens_unspecified":
                    widget.setChecked(
                        not agent_cfg.reasoning
                        or agent_cfg.reasoning.max_tokens is None
                    )
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(getattr(agent_cfg, field)))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(getattr(agent_cfg, field)))
                else:
                    widget.setValue(getattr(agent_cfg, field))
        finally:
            self._suppress_signals = old_suppress
