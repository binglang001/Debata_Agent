"""设置页通用配置保存测试。"""

# ruff: noqa: E402

from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from app_config.loader import load_config
from app_config.schema import AgentConfig
from ui.dashboard.settings_page import SettingsPage

from .helpers import _minimal_root_config


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

    saved = load_config(tmp_paths, set_global=False)
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

    saved = load_config(tmp_paths, set_global=False)
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

    saved = load_config(tmp_paths, set_global=False)
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
        assert "社交决策预算" in labels
        assert "主动思考" not in labels
        assert "人格编辑历史" not in labels
        assert "活跃历史保底" not in labels
        assert "当前会话保底记录" not in labels
        assert "运行时记录保留" not in labels
        assert "发送回执保留" not in labels
        assert "no_action 记录保留" not in labels

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
        retry_pct = spin("summarizeRetryTargetContextPercentSpin")
        retry_pct.setValue(25)
        retry_pct.editingFinished.emit()
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
        assert page.findChild(
            QtWidgets.QSpinBox,
            "context_min_working_history_tokens_spin",
        ) is None
        assert page.findChild(
            QtWidgets.QSpinBox,
            "context_current_conversation_min_records_spin",
        ) is None
        assert page.findChild(
            QtWidgets.QSpinBox,
            "context_runtime_record_keep_count_spin",
        ) is None
        assert page.findChild(
            QtWidgets.QSpinBox,
            "context_send_receipt_keep_count_spin",
        ) is None
        assert page.findChild(
            QtWidgets.QSpinBox,
            "context_no_action_keep_count_spin",
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

        saved = load_config(tmp_paths, set_global=False)
        assert saved.behavior.context.max_context_tokens == 4_000_000
        assert saved.behavior.summarize.trigger_at_context_percent == 80
        assert saved.behavior.summarize.target_after_context_percent == 60
        assert saved.behavior.summarize.retry_target_after_context_percent == 25
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
