"""设置页 —— 全字段可改 + 即时保存 + 重启提示。

左侧导航按模型、功能、渠道、记忆、软件行为、Token预算和日志诊断分区。
每个字段改动立即写入磁盘；hot 字段（白名单 / log 级别 / 主题）立即生效；
其它字段标记 needs_restart，底部状态条提示用户重启 Debata 服务。
"""

from __future__ import annotations

import asyncio
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
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_config.schema import (
    AgentConfig,
    ProviderConfig,
    ReasoningConfig,
    ToolResultBudgetConfig,
    default_tool_result_budgets,
)

from ..theme import Spacing
from ..widgets import AutoSizeStack, show_message
from ..widgets.model_combo import ModelComboBox
from ..widgets.wheel_freeze import install_wheel_freeze
from ..wizard.components import SectionCard
from .copy import DASHBOARD_COPY
from .settings import (
    CollapsibleSection,
    _AddProviderDialog,
    _load_provider_presets_for_dialog,
    _SaveStatusBar,
)
from .settings import (
    _ASREditDialog as _ASREditDialog,
)
from .settings import (
    _EmbeddingEditDialog as _EmbeddingEditDialog,
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
    _progress_slot,
    _tool_budget_group_hint,
)
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

    # ============================================================
    # 模型节：providers + 添加 + agents（provider 下拉）
    # ============================================================

    def _build_model_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_model"],
            subtitle="改 provider / Agent 模型 / 思考。修改即保存，需重启生效项见底部按钮。",
        )

        # 提供商小标题 + 添加按钮
        head = QHBoxLayout()
        p_title = QLabel("提供商")
        p_title.setProperty("role", "title-3")
        head.addWidget(p_title)
        head.addStretch(1)
        add_btn = QPushButton("+ 添加提供商")
        add_btn.setProperty("role", "secondary")
        add_btn.clicked.connect(self._on_add_provider)
        head.addWidget(add_btn)
        head_wrap = QWidget()
        head_wrap.setLayout(head)
        card.add_content(head_wrap)

        # 提供商列表容器（可重建）
        self._providers_container = QVBoxLayout()
        self._providers_container.setSpacing(Spacing.SM)
        wrap = QWidget()
        wrap.setLayout(self._providers_container)
        card.add_content(wrap)
        self._render_providers()

        # 分隔
        sep = QFrame()
        sep.setProperty("role", "separator")
        card.add_content(sep)

        # Agents
        a_title = QLabel("Agent 模型")
        a_title.setProperty("role", "title-3")
        card.add_content(a_title)

        a_hint = QLabel("每个 Agent 单独绑 provider + 模型 + 思考。Provider 下拉框选已添加的项目。")
        a_hint.setProperty("role", "secondary")
        a_hint.setWordWrap(True)
        card.add_content(a_hint)

        for agent_name in ("chat", "proactive", "summary"):
            agent_cfg = getattr(self._cfg().agents, agent_name)
            if agent_cfg is None:
                continue
            card.add_content(self._build_agent_row(agent_name, agent_cfg))
        return card

    def _render_providers(self) -> None:
        self._provider_status_labels.clear()
        # 清空旧 widget
        while self._providers_container.count():
            item = self._providers_container.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for name, p in (self._cfg().providers or {}).items():
            self._providers_container.addWidget(self._build_provider_row(name, p))
        # 同步所有 agent 的 provider 下拉
        self._refresh_agent_provider_combos()

    def _refresh_agent_provider_combos(self) -> None:
        provider_ids = list(self._cfg().providers.keys())
        for cmb in self._agent_provider_combos:
            current = cmb.currentData()
            self._suppress_signals = True
            cmb.clear()
            for pid in provider_ids:
                cmb.addItem(pid, pid)
            idx = cmb.findData(current)
            if idx >= 0:
                cmb.setCurrentIndex(idx)
            self._suppress_signals = False

    def _on_add_provider(self) -> None:
        existing = set(self._cfg().providers.keys())
        presets = _load_provider_presets_for_dialog(
            self._runtime.paths.PROVIDER_PRESETS_DIR
        )
        dlg = _AddProviderDialog(existing, presets, self)
        if dlg.exec() and dlg.result_data:
            data = dlg.result_data
            sid = f"{data['id']}_key"
            self._set_secret(sid, data["api_key"])
            is_custom = data["preset"] == "custom"
            new_p = ProviderConfig(
                preset=None if is_custom else data["preset"],
                display_name=data["display_name"],
                protocol="openai_compat" if is_custom else None,
                base_url=(data["base_url"] or None) if is_custom else None,
                api_key_id=sid,
            )
            self._cfg().providers[data["id"]] = new_p
            self._save_now(needs_restart=True, change_desc=f"添加 provider {data['id']}")
            self._render_providers()

    def _build_provider_row(self, name: str, p) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.XS)

        head = QHBoxLayout()
        id_lbl = QLabel(f"[{name}]")
        id_lbl.setProperty("role", "title-3")
        head.addWidget(id_lbl)
        kind_lbl = QLabel(f"preset: {p.preset or '自定义'}")
        kind_lbl.setProperty("role", "caption")
        head.addWidget(kind_lbl)
        head.addStretch(1)
        # 删除按钮（不能删除最后一个 / 不能删被引用的）
        del_btn = QPushButton("删除")
        del_btn.setProperty("role", "text")
        del_btn.clicked.connect(lambda *_, n=name: self._on_delete_provider(n))
        head.addWidget(del_btn)
        outer.addLayout(head)

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        # 显示名
        dname_edit = QLineEdit(p.display_name or "")
        dname_edit.setPlaceholderText("如 DeepSeek")
        dname_edit.editingFinished.connect(
            lambda *_, n=name, e=dname_edit: self._on_provider_dname_changed(n, e.text().strip())
        )
        form.addRow(QLabel("显示名"), dname_edit)

        # base_url（custom 才有）
        if not p.preset or p.preset == "custom" or p.base_url:
            url_edit = QLineEdit(p.base_url or "")
            url_edit.setPlaceholderText("https://api.example.com/v1")
            url_edit.editingFinished.connect(
                lambda *_, n=name, e=url_edit: self._on_provider_baseurl_changed(n, e.text().strip())
            )
            form.addRow(QLabel("Base URL"), url_edit)

        # 密钥替换
        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText(
            f"留空 = 保留现有（id={p.api_key_id or '未设'}）；填写则替换并保存"
        )
        key_edit.editingFinished.connect(
            lambda *_, n=name, e=key_edit: self._on_provider_key_changed(n, e)
        )

        show_btn = QPushButton("显示")
        show_btn.setProperty("role", "secondary")
        show_btn.setFixedWidth(72)

        def _toggle_vis(_e=key_edit, _b=show_btn) -> None:
            if _e.echoMode() == QLineEdit.EchoMode.Password:
                _e.setEchoMode(QLineEdit.EchoMode.Normal)
                _b.setText("隐藏")
            else:
                _e.setEchoMode(QLineEdit.EchoMode.Password)
                _b.setText("显示")

        show_btn.clicked.connect(_toggle_vis)
        key_row = QHBoxLayout()
        key_row.setSpacing(Spacing.SM)
        key_row.addWidget(key_edit, 1)
        key_row.addWidget(show_btn)
        test_btn = QPushButton("测试连接")
        test_btn.setProperty("role", "secondary")
        key_row.addWidget(test_btn)
        key_wrap = QWidget()
        key_wrap.setLayout(key_row)
        form.addRow(QLabel("API 密钥"), key_wrap)

        status = QLabel(self._provider_health_text(name))
        status.setProperty("role", "secondary")
        status.setWordWrap(True)
        self._provider_status_labels[name] = status
        form.addRow(QLabel("状态"), status)
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setTextVisible(False)
        progress.setVisible(False)
        form.addRow(QLabel(""), _progress_slot(progress))
        test_btn.clicked.connect(
            lambda *_, n=name, s=status, pbar=progress, btn=test_btn:
            self._on_test_provider(n, s, pbar, btn)
        )

        outer.addLayout(form)
        return wrap

    def _provider_health_text(self, name: str) -> str:
        item = (getattr(self._runtime, "provider_health", {}) or {}).get(name)
        if item is None:
            return "尚未检测"
        if getattr(item, "status", "") == "checking":
            message = getattr(item, "message", "") or ""
            return "启动自动检测中" if message in ("", "检测中") else message
        if getattr(item, "status", "") == "ok":
            latency = getattr(item, "latency_ms", 0)
            return "可用" + (f" · {latency}ms" if latency else "")
        return getattr(item, "message", "无响应")

    def _refresh_provider_status_labels(self) -> None:
        for name, label in list(self._provider_status_labels.items()):
            try:
                text = self._provider_health_text(name)
            except Exception as e:  # noqa: BLE001
                text = f"状态读取失败：{e}"
            if label.text() != text:
                label.setText(text)

    def _agent_model_for_provider(self, provider_name: str) -> str:
        for _agent_name, agent in self._cfg()._iter_agents():
            if agent.provider == provider_name:
                return agent.model
        vision = self._cfg().features.vision
        if (
            vision.type == "api"
            and vision.provider == provider_name
            and vision.model
        ):
            return vision.model
        return ""

    def _embedding_model_for_provider(self, provider_name: str) -> str:
        features = self._cfg().features
        emb = features.embedding
        if (
            features.long_term_memory.mode == "rag"
            and emb.enabled
            and emb.type == "api"
            and emb.provider == provider_name
        ):
            return emb.api_model
        return ""

    def _embedding_api_key_for_provider(self, provider_name: str) -> str:
        emb = self._cfg().features.embedding
        if emb.provider != provider_name:
            return ""
        if emb.api_key_id:
            try:
                return self._runtime.secrets.get(emb.api_key_id) or ""
            except Exception:
                return ""
        provider = self._runtime.providers.get(provider_name)
        return getattr(provider, "api_key", "") or ""

    def _provider_protocol(self, name: str) -> str:
        p = self._cfg().providers.get(name)
        if p is None:
            return "openai_compat"
        if p.protocol:
            return p.protocol
        preset_name = (p.preset or "").lower()
        preset = getattr(self._runtime.provider_registry, "presets", {}).get(preset_name)
        return getattr(preset, "protocol", "openai_compat")

    def _provider_preset_id(self, name: str) -> str:
        p = self._cfg().providers.get(name)
        return (p.preset or "") if p is not None else ""

    def _provider_endpoint_from_config(self, name: str) -> tuple[str, str, str, str]:
        """返回 provider 的 preset/protocol/base_url/api_key，优先使用当前配置。"""
        p = self._cfg().providers.get(name)
        if p is None:
            raise ValueError(f"未知 provider：{name}")
        preset_id = (p.preset or "").lower()
        preset = None
        if preset_id:
            preset = getattr(self._runtime.provider_registry, "presets", {}).get(preset_id)
            if preset is None:
                try:
                    from providers.presets_loader import load_all_presets

                    preset = load_all_presets(self._runtime.paths.PROVIDER_PRESETS_DIR).get(preset_id)
                except Exception:
                    preset = None
        protocol = p.protocol or getattr(preset, "protocol", None) or "openai_compat"
        base_url = p.base_url or getattr(preset, "base_url", "") or ""
        if not base_url:
            raise ValueError(f"provider {name} 缺少 Base URL")
        api_key = ""
        if p.api_key_id:
            try:
                api_key = self._runtime.secrets.get(p.api_key_id) or ""
            except Exception:
                api_key = ""
        if not api_key:
            provider = getattr(self._runtime, "providers", {}).get(name)
            api_key = getattr(provider, "api_key", "") or ""
        if not api_key:
            raise ValueError(f"provider {name} 没有可用 API 密钥")
        return preset_id, protocol, base_url, api_key

    async def _fetch_models_for_provider(self, name: str):
        from providers.model_fetcher import fetch_model_infos
        from providers.registry import normalize_base_url

        preset_id, protocol, base_url, api_key = self._provider_endpoint_from_config(name)
        return await fetch_model_infos(
            normalize_base_url(base_url, protocol),
            api_key,
            protocol,
            provider_id=preset_id,
            timeout=8.0,
        )

    def _on_test_provider(
        self,
        name: str,
        status: QLabel,
        progress: QProgressBar | None = None,
        button: QPushButton | None = None,
    ) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            status.setText("事件循环未就绪")
            return

        status.setText("正在测试……")
        if progress is not None:
            progress.setVisible(True)
            progress.setRange(0, 0)
        if button is not None:
            button.setEnabled(False)
            button.setText("测试中")

        async def _do_test() -> None:
            try:
                provider = self._runtime.providers.get(name)
                model = self._agent_model_for_provider(name)
                if provider is None:
                    status.setText("Runtime 中未装配该 provider，保存后需重启")
                    return
                if not model:
                    emb_model = self._embedding_model_for_provider(name)
                    if not emb_model:
                        status.setText("没有 Agent、Vision 或 RAG 使用该 provider，无法自动选择模型")
                        return
                    from providers import probe_embedding_provider_instance

                    result = await probe_embedding_provider_instance(
                        provider,
                        model=emb_model,
                        api_key=self._embedding_api_key_for_provider(name),
                        timeout_seconds=8.0,
                    )
                else:
                    from providers import probe_provider_instance

                    result = await probe_provider_instance(
                        provider,
                        model=model,
                        protocol=self._provider_protocol(name),
                        timeout_seconds=8.0,
                    )
                self._runtime.provider_health[name] = result
                if result.status == "ok":
                    status.setText(f"可用 · {result.latency_ms}ms")
                else:
                    status.setText(result.message)
            except Exception as e:  # noqa: BLE001
                status.setText(f"测试失败：{e}")
            finally:
                if progress is not None:
                    progress.setRange(0, 100)
                    progress.setValue(100)
                    progress.setVisible(False)
                if button is not None:
                    button.setEnabled(True)
                    button.setText("测试连接")

        loop.create_task(_do_test())

    def _on_provider_dname_changed(self, name: str, value: str) -> None:
        if self._suppress_signals:
            return
        p = self._cfg().providers.get(name)
        if p is None or p.display_name == value:
            return
        p.display_name = value or None
        self._save_now(needs_restart=True, change_desc=f"provider.{name}.display_name")

    def _on_provider_baseurl_changed(self, name: str, value: str) -> None:
        if self._suppress_signals:
            return
        p = self._cfg().providers.get(name)
        if p is None or (p.base_url or "") == value:
            return
        p.base_url = value or None
        self._save_now(needs_restart=True, change_desc=f"provider.{name}.base_url")

    def _on_provider_key_changed(self, name: str, edit: QLineEdit) -> None:
        if self._suppress_signals:
            return
        new_key = edit.text()
        if not new_key:
            return
        p = self._cfg().providers.get(name)
        if p is None:
            return
        sid = p.api_key_id or f"{name}_key"
        p.api_key_id = sid
        self._set_secret(sid, new_key)
        edit.clear()  # 防止误以为留存
        self._save_now(needs_restart=True, change_desc=f"provider.{name}.api_key")

    def _on_delete_provider(self, name: str) -> None:
        # 检查是否被 agent 引用
        refs = []
        for an in ("chat", "proactive", "summary", "persona_gen"):
            a = getattr(self._cfg().agents, an, None)
            if a and a.provider == name:
                refs.append(an)
        if refs:
            show_message(
                self, "无法删除",
                f"provider [{name}] 仍被 agents 引用：{', '.join(refs)}\n请先把这些 agent 换成别的 provider。",
            )
            return
        if len(self._cfg().providers) <= 1:
            show_message(self, "无法删除", "至少要保留一个 provider。")
            return
        if not show_message(
            self, "删除提供商",
            f"确定删除 provider [{name}] 吗？\n\n这只会从 config 移除引用，"
            "secrets 中的密钥不会被自动清理（如要清密钥请手动改 secrets）。",
            confirm_text="删除", cancel_text="取消", is_danger=True,
        ):
            return
        self._cfg().providers.pop(name, None)
        self._save_now(needs_restart=True, change_desc=f"删除 provider {name}")
        self._render_providers()

    def _build_agent_row(self, agent_name: str, agent_cfg: AgentConfig) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.XS)

        head = QHBoxLayout()
        label = {"chat": "主聊天", "proactive": "主动思考", "summary": "历史总结"}.get(
            agent_name, agent_name
        )
        title = QLabel(label)
        title.setProperty("role", "title-3")
        head.addWidget(title)
        head.addStretch(1)
        outer.addLayout(head)

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        # provider 下拉
        prov_combo = QComboBox()
        for pid in self._cfg().providers.keys():
            prov_combo.addItem(pid, pid)
        idx = prov_combo.findData(agent_cfg.provider)
        if idx >= 0:
            prov_combo.setCurrentIndex(idx)
        prov_combo.currentIndexChanged.connect(
            lambda *_, an=agent_name, c=prov_combo: self._on_agent_provider_changed(an, c.currentData())
        )
        self._agent_provider_combos.append(prov_combo)
        form.addRow(QLabel("Provider"), prov_combo)

        # 模型 ID
        model_edit = ModelComboBox()
        model_edit.setText(agent_cfg.model)
        model_edit.setPlaceholderText("如 deepseek-v4-flash / claude-sonnet-4-6")
        if model_edit.lineEdit() is not None:
            model_edit.lineEdit().editingFinished.connect(
                lambda an=agent_name, e=model_edit: self._on_agent_model_changed(an, e.current_model_id())
            )
        model_edit.activated.connect(
            lambda *_args, an=agent_name, e=model_edit: self._on_agent_model_changed(an, e.current_model_id())
        )
        fetch_btn = QPushButton("获取模型")
        fetch_btn.setProperty("role", "secondary")
        fetch_btn.clicked.connect(
            lambda *_, an=agent_name, pc=prov_combo, me=model_edit, btn=fetch_btn:
            self._on_fetch_agent_models(an, pc.currentData(), me, btn)
        )
        model_row = QHBoxLayout()
        model_row.setSpacing(Spacing.SM)
        model_row.addWidget(model_edit, 1)
        model_row.addWidget(fetch_btn)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        form.addRow(QLabel("模型 ID"), model_wrap)

        if agent_name == "chat":
            loops_spin = QSpinBox()
            loops_spin.setRange(5, 60)
            loops_spin.setValue(min(60, max(5, int(agent_cfg.max_loops or 25))))
            loops_spin.setSuffix(" 轮")
            install_wheel_freeze(loops_spin)
            loops_spin.editingFinished.connect(
                lambda *_, an=agent_name, s=loops_spin: self._on_agent_max_loops_changed(
                    an,
                    s.value(),
                )
            )
            form.addRow(QLabel("工具轮数上限"), loops_spin)

        # 思考
        chk = QCheckBox("启用")
        is_on = bool(agent_cfg.reasoning and agent_cfg.reasoning.enabled)
        chk.setChecked(is_on)

        cmb = QComboBox()
        cmb.addItem("默认", None)
        cmb.addItem("低 · 快但浅", "low")
        cmb.addItem("中 · 平衡", "medium")
        cmb.addItem("高 · 慢但深", "high")
        current_budget = agent_cfg.reasoning.budget if agent_cfg.reasoning else None
        idx = cmb.findData(current_budget)
        if idx >= 0:
            cmb.setCurrentIndex(idx)
        cmb.setEnabled(is_on)

        def _on_reason_changed(_=None, an=agent_name, c=chk, b=cmb) -> None:
            if self._suppress_signals:
                return
            b.setEnabled(c.isChecked())
            self._on_agent_reasoning_changed(an, c.isChecked(), b.currentData())
        chk.toggled.connect(_on_reason_changed)
        cmb.currentIndexChanged.connect(_on_reason_changed)

        reasoning_row = QHBoxLayout()
        reasoning_row.setSpacing(Spacing.SM)
        reasoning_row.addWidget(chk)
        reasoning_row.addWidget(QLabel("深度"))
        reasoning_row.addWidget(cmb)
        reasoning_row.addStretch(1)
        reasoning_wrap = QWidget()
        reasoning_wrap.setLayout(reasoning_row)
        form.addRow(QLabel("思考"), reasoning_wrap)

        outer.addLayout(form)
        return wrap

    def _on_agent_provider_changed(self, agent_name: str, new_provider: str) -> None:
        if self._suppress_signals or not new_provider:
            return
        a = getattr(self._cfg().agents, agent_name, None)
        if a is None or a.provider == new_provider:
            return
        a.provider = new_provider
        self._save_now(needs_restart=True, change_desc=f"agents.{agent_name}.provider={new_provider}")

    def _on_fetch_agent_models(
        self,
        agent_name: str,
        provider_id: str,
        model_edit: ModelComboBox,
        button: QPushButton,
    ) -> None:
        if not provider_id:
            self._status.mark_error("请先选择 provider")
            return
        button.setEnabled(False)
        button.setText("获取中")

        async def _do_fetch() -> None:
            try:
                models = await self._fetch_models_for_provider(str(provider_id))
                preset = self._provider_preset_id(str(provider_id))
                model_edit.set_models(
                    [m.id for m in models],
                    provider_id=preset,
                    current=model_edit.current_model_id(),
                )
                self._status.set_changes(self._count_changes(), needs_restart=False)
            except Exception as e:
                self._status.mark_error(f"获取 {agent_name} 模型失败：{e}")
            finally:
                button.setEnabled(True)
                button.setText("获取模型")

        try:
            asyncio.get_event_loop().create_task(_do_fetch())
        except RuntimeError:
            button.setEnabled(True)
            button.setText("获取模型")
            self._status.mark_error("事件循环未就绪")

    def _on_agent_model_changed(self, agent_name: str, model: str) -> None:
        if self._suppress_signals or not model:
            return
        a = getattr(self._cfg().agents, agent_name, None)
        if a is None or a.model == model:
            return
        a.model = model
        self._save_now(needs_restart=True, change_desc=f"agents.{agent_name}.model={model}")

    def _on_agent_max_loops_changed(self, agent_name: str, value: int) -> None:
        if self._suppress_signals:
            return
        a = getattr(self._cfg().agents, agent_name, None)
        if a is None:
            return
        value = min(60, max(5, int(value)))
        if a.max_loops == value:
            return
        a.max_loops = value
        self._save_now(needs_restart=False, change_desc=f"agents.{agent_name}.max_loops={value} (hot)")

    def _on_agent_reasoning_changed(self, agent_name: str, enabled: bool, budget) -> None:
        if self._suppress_signals:
            return
        a = getattr(self._cfg().agents, agent_name, None)
        if a is None:
            return
        if enabled:
            a.reasoning = ReasoningConfig(enabled=True, budget=budget)
        else:
            a.reasoning = None
        self._save_now(needs_restart=True, change_desc=f"agents.{agent_name}.reasoning")

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

