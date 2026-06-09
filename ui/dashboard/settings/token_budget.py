"""Token-budget setting sections for SettingsPage.

This module is a mechanical split from `ui.dashboard.settings_page`. Keep
behavior equivalent; do not change token budget controls or save callbacks.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QWidget,
)

from app_config.schema import ToolResultBudgetConfig, default_tool_result_budgets

from ...theme import Spacing
from ...widgets.unit_fields import unit_spinbox
from ...wizard.components import SectionCard
from .helpers import _format_tool_result_overrides, _tool_budget_group_hint
from .widgets import CollapsibleSection


def _unit_spinbox_with_checkbox(
    spin: QSpinBox,
    unit: str,
    checkbox: QCheckBox,
) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(Spacing.SM)
    lay.addWidget(unit_spinbox(spin, unit, add_stretch=False))
    lay.addWidget(checkbox)
    lay.addStretch(1)
    return row


class SettingsTokenBudgetMixin:
    def _build_token_budget_section(self) -> SectionCard:
        card = SectionCard(
            title="Token预算",
            subtitle=(
                "建议保留默认值，改动不当可能导致成本上升或回复质量下降。"
            ),
        )
        b = self._cfg().behavior

        hint = QLabel(
            "工作上下文控制每轮最多放入多少历史和记忆；输出预留留给模型回复。"
            "工具预算按工具分别控制，资料过长时会写入 workspace artifact，不会把不完整正文当完整内容给模型。"
        )
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)
        card.add_content(hint)

        action_row = QHBoxLayout()
        restore_btn = QPushButton("恢复推荐 Token 预算")
        restore_btn.setProperty("role", "secondary")
        restore_btn.clicked.connect(self._restore_default_tool_budgets)
        action_row.addStretch(1)
        action_row.addWidget(restore_btn)
        card.add_layout(action_row)

        context_section = CollapsibleSection(
            "上下文总预算",
            "控制每轮可放入的历史、记忆、摘要和默认工具结果预算。通常使用推荐值即可。",
            expanded=False,
        )
        context_form = QFormLayout()
        context_form.setSpacing(Spacing.SM)

        ctx_max = QSpinBox()
        ctx_max.setObjectName("contextMaxContextTokensSpin")
        ctx_max.setRange(1024, 4_000_000)
        ctx_max.setSingleStep(1024)
        ctx_max.setValue(
            b.context.max_context_tokens or self._settings_recommended_context_budget_preview()
        )
        ctx_auto = QCheckBox("按模型自动")
        ctx_auto.setObjectName("contextMaxContextTokensAutoCheck")
        ctx_auto.setChecked(b.context.max_context_tokens is None)
        ctx_max.setEnabled(not ctx_auto.isChecked())

        def _on_ctx_auto(on: bool) -> None:
            ctx_max.setEnabled(not on)
            self._on_behavior_nested(
                "context",
                "max_context_tokens",
                None if on else ctx_max.value(),
            )

        ctx_auto.toggled.connect(_on_ctx_auto)
        ctx_max.editingFinished.connect(
            lambda: None if ctx_auto.isChecked() else self._on_behavior_nested(
                "context",
                "max_context_tokens",
                ctx_max.value(),
            )
        )
        context_form.addRow(QLabel("工作上下文"), _unit_spinbox_with_checkbox(ctx_max, "token", ctx_auto))
        context_form.addRow(
            QLabel(""),
            self._token_budget_hint("这是每轮装入历史、记忆和工具上下文的工作预算，不是模型硬上限。"),
        )

        ctx_reserve = QSpinBox()
        ctx_reserve.setRange(1024, 500_000)
        ctx_reserve.setValue(b.context.reserve_output_tokens)
        ctx_reserve.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "reserve_output_tokens", ctx_reserve.value())
        )
        context_form.addRow(QLabel("输出预留"), unit_spinbox(ctx_reserve, "token"))

        ctx_mem = QSpinBox()
        ctx_mem.setRange(256, 100_000)
        ctx_mem.setValue(b.context.memory_token_budget)
        ctx_mem.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "memory_token_budget", ctx_mem.value())
        )
        context_form.addRow(QLabel("长期记忆预算"), unit_spinbox(ctx_mem, "token"))

        ctx_sum = QSpinBox()
        ctx_sum.setRange(256, 100_000)
        ctx_sum.setValue(b.context.summary_token_budget)
        ctx_sum.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "summary_token_budget", ctx_sum.value())
        )
        context_form.addRow(QLabel("滚动摘要预算"), unit_spinbox(ctx_sum, "token"))

        default_inline = QSpinBox()
        default_inline.setRange(256, 100_000)
        default_inline.setValue(b.context.tool_result_default_budget_tokens)
        default_inline.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "context",
                "tool_result_default_budget_tokens",
                default_inline.value(),
            )
        )
        context_form.addRow(QLabel("默认 inline 预算"), unit_spinbox(default_inline, "token"))

        default_hard = QSpinBox()
        default_hard.setRange(512, 200_000)
        default_hard.setValue(b.context.tool_result_default_hard_cap_tokens)
        default_hard.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "context",
                "tool_result_default_hard_cap_tokens",
                default_hard.value(),
            )
        )
        context_form.addRow(QLabel("默认硬截断上限"), unit_spinbox(default_hard, "token"))

        context_section.add_layout(context_form)
        card.add_content(context_section)

        summary_section = CollapsibleSection(
            "滚动摘要压缩",
            "按工作上下文百分比触发压缩，不再在界面暴露旧版显式 token 阈值。",
            expanded=False,
        )
        summary_form = QFormLayout()
        summary_form.setSpacing(Spacing.SM)

        trigger_pct = QSpinBox()
        trigger_pct.setObjectName("summarizeTriggerContextPercentSpin")
        trigger_pct.setRange(50, 100)
        trigger_pct.setValue(b.summarize.trigger_at_context_percent)
        trigger_pct.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "summarize",
                "trigger_at_context_percent",
                trigger_pct.value(),
            )
        )
        summary_form.addRow(QLabel("触发压缩"), unit_spinbox(trigger_pct, "%"))

        target_pct = QSpinBox()
        target_pct.setObjectName("summarizeTargetContextPercentSpin")
        target_pct.setRange(50, 100)
        target_pct.setValue(b.summarize.target_after_context_percent)
        target_pct.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "summarize",
                "target_after_context_percent",
                target_pct.value(),
            )
        )
        summary_form.addRow(QLabel("压缩后目标"), unit_spinbox(target_pct, "%"))
        summary_section.add_layout(summary_form)
        card.add_content(summary_section)

        advanced_context = CollapsibleSection(
            "上下文窗口高级",
            "控制提示词开销估算、活跃历史保底和运行时记录保留数量。",
            expanded=False,
        )
        advanced_form = QFormLayout()
        advanced_form.setSpacing(Spacing.SM)
        rec = b.context.recommended_context_budget
        for pattern, budget in rec.model_name_budget_tokens.items():
            spin = QSpinBox()
            spin.setObjectName(
                "contextRecommendedModelBudget"
                + "".join(ch if ch.isalnum() else "_" for ch in pattern)
                + "Spin"
            )
            spin.setRange(1024, 4_000_000)
            spin.setSingleStep(1024)
            spin.setValue(budget)
            spin.editingFinished.connect(
                lambda s=spin, p=pattern: self._on_context_model_budget_rule(p, s.value())
            )
            advanced_form.addRow(QLabel(f"模型命中 {pattern}"), unit_spinbox(spin, "token"))

        for index, rule in enumerate(rec.context_length_rules):
            threshold = QSpinBox()
            threshold.setObjectName(f"contextRecommendedLengthRule{index}ThresholdSpin")
            threshold.setRange(1, 4_000_000)
            threshold.setSingleStep(1024)
            threshold.setValue(rule.min_context_length_tokens)
            threshold.editingFinished.connect(
                lambda s=threshold, i=index: self._on_context_length_budget_rule(
                    i,
                    "min_context_length_tokens",
                    s.value(),
                )
            )
            advanced_form.addRow(QLabel(f"context_length 规则 {index + 1} 阈值"), unit_spinbox(threshold, "token"))

            budget = QSpinBox()
            budget.setObjectName(f"contextRecommendedLengthRule{index}BudgetSpin")
            budget.setRange(1024, 4_000_000)
            budget.setSingleStep(1024)
            budget.setValue(rule.budget_tokens)
            budget.editingFinished.connect(
                lambda s=budget, i=index: self._on_context_length_budget_rule(
                    i,
                    "budget_tokens",
                    s.value(),
                )
            )
            advanced_form.addRow(QLabel(f"context_length 规则 {index + 1} 预算"), unit_spinbox(budget, "token"))

        self._add_context_recommendation_spin_row(
            advanced_form,
            "低阈值缩放比例",
            "context_length_scale_percent",
            rec.context_length_scale_percent,
            1,
            100,
            "%",
        )
        self._add_context_recommendation_spin_row(
            advanced_form,
            "低阈值最低预算",
            "min_scaled_budget_tokens",
            rec.min_scaled_budget_tokens,
            1024,
            4_000_000,
            "token",
            step=1024,
        )
        self._add_context_recommendation_spin_row(
            advanced_form,
            "自动预算兜底",
            "fallback_budget_tokens",
            rec.fallback_budget_tokens,
            1024,
            4_000_000,
            "token",
            step=1024,
        )
        self._add_context_spin_row(
            advanced_form,
            "提示词开销估算",
            "prompt_overhead_estimate_tokens",
            b.context.prompt_overhead_estimate_tokens,
            0,
            1_000_000,
            "token",
            step=1024,
        )
        self._add_context_spin_row(
            advanced_form,
            "活跃历史保底",
            "min_working_history_tokens",
            b.context.min_working_history_tokens,
            1024,
            1_000_000,
            "token",
            step=1024,
        )
        self._add_context_spin_row(
            advanced_form,
            "当前会话保底记录",
            "current_conversation_min_records",
            b.context.current_conversation_min_records,
            0,
            1000,
            "条",
        )
        self._add_context_spin_row(
            advanced_form,
            "运行时记录保留",
            "runtime_record_keep_count",
            b.context.runtime_record_keep_count,
            0,
            1000,
            "条",
        )
        self._add_context_spin_row(
            advanced_form,
            "发送回执保留",
            "send_receipt_keep_count",
            b.context.send_receipt_keep_count,
            0,
            1000,
            "条",
        )
        self._add_context_spin_row(
            advanced_form,
            "no_action 记录保留",
            "no_action_keep_count",
            b.context.no_action_keep_count,
            0,
            1000,
            "条",
        )
        advanced_context.add_layout(advanced_form)
        card.add_content(advanced_context)

        tool_section = CollapsibleSection(
            "按工具结果预算",
            "每个工具单独控制 inline / artifact / hard 上限。建议只在确认工具输出不够用时调整。",
            expanded=False,
        )
        tool_hint = QLabel(
            "inline 是直接回传给模型的预算；artifact 是资料型工具改写文件的阈值；"
            "hard 是事故兜底上限。留空 artifact/hard 表示按 inline 或默认硬上限处理。"
        )
        tool_hint.setProperty("role", "secondary")
        tool_hint.setWordWrap(True)
        tool_section.add_content(tool_hint)

        defaults = default_tool_result_budgets()
        budgets = b.context.tool_result_budgets
        grouped_tools = {
            "消息动作": [
                "send_private_messages",
                "send_group_message",
                "send_voice_message",
                "upload_file",
                "recall_message",
                "set_friend_add_request",
                "set_group_add_request",
                "no_action",
                "schedule_wakeup",
            ],
            "查询工具": ["list_contacts", "get_user_info", "get_weather", "web_search"],
            "资料工具": [
                "describe_image",
                "read_file",
                "run_python",
                "get_forward_msg",
                "recall_history",
                "get_recent_chat_messages",
            ],
            "子 Agent": ["start_agent_task", "summarize_chat_history", "summarize_conversation"],
        }
        for group_name, tool_names in grouped_tools.items():
            group_section = CollapsibleSection(
                group_name,
                _tool_budget_group_hint(group_name),
                expanded=False,
            )
            for tool_name in tool_names:
                if tool_name not in defaults:
                    continue
                budget = budgets.get(tool_name) or defaults[tool_name]
                if tool_name not in budgets:
                    budgets[tool_name] = budget
                group_section.add_content(self._build_tool_budget_row(tool_name, budget))
            tool_section.add_content(group_section)
        card.add_content(tool_section)

        legacy = _format_tool_result_overrides(b.context.tool_result_soft_overrides)
        if legacy:
            legacy_lbl = QLabel(f"检测到旧版工具软阈值覆盖：{legacy}。当前页面不再编辑旧字段。")
            legacy_lbl.setProperty("role", "warning")
            legacy_lbl.setWordWrap(True)
            card.add_content(legacy_lbl)

        return card

    def _build_tool_budget_row(
        self,
        tool_name: str,
        budget: ToolResultBudgetConfig,
    ) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(Spacing.SM)

        name = QLabel(tool_name)
        name.setMinimumWidth(190)
        lay.addWidget(name)

        inline = QSpinBox()
        inline.setRange(256, 100_000)
        inline.setValue(budget.inline_budget_tokens)
        inline.editingFinished.connect(
            lambda: self._on_tool_result_budget_field(
                tool_name,
                "inline_budget_tokens",
                inline.value(),
            )
        )
        lay.addWidget(QLabel("inline"))
        lay.addWidget(unit_spinbox(inline, "token", spin_min_width=96, add_stretch=False))

        artifact = QSpinBox()
        artifact.setRange(256, 100_000)
        artifact.setValue(budget.artifact_threshold_tokens or max(256, budget.inline_budget_tokens))
        artifact_auto = QCheckBox("自动 artifact")
        artifact_auto.setChecked(budget.artifact_threshold_tokens is None)
        artifact.setEnabled(not artifact_auto.isChecked())

        def _on_artifact_auto(on: bool) -> None:
            artifact.setEnabled(not on)
            self._on_tool_result_budget_field(
                tool_name,
                "artifact_threshold_tokens",
                None if on else artifact.value(),
            )

        artifact_auto.toggled.connect(_on_artifact_auto)
        artifact.editingFinished.connect(
            lambda: None if artifact_auto.isChecked() else self._on_tool_result_budget_field(
                tool_name,
                "artifact_threshold_tokens",
                artifact.value(),
            )
        )
        lay.addWidget(QLabel("artifact"))
        lay.addWidget(unit_spinbox(artifact, "token", spin_min_width=96, add_stretch=False))
        lay.addWidget(artifact_auto)

        hard = QSpinBox()
        hard.setObjectName(
            "toolBudget"
            + "".join(ch if ch.isalnum() else "_" for ch in tool_name)
            + "HardCapSpin"
        )
        hard.setRange(512, 200_000)
        hard.setValue(
            budget.hard_cap_tokens
            or self._cfg().behavior.context.tool_result_default_hard_cap_tokens
        )
        hard_default = QCheckBox("默认 hard")
        hard_default.setChecked(budget.hard_cap_tokens is None)
        hard.setEnabled(not hard_default.isChecked())

        def _on_hard_default(on: bool) -> None:
            hard.setEnabled(not on)
            self._on_tool_result_budget_field(
                tool_name,
                "hard_cap_tokens",
                None if on else hard.value(),
            )

        hard_default.toggled.connect(_on_hard_default)
        hard.editingFinished.connect(
            lambda: None if hard_default.isChecked() else self._on_tool_result_budget_field(
                tool_name,
                "hard_cap_tokens",
                hard.value(),
            )
        )
        lay.addWidget(QLabel("hard"))
        lay.addWidget(unit_spinbox(hard, "token", spin_min_width=96, add_stretch=False))
        lay.addWidget(hard_default)

        lay.addStretch(1)
        return row

    def _add_context_spin_row(
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
        spin.setObjectName(f"context_{field}_spin")
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.editingFinished.connect(
            lambda s=spin, f=field: self._on_behavior_nested("context", f, s.value())
        )
        form.addRow(QLabel(label), unit_spinbox(spin, unit))

    def _add_context_recommendation_spin_row(
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
        spin.setObjectName(f"context_recommended_{field}_spin")
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.editingFinished.connect(
            lambda s=spin, f=field: self._on_context_budget_recommendation_field(
                f,
                s.value(),
            )
        )
        form.addRow(QLabel(label), unit_spinbox(spin, unit))

    def _settings_recommended_context_budget_preview(self) -> int:
        cfg = self._cfg()
        rec = cfg.behavior.context.recommended_context_budget
        model = (cfg.agents.chat.model or "").lower()
        for pattern, budget in rec.model_name_budget_tokens.items():
            if pattern.lower() in model:
                return budget
        return rec.fallback_budget_tokens

    def _on_context_model_budget_rule(self, pattern: str, value: int) -> None:
        if self._suppress_signals:
            return
        rec = self._cfg().behavior.context.recommended_context_budget
        if rec.model_name_budget_tokens.get(pattern) == value:
            return
        rec.model_name_budget_tokens[pattern] = value
        self._save_now(
            needs_restart=True,
            change_desc=f"behavior.context.recommended_context_budget.model_name_budget_tokens.{pattern}",
        )

    def _on_context_length_budget_rule(self, index: int, field: str, value: int) -> None:
        if self._suppress_signals:
            return
        rec = self._cfg().behavior.context.recommended_context_budget
        if index < 0 or index >= len(rec.context_length_rules):
            return
        rule = rec.context_length_rules[index]
        if getattr(rule, field) == value:
            return
        setattr(rule, field, value)
        self._save_now(
            needs_restart=True,
            change_desc=f"behavior.context.recommended_context_budget.context_length_rules.{index}.{field}",
        )

    def _on_context_budget_recommendation_field(self, field: str, value: int) -> None:
        if self._suppress_signals:
            return
        rec = self._cfg().behavior.context.recommended_context_budget
        if getattr(rec, field) == value:
            return
        setattr(rec, field, value)
        self._save_now(
            needs_restart=True,
            change_desc=f"behavior.context.recommended_context_budget.{field}",
        )

    @staticmethod
    def _token_budget_hint(text: str) -> QLabel:
        hint = QLabel(text)
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)
        return hint
