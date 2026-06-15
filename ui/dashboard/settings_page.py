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
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from ..widgets import AutoSizeStack
from ..widgets.wheel_freeze import install_wheel_freeze
from ..wizard.components import SectionCard
from .settings import (
    CollapsibleSection as CollapsibleSection,
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
    _SaveStatusBar,
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
from .settings.model import SettingsModelMixin
from .settings.persona_appearance import SettingsPersonaAppearanceMixin
from .settings.persona_physiology import SettingsPersonaPhysiologyMixin
from .settings.software import SettingsSoftwareMixin
from .settings.state import SettingsStateMixin
from .settings.token_budget import SettingsTokenBudgetMixin

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
    SettingsPersonaPhysiologyMixin,
    SettingsSoftwareMixin,
    SettingsStateMixin,
    SettingsTokenBudgetMixin,
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
        self._add_settings_page(
            "persona_physiology",
            "人格与生理",
            self._build_persona_physiology_section(),
        )

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

    def on_shown(self) -> None:
        if not self._provider_status_timer.isActive():
            self._provider_status_timer.start()
        self.refresh()

    def on_hidden(self) -> None:
        self._provider_status_timer.stop()
        self._settings_content_sync_timer.stop()

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

        content_height = stack.sizeHint().height()
        current = stack.currentWidget()
        content_width = scroll.viewport().width()
        if current.hasHeightForWidth() and content_width > 0:
            width_height = current.heightForWidth(content_width)
            if width_height > 0:
                content_height = max(width_height, current.minimumSizeHint().height())

        target_height = max(viewport_height, content_height)
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
            subtitle="重要记忆始终启用；这里配置是否额外启用 RAG 历史召回增强，改动后重启生效。",
        )
        card.add_content(self._build_longterm_memory_card())
        card.add_content(self._build_embedding_card())
        return card

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
            self._refresh_persona_physiology_controls()
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

