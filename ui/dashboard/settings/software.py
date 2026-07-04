"""Software behavior and diagnostics sections for SettingsPage.

This module is a mechanical split from ``ui.dashboard.settings_page``. Keep UI
widgets, ranges, labels, tooltips, and signal wiring equivalent while moving
methods.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
)

from ...theme import Spacing
from ...widgets.unit_fields import unit_spinbox
from ...wizard.components import SectionCard
from . import CollapsibleSection


class SettingsSoftwareMixin:
    def _build_software_behavior_section(self) -> SectionCard:
        card = SectionCard(
            title="软件行为",
            subtitle="界面主题、消息合并、社交决策和陌生人限速。常用项在上方，高风险项保持收起前的简洁说明。",
        )
        card.add_content(self._build_appearance_section())

        b = self._cfg().behavior
        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        merge_spin = QDoubleSpinBox()
        merge_spin.setRange(0.0, 60.0)
        merge_spin.setSingleStep(0.5)
        merge_spin.setValue(b.merge_window_seconds)
        merge_spin.setToolTip("同一窗口内收到的连续消息会合并成一次模型调用。")
        merge_spin.editingFinished.connect(
            lambda: self._on_behavior_field("merge_window_seconds", merge_spin.value())
        )
        form.addRow(QLabel("消息合并窗口"), unit_spinbox(merge_spin, "秒"))

        recall_spin = QDoubleSpinBox()
        recall_spin.setRange(0.0, 60.0)
        recall_spin.setSingleStep(0.5)
        recall_spin.setValue(b.recall_merge_window_seconds)
        recall_spin.setToolTip("撤回事件在该时间内合并处理，避免频繁打断。")
        recall_spin.editingFinished.connect(
            lambda: self._on_behavior_field("recall_merge_window_seconds", recall_spin.value())
        )
        form.addRow(QLabel("撤回合并窗口"), unit_spinbox(recall_spin, "秒"))

        hist_spin = QSpinBox()
        hist_spin.setRange(1, 1000)
        hist_spin.setValue(b.default_history_fetch_count)
        hist_spin.setToolTip("总结工具默认拉取的历史条数。")
        hist_spin.editingFinished.connect(
            lambda: self._on_behavior_field("default_history_fetch_count", hist_spin.value())
        )
        form.addRow(QLabel("默认拉历史条数"), unit_spinbox(hist_spin, "条"))

        card.add_layout(form)

        proactive_section = CollapsibleSection(
            "社交决策",
            "后台定时判断是否需要主动开口。频率越高，成本越高。",
            expanded=False,
        )
        proactive_hint = QLabel("社交决策会定时判断是否需要主动开口。频率越高，成本越高。")
        proactive_hint.setProperty("role", "secondary")
        proactive_hint.setWordWrap(True)
        proactive_section.add_content(proactive_hint)

        proactive_form = QFormLayout()
        proactive_form.setSpacing(Spacing.SM)
        proactive_spin = QDoubleSpinBox()
        proactive_spin.setRange(10.0, 86400.0)
        proactive_spin.setSingleStep(60.0)
        proactive_spin.setValue(b.proactive_think_interval_seconds)
        proactive_spin.editingFinished.connect(
            lambda: self._on_behavior_field("proactive_think_interval_seconds", proactive_spin.value())
        )
        proactive_form.addRow(QLabel("主动社交间隔"), unit_spinbox(proactive_spin, "秒"))

        proactive_budget = QSpinBox()
        proactive_budget.setRange(1024, 65536)
        proactive_budget.setSingleStep(1024)
        proactive_budget.setValue(b.proactive_context_token_budget)
        proactive_budget.setToolTip("社交决策读取近期上下文和记忆的预算。默认 4K。")
        proactive_budget.editingFinished.connect(
            lambda: self._on_behavior_field("proactive_context_token_budget", proactive_budget.value())
        )
        proactive_form.addRow(QLabel("社交决策上下文"), unit_spinbox(proactive_budget, "token"))
        proactive_section.add_layout(proactive_form)
        card.add_content(proactive_section)

        proactive_budget_section = CollapsibleSection(
            "社交决策预算",
            "控制社交决策读取文本、工具结果、摘要和历史的预算。",
            expanded=False,
        )
        proactive_budget_form = QFormLayout()
        proactive_budget_form.setSpacing(Spacing.SM)
        self._add_behavior_spin_row(
            proactive_budget_form,
            "输入文本片段",
            "proactive_router_text_limit_tokens",
            b.proactive_router_text_limit_tokens,
            32,
            100_000,
            "token",
        )
        self._add_behavior_spin_row(
            proactive_budget_form,
            "工具结果 inline",
            "proactive_router_tool_result_inline_tokens",
            b.proactive_router_tool_result_inline_tokens,
            32,
            100_000,
            "token",
        )
        self._add_behavior_spin_row(
            proactive_budget_form,
            "工具结果硬上限",
            "proactive_router_tool_result_hard_cap_tokens",
            b.proactive_router_tool_result_hard_cap_tokens,
            64,
            100_000,
            "token",
        )
        self._add_behavior_spin_row(
            proactive_budget_form,
            "历史摘要上限",
            "proactive_router_summary_limit_tokens",
            b.proactive_router_summary_limit_tokens,
            128,
            100_000,
            "token",
        )
        self._add_behavior_spin_row(
            proactive_budget_form,
            "历史窗口预算",
            "proactive_router_history_token_budget",
            b.proactive_router_history_token_budget,
            1024,
            1_000_000,
            "token",
            step=1024,
        )
        proactive_budget_section.add_layout(proactive_budget_form)
        card.add_content(proactive_budget_section)

        rate_section = CollapsibleSection(
            "陌生人限速",
            "控制未加入白名单的会话成本。好友和管理员不受此限制。",
            expanded=False,
        )
        rate_hint = QLabel("陌生人限速用于控制未加入白名单的会话成本。")
        rate_hint.setProperty("role", "secondary")
        rate_hint.setWordWrap(True)
        rate_section.add_content(rate_hint)

        rate_form = QFormLayout()
        rate_form.setSpacing(Spacing.SM)
        rl_chk = QCheckBox("启用速率限制（非好友）")
        rl_chk.setChecked(b.rate_limit.enabled)
        rl_chk.toggled.connect(lambda on: self._on_behavior_nested("rate_limit", "enabled", on))
        rate_form.addRow(QLabel("速率限制"), rl_chk)

        rl_window = QSpinBox()
        rl_window.setRange(1, 3600)
        rl_window.setValue(b.rate_limit.window_seconds)
        rl_window.editingFinished.connect(lambda: self._on_behavior_nested("rate_limit", "window_seconds", rl_window.value()))
        rate_form.addRow(QLabel("窗口"), unit_spinbox(rl_window, "秒"))

        rl_max = QSpinBox()
        rl_max.setRange(1, 1000)
        rl_max.setValue(b.rate_limit.max_messages)
        rl_max.editingFinished.connect(lambda: self._on_behavior_nested("rate_limit", "max_messages", rl_max.value()))
        rate_form.addRow(QLabel("最多条数"), unit_spinbox(rl_max, "条"))
        rate_section.add_layout(rate_form)
        card.add_content(rate_section)

        return card

    def _build_diagnostics_section(self) -> SectionCard:
        card = SectionCard(
            title="日志与诊断",
            subtitle="日志级别和诊断入口。过于详细的日志建议只在排查问题时临时开启 DEBUG。",
        )

        form = QFormLayout()
        form.setSpacing(Spacing.SM)
        log_combo = QComboBox()
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR"):
            log_combo.addItem(lvl, lvl)
        idx = log_combo.findData(self._cfg().app.log_level)
        if idx >= 0:
            log_combo.setCurrentIndex(idx)
        log_combo.currentIndexChanged.connect(lambda *_: self._on_log_level_changed(log_combo.currentData()))
        form.addRow(QLabel("日志级别"), log_combo)
        card.add_layout(form)

        diag = QLabel("KV 缓存、工具输出和启动耗时诊断由测试与日志页面查看。需要排查时先切到 DEBUG，完成后改回 INFO。")
        diag.setProperty("role", "secondary")
        diag.setWordWrap(True)
        card.add_content(diag)
        return card

    def _add_behavior_spin_row(
        self,
        form: QFormLayout,
        label: str,
        field: str,
        value: int,
        minimum: int,
        maximum: int,
        unit: str,
        *,
        step: int = 1,
    ) -> None:
        spin = QSpinBox()
        spin.setObjectName(f"behavior_{field}_spin")
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.editingFinished.connect(
            lambda s=spin, f=field: self._on_behavior_field(f, s.value())
        )
        form.addRow(QLabel(label), unit_spinbox(spin, unit))
