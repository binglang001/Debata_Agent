"""Behavior/token-budget handlers for SettingsPage.

This module is a mechanical split from ``ui.dashboard.settings_page``. Keep
behavior equivalent; do not change hot-field, save, or tool-budget logic while
moving methods.
"""

from __future__ import annotations

import logging

from app_config.schema import ToolResultBudgetConfig, default_tool_result_budgets

from .helpers import _parse_tool_result_overrides


class SettingsBehaviorMixin:
    def _restore_default_tool_budgets(self) -> None:
        if self._suppress_signals:
            return
        self._cfg().behavior.context.tool_result_budgets = default_tool_result_budgets()
        self._save_now(
            needs_restart=False,
            change_desc="behavior.context.tool_result_budgets restore defaults",
        )
        # 当前页面的 spinbox 不热重建；重新进入设置页会看到推荐值。

    # hot 字段（立即生效，无需重启）
    _HOT_FIELDS = {
        "chars_per_second", "max_delay_seconds",
        "window_seconds", "max_messages", "enabled",
        "trigger_at_messages", "range_start_messages", "range_end_messages",
        "trigger_at_tokens", "target_after_tokens",
        "max_context_tokens", "reserve_output_tokens", "memory_token_budget", "summary_token_budget",
        "tool_result_default_budget_tokens", "tool_result_default_hard_cap_tokens",
        "tool_result_budgets", "tool_result_soft_limit_tokens", "tool_result_hard_cap_tokens",
        "tool_result_soft_overrides",
        "default_history_fetch_count",
        "proactive_context_token_budget",
    }

    def _on_behavior_field(self, field: str, value) -> None:
        if self._suppress_signals:
            return
        obj = self._cfg().behavior
        if getattr(obj, field) == value:
            return
        setattr(obj, field, value)
        needs = field not in self._HOT_FIELDS
        self._save_now(needs_restart=needs, change_desc=f"behavior.{field}")

    def _on_behavior_nested(self, section: str, field: str, value) -> None:
        if self._suppress_signals:
            return
        obj = getattr(self._cfg().behavior, section)
        if getattr(obj, field) == value:
            return
        setattr(obj, field, value)
        needs = field not in self._HOT_FIELDS
        self._save_now(needs_restart=needs, change_desc=f"behavior.{section}.{field}")

    def _on_tool_result_overrides(self, text: str) -> None:
        try:
            value = _parse_tool_result_overrides(text)
        except ValueError as e:
            self._status.mark_error(str(e))
            return
        self._on_behavior_nested("context", "tool_result_soft_overrides", value)

    def _on_tool_result_budget_field(
        self,
        tool_name: str,
        field: str,
        value: int | None,
    ) -> None:
        if self._suppress_signals:
            return
        budgets = self._cfg().behavior.context.tool_result_budgets
        budget = budgets.get(tool_name)
        if budget is None:
            budget = default_tool_result_budgets().get(tool_name) or ToolResultBudgetConfig()
            budgets[tool_name] = budget
        if getattr(budget, field) == value:
            return
        setattr(budget, field, value)
        self._save_now(
            needs_restart=False,
            change_desc=f"behavior.context.tool_result_budgets.{tool_name}.{field}",
        )

    def _on_log_level_changed(self, level: str) -> None:
        if self._suppress_signals:
            return
        self._cfg().app.log_level = level
        # 立即应用到 root logger
        logging.getLogger().setLevel(level)
        self._save_now(needs_restart=False, change_desc=f"app.log_level={level} (hot)")
