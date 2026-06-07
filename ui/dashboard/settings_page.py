"""设置页 —— 全字段可改 + 即时保存 + 重启提示。

左侧导航按模型、功能、渠道、记忆、软件行为、Token预算和日志诊断分区。
每个字段改动立即写入磁盘；hot 字段（白名单 / log 级别 / 主题）立即生效；
其它字段标记 needs_restart，底部状态条提示用户重启 Debata 服务。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_config.schema import ToolResultBudgetConfig, default_tool_result_budgets

from ..theme import Spacing
from ..widgets import AutoSizeStack
from ..widgets.wheel_freeze import install_wheel_freeze
from ..wizard.components import SectionCard
from .settings import (
    CollapsibleSection,
    _SaveStatusBar,
)
from .settings import (
    _AddProviderDialog as _AddProviderDialog,
)
from .settings import (
    _ASREditDialog as _ASREditDialog,
)
from .settings import (
    _EmbeddingEditDialog as _EmbeddingEditDialog,
)
from .settings import (
    _load_provider_presets_for_dialog as _load_provider_presets_for_dialog,
)
from .settings import (
    _TTSEditDialog as _TTSEditDialog,
)
from .settings import (
    _VisionEditDialog as _VisionEditDialog,
)
from .settings import (
    _WeatherEditDialog as _WeatherEditDialog,
)
from .settings.adapter import SettingsAdapterMixin
from .settings.behavior import SettingsBehaviorMixin
from .settings.features import SettingsFeaturesMixin
from .settings.helpers import (
    _format_tool_result_overrides,
    _tool_budget_group_hint,
)
from .settings.model import SettingsModelMixin
from .settings.persona_appearance import SettingsPersonaAppearanceMixin
from .settings.state import SettingsStateMixin

logger = logging.getLogger(__name__)


# ============================================================
# 主 SettingsPage
# ============================================================


class SettingsPage(
    SettingsAdapterMixin,
    SettingsBehaviorMixin,
    SettingsFeaturesMixin,
    SettingsModelMixin,
    SettingsPersonaAppearanceMixin,
    SettingsStateMixin,
    QWidget,
):
    """设置页。每字段即时保存；改完按需重启。"""

    theme_changed = Signal(str)  # "auto" / "light" / "dark"
    restart_runtime_requested = Signal()  # main.py 接此请求做 runtime hot restart

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._agent_provider_combos: list[QComboBox] = []
        self._provider_status_labels: dict[str, QLabel] = {}
        self._settings_content_sync_timer = QTimer(self)
        self._settings_content_sync_timer.setSingleShot(True)
        self._settings_content_sync_timer.setInterval(0)
        self._settings_content_sync_timer.timeout.connect(self._sync_settings_content_height)
        self._settings_layout_watch: list[QWidget] = []
        self._suppress_signals = False
        # 基线配置快照（深拷贝），用于比对改动项数
        self._baseline = deepcopy(self._cfg())
        self._opened_snapshot = deepcopy(self._cfg())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        main_row = QHBoxLayout()
        main_row.setContentsMargins(0, 0, 0, 0)
        main_row.setSpacing(Spacing.MD)

        self._settings_nav = QListWidget()
        self._settings_nav.setObjectName("SettingsNav")
        self._settings_nav.setFixedWidth(168)
        self._settings_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        main_row.addWidget(self._settings_nav)

        self._settings_stack = AutoSizeStack()
        self._settings_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._settings_scroll = QScrollArea()
        self._settings_scroll.setObjectName("SettingsContentScroll")
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._settings_scroll.viewport().installEventFilter(self)
        self._settings_scroll.setWidget(self._settings_stack)
        main_row.addWidget(self._settings_scroll, 1)

        outer.addLayout(main_row, 1)

        self._add_settings_page("models", "模型", self._build_model_section())
        self._add_settings_page("features", "功能", self._build_features_section())

        self._adapter_container = QVBoxLayout()
        self._adapter_container.setContentsMargins(0, 0, 0, 0)
        self._adapter_container.setSpacing(Spacing.MD)
        adapter_wrap = QWidget()
        adapter_wrap.setLayout(self._adapter_container)
        self._add_settings_page("adapter", "渠道", adapter_wrap)

        self._add_settings_page("memory", "记忆", self._build_memory_section())
        self._add_settings_page("behavior", "软件行为", self._build_software_behavior_section())
        self._add_settings_page("token_budget", "Token预算", self._build_token_budget_section())
        self._add_settings_page("diagnostics", "日志与诊断", self._build_diagnostics_section())

        self._settings_nav.currentRowChanged.connect(self._on_settings_section_changed)
        self._settings_nav.setCurrentRow(0)

        # 底部状态条（始终可见，不滚动）
        self._status = _SaveStatusBar()
        self._status.restart_requested.connect(self._on_restart_clicked)
        self._status.restore_requested.connect(self._restore_opened_config)
        outer.addWidget(self._status)

        # 初始化 adapter 表单
        self._rebuild_adapter_form()

        # 滚轮冻结
        self._wheel_freeze_filter = install_wheel_freeze(self)

        self._provider_status_timer = QTimer(self)
        self._provider_status_timer.setInterval(1000)
        self._provider_status_timer.timeout.connect(self._refresh_provider_status_labels)
        self._provider_status_timer.start()

    def _add_settings_page(self, key: str, title: str, content: QWidget) -> None:
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole, key)
        self._settings_nav.addItem(item)

        page = QWidget()
        page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(Spacing.MD)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(content, 0, Qt.AlignmentFlag.AlignTop)
        page.installEventFilter(self)
        content.installEventFilter(self)
        self._settings_layout_watch.extend([page, content])
        self._settings_stack.addWidget(page)

    def _on_settings_section_changed(self, row: int) -> None:
        if row < 0:
            return
        self._settings_stack.setCurrentIndex(row)
        self._settings_stack.sync_current_size()
        self._schedule_settings_content_sync()
        self._settings_scroll.verticalScrollBar().setValue(0)

    def eventFilter(self, obj: object, event: QEvent) -> bool:  # noqa: N802
        scroll = getattr(self, "_settings_scroll", None)
        if scroll is not None and obj is scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._schedule_settings_content_sync()
        elif (
            obj in getattr(self, "_settings_layout_watch", [])
            and event.type() == QEvent.Type.LayoutRequest
        ):
            self._schedule_settings_content_sync()
        return super().eventFilter(obj, event)

    def _schedule_settings_content_sync(self) -> None:
        if self._settings_content_sync_timer.isActive():
            return
        self._settings_content_sync_timer.start()

    def _sync_settings_content_height(self) -> None:
        stack = getattr(self, "_settings_stack", None)
        scroll = getattr(self, "_settings_scroll", None)
        if stack is None or scroll is None or stack.currentWidget() is None:
            return
        viewport_height = scroll.viewport().height()
        if viewport_height <= 0:
            return

        target_height = max(viewport_height, stack.sizeHint().height())
        if stack.minimumHeight() != target_height or stack.maximumHeight() != target_height:
            stack.setFixedHeight(target_height)
            stack.updateGeometry()
            widget = scroll.widget()
            if widget is not None:
                widget.updateGeometry()

    # ============================================================
    # 公共辅助
    # ============================================================

    def _build_memory_section(self) -> SectionCard:
        card = SectionCard(
            title="记忆方式",
            subtitle="长期记忆模式与 RAG embedding 配置集中在这里，改动后重启生效。",
        )
        card.add_content(self._build_longterm_memory_card())
        card.add_content(self._build_embedding_card())
        return card

    def _build_software_behavior_section(self) -> SectionCard:
        card = SectionCard(
            title="软件行为",
            subtitle="界面主题、消息节奏、主动思考和陌生人限速。常用项在上方，高风险项保持收起前的简洁说明。",
        )
        card.add_content(self._build_appearance_section())

        b = self._cfg().behavior
        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        merge_spin = QDoubleSpinBox()
        merge_spin.setRange(0.0, 60.0)
        merge_spin.setSingleStep(0.5)
        merge_spin.setValue(b.merge_window_seconds)
        merge_spin.setSuffix(" 秒")
        merge_spin.setToolTip("同一窗口内收到的连续消息会合并成一次模型调用。")
        merge_spin.editingFinished.connect(
            lambda: self._on_behavior_field("merge_window_seconds", merge_spin.value())
        )
        form.addRow(QLabel("消息合并窗口"), merge_spin)

        recall_spin = QDoubleSpinBox()
        recall_spin.setRange(0.0, 60.0)
        recall_spin.setSingleStep(0.5)
        recall_spin.setValue(b.recall_merge_window_seconds)
        recall_spin.setSuffix(" 秒")
        recall_spin.setToolTip("撤回事件在该时间内合并处理，避免频繁打断。")
        recall_spin.editingFinished.connect(
            lambda: self._on_behavior_field("recall_merge_window_seconds", recall_spin.value())
        )
        form.addRow(QLabel("撤回合并窗口"), recall_spin)

        hist_spin = QSpinBox()
        hist_spin.setRange(1, 1000)
        hist_spin.setValue(b.default_history_fetch_count)
        hist_spin.setToolTip("总结工具默认拉取的历史条数。")
        hist_spin.editingFinished.connect(
            lambda: self._on_behavior_field("default_history_fetch_count", hist_spin.value())
        )
        form.addRow(QLabel("默认拉历史条数"), hist_spin)

        chars_spin = QDoubleSpinBox()
        chars_spin.setRange(0.1, 50.0)
        chars_spin.setSingleStep(0.5)
        chars_spin.setValue(b.typing.chars_per_second)
        chars_spin.setToolTip("影响分条发送时模拟打字的等待时间。")
        chars_spin.editingFinished.connect(
            lambda: self._on_behavior_nested("typing", "chars_per_second", chars_spin.value())
        )
        form.addRow(QLabel("打字速度（字/秒）"), chars_spin)

        card.add_layout(form)

        proactive_section = CollapsibleSection(
            "主动思考",
            "后台定时判断是否需要主动开口。频率越高，成本越高。",
            expanded=False,
        )
        proactive_hint = QLabel("主动思考会定时判断是否需要主动开口。频率越高，成本越高。")
        proactive_hint.setProperty("role", "secondary")
        proactive_hint.setWordWrap(True)
        proactive_section.add_content(proactive_hint)

        proactive_form = QFormLayout()
        proactive_form.setSpacing(Spacing.SM)
        proactive_spin = QDoubleSpinBox()
        proactive_spin.setRange(10.0, 86400.0)
        proactive_spin.setSingleStep(60.0)
        proactive_spin.setValue(b.proactive_think_interval_seconds)
        proactive_spin.setSuffix(" 秒")
        proactive_spin.editingFinished.connect(
            lambda: self._on_behavior_field("proactive_think_interval_seconds", proactive_spin.value())
        )
        proactive_form.addRow(QLabel("主动思考间隔"), proactive_spin)

        proactive_budget = QSpinBox()
        proactive_budget.setRange(1024, 65536)
        proactive_budget.setSingleStep(1024)
        proactive_budget.setValue(b.proactive_context_token_budget)
        proactive_budget.setSuffix(" token")
        proactive_budget.setToolTip("主动思考路由器读取近期上下文和记忆的预算。默认 4K。")
        proactive_budget.editingFinished.connect(
            lambda: self._on_behavior_field("proactive_context_token_budget", proactive_budget.value())
        )
        proactive_form.addRow(QLabel("主动思考上下文"), proactive_budget)
        proactive_section.add_layout(proactive_form)
        card.add_content(proactive_section)

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
        rl_window.setSuffix(" 秒")
        rl_window.editingFinished.connect(lambda: self._on_behavior_nested("rate_limit", "window_seconds", rl_window.value()))
        rate_form.addRow(QLabel("窗口"), rl_window)

        rl_max = QSpinBox()
        rl_max.setRange(1, 1000)
        rl_max.setValue(b.rate_limit.max_messages)
        rl_max.setSuffix(" 条")
        rl_max.editingFinished.connect(lambda: self._on_behavior_nested("rate_limit", "max_messages", rl_max.value()))
        rate_form.addRow(QLabel("最多条数"), rl_max)
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
        ctx_max.setRange(0, 1_000_000)
        ctx_max.setValue(b.context.max_context_tokens or 0)
        ctx_max.setSuffix(" token")
        ctx_max.setSpecialValueText("按模型自动")
        ctx_max.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "max_context_tokens", ctx_max.value() or None)
        )
        context_form.addRow(QLabel("工作上下文"), ctx_max)

        ctx_reserve = QSpinBox()
        ctx_reserve.setRange(1024, 500_000)
        ctx_reserve.setValue(b.context.reserve_output_tokens)
        ctx_reserve.setSuffix(" token")
        ctx_reserve.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "reserve_output_tokens", ctx_reserve.value())
        )
        context_form.addRow(QLabel("输出预留"), ctx_reserve)

        ctx_mem = QSpinBox()
        ctx_mem.setRange(256, 100_000)
        ctx_mem.setValue(b.context.memory_token_budget)
        ctx_mem.setSuffix(" token")
        ctx_mem.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "memory_token_budget", ctx_mem.value())
        )
        context_form.addRow(QLabel("长期记忆预算"), ctx_mem)

        ctx_sum = QSpinBox()
        ctx_sum.setRange(256, 100_000)
        ctx_sum.setValue(b.context.summary_token_budget)
        ctx_sum.setSuffix(" token")
        ctx_sum.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "summary_token_budget", ctx_sum.value())
        )
        context_form.addRow(QLabel("滚动摘要预算"), ctx_sum)

        default_inline = QSpinBox()
        default_inline.setRange(256, 100_000)
        default_inline.setValue(b.context.tool_result_default_budget_tokens)
        default_inline.setSuffix(" token")
        default_inline.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "context",
                "tool_result_default_budget_tokens",
                default_inline.value(),
            )
        )
        context_form.addRow(QLabel("默认 inline 预算"), default_inline)

        default_hard = QSpinBox()
        default_hard.setRange(512, 200_000)
        default_hard.setValue(b.context.tool_result_default_hard_cap_tokens)
        default_hard.setSuffix(" token")
        default_hard.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "context",
                "tool_result_default_hard_cap_tokens",
                default_hard.value(),
            )
        )
        context_form.addRow(QLabel("默认硬截断上限"), default_hard)

        context_section.add_layout(context_form)
        card.add_content(context_section)

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
        inline.setSuffix(" inline")
        inline.editingFinished.connect(
            lambda: self._on_tool_result_budget_field(
                tool_name,
                "inline_budget_tokens",
                inline.value(),
            )
        )
        lay.addWidget(inline)

        artifact = QSpinBox()
        artifact.setRange(0, 100_000)
        artifact.setValue(budget.artifact_threshold_tokens or 0)
        artifact.setSpecialValueText("自动 artifact")
        artifact.editingFinished.connect(
            lambda: self._on_tool_result_budget_field(
                tool_name,
                "artifact_threshold_tokens",
                artifact.value() or None,
            )
        )
        lay.addWidget(artifact)

        hard = QSpinBox()
        hard.setRange(0, 200_000)
        hard.setValue(budget.hard_cap_tokens or 0)
        hard.setSpecialValueText("默认 hard")
        hard.editingFinished.connect(
            lambda: self._on_tool_result_budget_field(
                tool_name,
                "hard_cap_tokens",
                hard.value() or None,
            )
        )
        lay.addWidget(hard)

        lay.addStretch(1)
        return row

    # ============================================================
    # 外部
    # ============================================================

    def refresh(self) -> None:
        """从 config 重新同步 features 节所有复选框和 summary 文本。"""
        self._suppress_signals = True
        try:
            f = self._cfg().features
            if hasattr(self, "_asr_chk"):
                self._asr_chk.setChecked(f.asr.enabled)
                self._asr_summary_lbl.setText(self._asr_summary())
            if hasattr(self, "_tts_chk"):
                self._tts_chk.setChecked(f.tts.enabled)
                self._tts_summary_lbl.setText(self._tts_summary())
            if hasattr(self, "_vision_chk"):
                self._vision_chk.setChecked(f.vision.enabled)
                self._vision_summary_lbl.setText(self._vision_summary())
            if hasattr(self, "_weather_chk"):
                self._weather_chk.setChecked(f.weather.enabled)
                self._weather_summary_lbl.setText(self._weather_summary())
            if hasattr(self, "_ws_chk"):
                self._ws_chk.setChecked(f.web_search.enabled)
            if hasattr(self, "_emb_summary_lbl"):
                self._emb_summary_lbl.setText(self._embedding_summary())
            self._refresh_provider_status_labels()
            # 主题单选按钮同步
            if hasattr(self, "_theme_group"):
                target = self._cfg().app.theme
                self._current_theme = target
                for rb in self._theme_group.buttons():
                    if rb.property("theme_value") == target:
                        rb.setChecked(True)
                        break
        finally:
            self._suppress_signals = False
        self._schedule_settings_content_sync()

