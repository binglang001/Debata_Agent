"""设置页 —— 全字段可改 + 即时保存 + 重启提示。

左侧导航按模型、功能、渠道、记忆、软件行为、Token预算和日志诊断分区。
每个字段改动立即写入磁盘；hot 字段（白名单 / log 级别 / 主题）立即生效；
其它字段标记 needs_restart，底部状态条提示用户重启 Debata 服务。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
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
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_config.schema import (
    AgentConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    ReasoningConfig,
    ToolResultBudgetConfig,
    WhitelistConfig,
    default_tool_result_budgets,
)

from ..theme import Spacing
from ..widgets import AutoSizeStack, show_message
from ..widgets.model_combo import ModelComboBox
from ..widgets.wheel_freeze import install_wheel_freeze
from ..wizard.components import SectionCard, WhitelistEditor, WhitelistState
from .copy import DASHBOARD_COPY
from .settings import (
    CollapsibleSection,
    _AddProviderDialog,
    _ASREditDialog,
    _EmbeddingEditDialog,
    _load_provider_presets_for_dialog,
    _SaveStatusBar,
    _TTSEditDialog,
    _VisionEditDialog,
    _WeatherEditDialog,
)
from .settings.helpers import (
    _format_tool_result_overrides,
    _parse_tool_result_overrides,
    _progress_slot,
    _tool_budget_group_hint,
)
from .settings.state import SettingsStateMixin

logger = logging.getLogger(__name__)


# ============================================================
# 主 SettingsPage
# ============================================================


class SettingsPage(SettingsStateMixin, QWidget):
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

    # ============================================================
    # 功能节：features 全部可改
    # ============================================================

    def _build_features_section(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.MD)

        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_features"],
            subtitle="每项功能独立配置。开关即时保存，密钥/配置修改后需重启。",
        )
        card.add_content(self._build_vision_card())
        card.add_content(self._build_weather_card())
        card.add_content(self._build_websearch_card())
        card.add_content(self._build_tts_card())
        layout.addWidget(card)
        layout.addWidget(self._build_emoji_section())
        return page

    def _build_vision_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        v = self._cfg().features.vision
        head = QHBoxLayout()
        chk = QCheckBox("看懂图片（vision）")
        chk.setChecked(v.enabled)
        self._vision_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        edit_btn = QPushButton("编辑配置")
        edit_btn.setProperty("role", "secondary")
        head.addWidget(edit_btn)
        outer.addLayout(head)

        summary = QLabel(self._vision_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(24, 0, 0, 0)
        self._vision_summary_lbl = summary
        outer.addWidget(summary)

        edit_btn.clicked.connect(lambda: self._open_vision_dialog(chk, summary))

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            v_now = self._cfg().features.vision
            if on:
                if not v_now.provider or not v_now.model:
                    if not self._open_vision_dialog(chk, summary):
                        self._suppress_signals = True
                        chk.setChecked(False)
                        self._suppress_signals = False
                        return
                v_now.enabled = True
            else:
                v_now.enabled = False
            summary.setText(self._vision_summary())
            try:
                self._validate_features_then_save("vision toggle")
            except Exception as e:  # noqa: BLE001
                self._suppress_signals = True
                chk.setChecked(not on)
                v_now.enabled = not on
                self._suppress_signals = False
                show_message(self, "未能保存", str(e))

        chk.toggled.connect(_on_toggle)
        return wrap

    def _vision_summary(self) -> str:
        v = self._cfg().features.vision
        if not v.provider and not v.model:
            return "未配置 · 启用时会弹窗引导填写"
        return f"provider: {v.provider or '?'}  ·  model: {v.model or '?'}"

    def _open_vision_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        v = self._cfg().features.vision
        provider_ids = list(self._cfg().providers.keys())
        provider_presets = {pid: (p.preset or "") for pid, p in self._cfg().providers.items()}
        dlg = _VisionEditDialog(
            provider_ids, provider_presets,
            v.provider, v.model, v.api_key_id, self,
        )
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        v.provider = data["provider"]
        v.model = data["model"]
        if data["api_key"]:
            sid = v.api_key_id or "vision_key"
            v.api_key_id = sid
            provider_cfg = self._cfg().providers.get(v.provider)
            if provider_cfg is not None:
                provider_cfg.api_key_id = sid
            self._set_secret(sid, data["api_key"])
        v.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._vision_summary())
        self._save_now(needs_restart=True, change_desc="features.vision (dialog)")
        return True

    def _build_weather_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        w = self._cfg().features.weather
        head = QHBoxLayout()
        chk = QCheckBox("查天气（和风天气）")
        chk.setChecked(w.enabled)
        self._weather_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        edit_btn = QPushButton("编辑配置")
        edit_btn.setProperty("role", "secondary")
        head.addWidget(edit_btn)
        outer.addLayout(head)

        summary = QLabel(self._weather_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(24, 0, 0, 0)
        self._weather_summary_lbl = summary
        outer.addWidget(summary)

        edit_btn.clicked.connect(lambda: self._open_weather_dialog(chk, summary))

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            w_now = self._cfg().features.weather
            if on:
                if not w_now.api_key_id:
                    if not self._open_weather_dialog(chk, summary):
                        self._suppress_signals = True
                        chk.setChecked(False)
                        self._suppress_signals = False
                        return
                w_now.enabled = True
            else:
                w_now.enabled = False
            summary.setText(self._weather_summary())
            try:
                self._validate_features_then_save("weather toggle")
            except Exception as e:  # noqa: BLE001
                self._suppress_signals = True
                chk.setChecked(not on)
                w_now.enabled = not on
                self._suppress_signals = False
                show_message(self, "未能保存", str(e))

        chk.toggled.connect(_on_toggle)
        return wrap

    def _weather_summary(self) -> str:
        w = self._cfg().features.weather
        if not w.api_key_id:
            return "未配置 · 启用时会弹窗引导填写"
        return f"host: {w.host}  ·  密钥 id: {w.api_key_id}"

    def _open_weather_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        w = self._cfg().features.weather
        dlg = _WeatherEditDialog(w.host or "", w.api_key_id, self)
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        w.host = data["host"]
        if data["api_key"]:
            sid = w.api_key_id or "qweather"
            w.api_key_id = sid
            self._set_secret(sid, data["api_key"])
        w.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._weather_summary())
        self._save_now(needs_restart=True, change_desc="features.weather (dialog)")
        return True

    def _build_websearch_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        ws = self._cfg().features.web_search
        head = QHBoxLayout()
        chk = QCheckBox("联网搜索（DuckDuckGo · 无需密钥）")
        chk.setChecked(ws.enabled)
        self._ws_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        outer.addLayout(head)

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            ws.enabled = on
            self._save_now(needs_restart=True, change_desc="features.web_search.enabled")

        chk.toggled.connect(_on_toggle)
        return wrap

    def _open_asr_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        a = self._cfg().features.asr
        dlg = _ASREditDialog(a, self)
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        a.type = data["type"]
        if data["type"] == "api":
            a.provider = data["provider"]
            a.extra_credentials = data.get("extra_credentials", {})
            if data.get("api_key"):
                sid = a.api_key_id or "asr_key"
                a.api_key_id = sid
                self._set_secret(sid, data["api_key"])
        else:
            a.device = data["device"]
            a.language = data["language"]
            a.model_dir = data["model_dir"]
        a.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._asr_summary())
        self._save_now(needs_restart=True, change_desc="features.asr (dialog)")
        return True

    def _build_tts_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        feat = self._cfg().features.tts
        head = QHBoxLayout()
        chk = QCheckBox("用声音说话（TTS）")
        chk.setChecked(feat.enabled)
        self._tts_chk = chk
        head.addWidget(chk)
        head.addStretch(1)
        edit_btn = QPushButton("编辑配置")
        edit_btn.setProperty("role", "secondary")
        head.addWidget(edit_btn)
        outer.addLayout(head)

        summary = QLabel(self._tts_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(24, 0, 0, 0)
        self._tts_summary_lbl = summary
        outer.addWidget(summary)

        edit_btn.clicked.connect(lambda: self._open_tts_dialog(chk, summary))

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            if on:
                t = self._cfg().features.tts
                if t.type == "api" and not t.provider:
                    if not self._open_tts_dialog(chk, summary):
                        self._suppress_signals = True
                        chk.setChecked(False)
                        self._suppress_signals = False
                        return
                t.enabled = True
            else:
                feat.enabled = False
            summary.setText(self._tts_summary())
            self._save_now(needs_restart=True, change_desc="features.tts.enabled")

        chk.toggled.connect(_on_toggle)
        return wrap

    def _open_tts_dialog(self, chk: QCheckBox, summary: QLabel) -> bool:
        t = self._cfg().features.tts
        dlg = _TTSEditDialog(t, self)
        if not dlg.exec() or not dlg.result_data:
            return False
        data = dlg.result_data
        t.type = data["type"]
        if data["type"] == "api":
            t.provider = data["provider"]
            t.extra_credentials = data.get("extra_credentials", {})
            if data["provider"] == "edge":
                t.api_key_id = None
            elif data.get("api_key"):
                sid = t.api_key_id or f"tts_{data['provider']}"
                t.api_key_id = sid
                self._set_secret(sid, data["api_key"])
        else:
            t.device = data.get("device", "auto")
            t.reference_audio = data["reference_audio"]
            t.default_prompt = data["default_prompt"]
            t.model_dir = data.get("model_dir", "data/models/VoxCPM2")
            t.load_denoiser = bool(data.get("load_denoiser", False))
            t.cfg_value = float(data.get("cfg_value", 2.0))
            t.inference_timesteps = int(data.get("inference_timesteps", 10))
        t.enabled = True
        self._suppress_signals = True
        chk.setChecked(True)
        self._suppress_signals = False
        summary.setText(self._tts_summary())
        self._save_now(needs_restart=True, change_desc="features.tts (dialog)")
        return True

    def _asr_summary(self) -> str:
        a = self._cfg().features.asr
        if not a.enabled:
            return "未启用"
        if a.type == "local":
            return f"本地 · {a.local_model} · device={a.device} · lang={a.language}"
        return f"API · provider={a.provider or '?'}"

    def _tts_summary(self) -> str:
        t = self._cfg().features.tts
        if not t.enabled:
            return "未启用"
        if t.type == "local":
            detail = f" · ref={t.reference_audio}" if t.reference_audio else ""
            if t.default_prompt and not detail:
                detail = f" · prompt={t.default_prompt}"
            return f"本地 · {t.local_model} · device={t.device}{detail}"
        if t.provider == "edge":
            return "API · EdgeTTS（无需密钥）"
        voice = t.extra_credentials.get("voice", "") if t.extra_credentials else ""
        detail = f" · voice={voice}" if voice else ""
        return f"API · provider={t.provider or '?'}{detail}"

    def _build_embedding_card(self) -> QWidget:
        """RAG embedding 配置卡片。"""
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        title = QLabel("Embedding（RAG 向量检索）")
        title.setProperty("role", "title-3")
        outer.addWidget(title)

        desc_text = "将对话内容转为数学向量用于语义检索，让长记忆模式下 AI 只注入最相关的少量记忆。"
        lt = self._cfg().features.long_term_memory
        if lt.mode != "rag":
            desc_text += "\n当前长期记忆模式不是 RAG，此处配置暂不生效。"
        desc = QLabel(desc_text)
        desc.setProperty("role", "secondary")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        summary = QLabel(self._embedding_summary())
        summary.setProperty("role", "secondary")
        summary.setWordWrap(True)
        summary.setContentsMargins(0, Spacing.SM, 0, 0)
        self._emb_summary_lbl = summary
        outer.addWidget(summary)

        action_row = QHBoxLayout()
        edit_btn = QPushButton("编辑 Embedding 配置")
        edit_btn.setProperty("role", "secondary")
        action_row.addWidget(edit_btn)
        guide_btn = QPushButton("教程")
        guide_btn.setProperty("role", "secondary")
        action_row.addWidget(guide_btn)
        action_row.addStretch(1)
        outer.addLayout(action_row)

        edit_btn.clicked.connect(lambda: self._open_embedding_dialog(summary))
        guide_btn.clicked.connect(lambda: self._open_feature_guide("embedding_rag"))
        return wrap

    def _open_feature_guide(self, guide_name: str) -> None:
        from ui.wizard.components import open_feature_guide

        open_feature_guide(guide_name, self)

    def _embedding_summary(self) -> str:
        emb = self._cfg().features.embedding
        if emb.type == "api":
            return f"API 模式 · provider={emb.provider or '?'} · model={emb.api_model or '?'}"
        return f"本地模式 · {emb.local_quality} · dir={emb.local_model_dir or '?'}"

    def _open_embedding_dialog(self, summary: QLabel) -> None:
        emb = self._cfg().features.embedding
        provider_ids = list(self._cfg().providers.keys()) if self._cfg().providers else []
        dlg = _EmbeddingEditDialog(provider_ids, emb, self)
        if not dlg.exec() or not dlg.result_data:
            return
        data = dlg.result_data
        emb.enabled = True
        emb.type = data["type"]
        if data["type"] == "api":
            emb.provider = data["provider"]
            emb.api_model = data["model"]
            if data.get("api_key"):
                sid = emb.api_key_id or "embedding_key"
                emb.api_key_id = sid
                self._set_secret(sid, data["api_key"])
        else:
            emb.local_quality = data["local_quality"]
            emb.local_model_dir = data["local_model_dir"]
        summary.setText(self._embedding_summary())
        self._save_now(needs_restart=True, change_desc="features.embedding (dialog)")

    def _build_longterm_memory_card(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        lt = self._cfg().features.long_term_memory
        title = QLabel("长期记忆模式")
        title.setProperty("role", "title-3")
        outer.addWidget(title)

        group = QButtonGroup(wrap)
        group.setExclusive(True)
        rb_file = QRadioButton("文件模式（默认 · 零开销 · AI 主动调工具）")
        rb_rag = QRadioButton("RAG 向量检索（需启用下方 Embedding 配置）")
        rb_file.setChecked(lt.mode == "file")
        rb_rag.setChecked(lt.mode == "rag")
        group.addButton(rb_file)
        group.addButton(rb_rag)
        outer.addWidget(rb_file)
        outer.addWidget(rb_rag)

        chk_kw = QCheckBox("命中关键词强制保存（记住 / 约定 / 我叫等）")
        chk_kw.setChecked(lt.keyword_trigger_save)
        outer.addWidget(chk_kw)

        def _on_mode(*_) -> None:
            if self._suppress_signals:
                return
            lt.mode = "rag" if rb_rag.isChecked() else "file"
            self._save_now(needs_restart=True, change_desc=f"long_term_memory.mode={lt.mode}")

        def _on_kw(on: bool) -> None:
            if self._suppress_signals:
                return
            lt.keyword_trigger_save = on
            self._save_now(needs_restart=True, change_desc="long_term_memory.keyword_trigger_save")

        rb_file.toggled.connect(_on_mode)
        rb_rag.toggled.connect(_on_mode)
        chk_kw.toggled.connect(_on_kw)
        return wrap

    def _validate_features_then_save(self, change_desc: str) -> None:
        # 让 pydantic 重新走一遍 model_validator
        f = self._cfg().features
        # vision 启用时 provider 或 api_key_id 不能空
        if f.vision.enabled and f.vision.type == "api":
            if not f.vision.provider and not f.vision.api_key_id:
                raise ValueError("vision 启用必须填 provider 或 api_key_id")
        # weather 启用时密钥必填
        if f.weather.enabled and not f.weather.api_key_id:
            raise ValueError("weather 启用必须填 api_key_id（先粘贴 API 密钥再开启开关）")
        self._save_now(needs_restart=True, change_desc=change_desc)

    # ============================================================
    # 渠道节：adapter 全部可改 + 测试连接
    # ============================================================

    def _rebuild_adapter_form(self) -> None:
        """清空并重建 adapter 节表单（切换 adapter 时调用）。"""
        # 清空旧控件
        while self._adapter_container.count():
            item = self._adapter_container.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._adapter_container.addWidget(self._build_adapter_section())
        self._schedule_settings_content_sync()

    def _build_adapter_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_adapter"],
            subtitle="NapCat 连接、白名单。白名单立即生效；其它字段改完需重启。",
        )

        adapter_names = list(self._cfg().adapters.keys())
        if not adapter_names:
            card.add_content(QLabel("未配置任何 adapter。"))
            return card

        # 多 adapter 选择器
        if len(adapter_names) > 1:
            sel_row = QHBoxLayout()
            sel_row.addWidget(QLabel("配置的 Adapter"))
            adapter_combo = QComboBox()
            for aname in adapter_names:
                adapter_combo.addItem(aname, aname)
            # 回填当前选中
            cur_idx = adapter_combo.findData(self._adapter_name)
            if cur_idx >= 0:
                adapter_combo.setCurrentIndex(cur_idx)
            sel_row.addWidget(adapter_combo, 1)
            card.add_layout(sel_row)

            def _on_adapter_switch():
                new_name = adapter_combo.currentData()
                if new_name and new_name != self._adapter_name:
                    self._adapter_name = new_name
                    self._rebuild_adapter_form()

            adapter_combo.currentIndexChanged.connect(_on_adapter_switch)
        else:
            self._adapter_name = adapter_names[0]

        # 总是动态取当前 adapter 的配置
        def _cfg():
            return self._cfg().adapters[self._adapter_name]

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        # 模式
        mode_combo = QComboBox()
        mode_combo.addItem("client（程序连 NapCat 正向 WS）", "client")
        mode_combo.addItem("server（程序监听，NapCat 反向连入）", "server")
        idx = mode_combo.findData(_cfg().mode)
        if idx >= 0:
            mode_combo.setCurrentIndex(idx)
        mode_combo.currentIndexChanged.connect(
            lambda *_: self._on_adapter_field_changed(_cfg(), "mode", mode_combo.currentData())
        )
        form.addRow(QLabel("模式"), mode_combo)

        host_edit = QLineEdit(_cfg().host)
        host_edit.setPlaceholderText("client: NapCat 地址；server: 监听地址，跨设备用 0.0.0.0")
        host_edit.editingFinished.connect(
            lambda h=host_edit: self._on_adapter_field_changed(
                _cfg(),
                "host",
                h.text().strip()
                or ("0.0.0.0" if mode_combo.currentData() == "server" else "127.0.0.1"),
            )
        )
        form.addRow(QLabel("地址"), host_edit)

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(_cfg().port)
        port_spin.editingFinished.connect(
            lambda p=port_spin: self._on_adapter_field_changed(_cfg(), "port", p.value())
        )
        form.addRow(QLabel("端口"), port_spin)

        path_edit = QLineEdit(_cfg().path)
        path_edit.editingFinished.connect(
            lambda e=path_edit: self._on_adapter_field_changed(_cfg(), "path", e.text().strip() or "/")
        )
        form.addRow(QLabel("WebSocket 路径"), path_edit)

        # token 替换
        tok_edit = QLineEdit()
        tok_edit.setEchoMode(QLineEdit.EchoMode.Password)
        tok_edit.setPlaceholderText(
            f"留空 = 保留现有（id={_cfg().access_token_id or '未设'}）；填写则替换"
        )
        tok_edit.editingFinished.connect(lambda e=tok_edit: self._on_adapter_token_changed(_cfg(), e))
        form.addRow(QLabel("Access Token"), tok_edit)

        # 进程托管
        manage_chk = QCheckBox("由 Debata 托管 NapCat 进程")
        manage_chk.setChecked(_cfg().manage_process)
        proc_edit = QLineEdit(_cfg().process_path)
        proc_edit.setPlaceholderText("如 D:/NapCat/start.bat 或 NapCatWinBootMain.exe")
        proc_edit.setVisible(_cfg().manage_process)

        def _on_manage(on: bool) -> None:
            if self._suppress_signals:
                return
            proc_edit.setVisible(on)
            _cfg().manage_process = on
            self._save_now(needs_restart=True, change_desc="adapter.manage_process")

        manage_chk.toggled.connect(_on_manage)
        proc_edit.editingFinished.connect(
            lambda e=proc_edit: self._on_adapter_field_changed(_cfg(), "process_path", e.text().strip())
        )
        manage_row = QVBoxLayout()
        manage_row.addWidget(manage_chk)
        manage_row.addWidget(proc_edit)
        manage_wrap = QWidget()
        manage_wrap.setLayout(manage_row)
        form.addRow(QLabel("进程"), manage_wrap)

        # 测试连接按钮
        test_row = QHBoxLayout()
        test_btn = QPushButton("测试连接")
        test_btn.setProperty("role", "secondary")
        self._adapter_test_status = QLabel("")
        self._adapter_test_status.setProperty("role", "secondary")
        self._adapter_test_progress = QProgressBar()
        self._adapter_test_progress.setRange(0, 100)
        self._adapter_test_progress.setTextVisible(False)
        self._adapter_test_progress.setVisible(False)
        test_btn.clicked.connect(lambda: self._on_test_adapter(_cfg(), test_btn))
        test_row.addWidget(test_btn)
        test_row.addWidget(self._adapter_test_status, 1)
        test_wrap = QWidget()
        test_wrap_layout = QVBoxLayout(test_wrap)
        test_wrap_layout.setContentsMargins(0, 0, 0, 0)
        test_wrap_layout.setSpacing(Spacing.XS)
        test_wrap_layout.addLayout(test_row)
        test_wrap_layout.addWidget(_progress_slot(self._adapter_test_progress))
        form.addRow(QLabel(""), test_wrap)

        card.add_layout(form)

        advanced = CollapsibleSection(
            "NapCat 连接高级参数",
            "这些参数通常不需要改。只有在首条消息经常等待、断线重连异常或托管进程启动过慢时再调整。",
            expanded=False,
        )
        adv_form = QFormLayout()
        adv_form.setSpacing(Spacing.SM)

        startup_timeout = QDoubleSpinBox()
        startup_timeout.setRange(0.0, 30.0)
        startup_timeout.setSingleStep(0.5)
        startup_timeout.setValue(_cfg().startup_connect_timeout_seconds)
        startup_timeout.setSuffix(" 秒")
        startup_timeout.setToolTip("Runtime 启动时最多等待 NapCat 首次连接的时间；超过后后台继续重连。")
        startup_timeout.editingFinished.connect(
            lambda s=startup_timeout: self._on_adapter_field_changed(
                _cfg(), "startup_connect_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("启动等待连接"), startup_timeout)

        api_wait = QDoubleSpinBox()
        api_wait.setRange(0.0, 30.0)
        api_wait.setSingleStep(0.5)
        api_wait.setValue(_cfg().api_wait_connected_timeout_seconds)
        api_wait.setSuffix(" 秒")
        api_wait.setToolTip("调用 OneBot API 前等待连接建立的最长时间。0 表示不等待。")
        api_wait.editingFinished.connect(
            lambda s=api_wait: self._on_adapter_field_changed(
                _cfg(), "api_wait_connected_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("API 前等待连接"), api_wait)

        api_timeout = QDoubleSpinBox()
        api_timeout.setRange(1.0, 300.0)
        api_timeout.setSingleStep(5.0)
        api_timeout.setValue(_cfg().api_timeout_seconds)
        api_timeout.setSuffix(" 秒")
        api_timeout.setToolTip("单次 OneBot API 调用的超时。")
        api_timeout.editingFinished.connect(
            lambda s=api_timeout: self._on_adapter_field_changed(
                _cfg(), "api_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("API 超时"), api_timeout)

        fast_attempts = QSpinBox()
        fast_attempts.setRange(0, 50)
        fast_attempts.setValue(_cfg().fast_reconnect_attempts)
        fast_attempts.setToolTip("断线后的快速重试次数。")
        fast_attempts.editingFinished.connect(
            lambda s=fast_attempts: self._on_adapter_field_changed(
                _cfg(), "fast_reconnect_attempts", s.value()
            )
        )
        adv_form.addRow(QLabel("快速重试次数"), fast_attempts)

        fast_interval = QDoubleSpinBox()
        fast_interval.setRange(0.0, 10.0)
        fast_interval.setSingleStep(0.1)
        fast_interval.setValue(_cfg().fast_reconnect_interval_seconds)
        fast_interval.setSuffix(" 秒")
        fast_interval.setToolTip("快速重试阶段每次等待多久。")
        fast_interval.editingFinished.connect(
            lambda s=fast_interval: self._on_adapter_field_changed(
                _cfg(), "fast_reconnect_interval_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("快速重试间隔"), fast_interval)

        reconnect_interval = QDoubleSpinBox()
        reconnect_interval.setRange(0.1, 120.0)
        reconnect_interval.setSingleStep(0.5)
        reconnect_interval.setValue(_cfg().reconnect_interval_seconds)
        reconnect_interval.setSuffix(" 秒")
        reconnect_interval.setToolTip("快速重试结束后的指数退避起点。")
        reconnect_interval.editingFinished.connect(
            lambda s=reconnect_interval: self._on_adapter_field_changed(
                _cfg(), "reconnect_interval_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("慢速重连起点"), reconnect_interval)

        backoff_max = QDoubleSpinBox()
        backoff_max.setRange(1.0, 600.0)
        backoff_max.setSingleStep(5.0)
        backoff_max.setValue(_cfg().reconnect_backoff_max_seconds)
        backoff_max.setSuffix(" 秒")
        backoff_max.setToolTip("指数退避的最大等待时间。")
        backoff_max.editingFinished.connect(
            lambda s=backoff_max: self._on_adapter_field_changed(
                _cfg(), "reconnect_backoff_max_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("退避上限"), backoff_max)

        jitter = QDoubleSpinBox()
        jitter.setRange(0.0, 10.0)
        jitter.setSingleStep(0.1)
        jitter.setValue(_cfg().reconnect_jitter_seconds)
        jitter.setSuffix(" 秒")
        jitter.setToolTip("慢速重连等待的随机抖动上限，避免固定节拍重试。")
        jitter.editingFinished.connect(
            lambda s=jitter: self._on_adapter_field_changed(
                _cfg(), "reconnect_jitter_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("重连抖动"), jitter)

        max_reconnect = QSpinBox()
        max_reconnect.setRange(-1, 100000)
        max_reconnect.setValue(_cfg().max_reconnect_attempts)
        max_reconnect.setSpecialValueText("无限")
        max_reconnect.setToolTip("-1 表示无限重连。")
        max_reconnect.editingFinished.connect(
            lambda s=max_reconnect: self._on_adapter_field_changed(
                _cfg(), "max_reconnect_attempts", s.value()
            )
        )
        adv_form.addRow(QLabel("最大重连次数"), max_reconnect)

        ping_interval = QDoubleSpinBox()
        ping_interval.setRange(1.0, 300.0)
        ping_interval.setSingleStep(5.0)
        ping_interval.setValue(_cfg().ping_interval_seconds)
        ping_interval.setSuffix(" 秒")
        ping_interval.setToolTip("WebSocket ping 间隔。")
        ping_interval.editingFinished.connect(
            lambda s=ping_interval: self._on_adapter_field_changed(
                _cfg(), "ping_interval_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("Ping 间隔"), ping_interval)

        ping_timeout = QDoubleSpinBox()
        ping_timeout.setRange(1.0, 300.0)
        ping_timeout.setSingleStep(5.0)
        ping_timeout.setValue(_cfg().ping_timeout_seconds)
        ping_timeout.setSuffix(" 秒")
        ping_timeout.setToolTip("WebSocket ping 未响应多久判定断线。")
        ping_timeout.editingFinished.connect(
            lambda s=ping_timeout: self._on_adapter_field_changed(
                _cfg(), "ping_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("Ping 超时"), ping_timeout)

        warmup = QDoubleSpinBox()
        warmup.setRange(0.0, 60.0)
        warmup.setSingleStep(0.5)
        warmup.setValue(_cfg().process_warmup_seconds)
        warmup.setSuffix(" 秒")
        warmup.setToolTip("托管 NapCat 进程启动后，连接前等待的时间。")
        warmup.editingFinished.connect(
            lambda s=warmup: self._on_adapter_field_changed(
                _cfg(), "process_warmup_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("托管进程预热"), warmup)

        voice_delay = QDoubleSpinBox()
        voice_delay.setRange(0.0, 8.0)
        voice_delay.setSingleStep(0.5)
        voice_delay.setValue(_cfg().voice_fetch_delay_seconds)
        voice_delay.setSuffix(" 秒")
        voice_delay.setToolTip("调用 NapCat 语音转文字前先等待多久。")
        voice_delay.editingFinished.connect(
            lambda s=voice_delay: self._on_adapter_field_changed(
                _cfg(), "voice_fetch_delay_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("语音转写等待"), voice_delay)

        advanced.add_layout(adv_form)
        card.add_content(advanced)

        # 白名单（hot，立即生效）
        sep = QFrame()
        sep.setProperty("role", "separator")
        card.add_content(sep)
        wl_title = QLabel("白名单（立即生效）")
        wl_title.setProperty("role", "title-3")
        card.add_content(wl_title)

        cfg_snapshot = _cfg()
        current = WhitelistState(
            mode=cfg_snapshot.whitelist.mode,
            qq_ids=[str(x) for x in cfg_snapshot.whitelist.qq_ids],
            group_ids=[str(x) for x in cfg_snapshot.whitelist.group_ids],
        )
        wl_editor = WhitelistEditor(
            initial=current,
            on_open_confirm=lambda: bool(show_message(
                self, "对所有人开放？",
                "陌生人也能让 Debata 回复，可能产生意外的 API 费用。",
                confirm_text="我清楚了", cancel_text="算了", is_danger=True,
            )),
        )

        def _on_wl(state: WhitelistState) -> None:
            if self._suppress_signals:
                return
            wl = WhitelistConfig(
                mode=state.mode,
                qq_ids=[int(x) for x in state.qq_ids if x.isdigit()],
                group_ids=[int(x) for x in state.group_ids if x.isdigit()],
            )
            _cfg().whitelist = wl
            self._save_now(needs_restart=False, change_desc="adapter.whitelist (hot)")

        wl_editor.state_changed.connect(_on_wl)
        card.add_content(wl_editor)
        return card

    def _on_adapter_field_changed(self, cfg: NapCatAdapterConfig, field: str, value) -> None:
        if self._suppress_signals:
            return
        if getattr(cfg, field) == value:
            return
        setattr(cfg, field, value)
        self._save_now(needs_restart=True, change_desc=f"adapter.{field}")

    def _on_adapter_token_changed(self, cfg: NapCatAdapterConfig, edit: QLineEdit) -> None:
        if self._suppress_signals:
            return
        new = edit.text()
        if not new:
            return
        sid = cfg.access_token_id or "napcat_default_token"
        cfg.access_token_id = sid
        self._set_secret(sid, new)
        edit.clear()
        self._save_now(needs_restart=True, change_desc="adapter.access_token")

    def _on_test_adapter(
        self,
        cfg: NapCatAdapterConfig,
        button: QPushButton | None = None,
    ) -> None:
        """非侵入式测试渠道状态。

        设置页不能创建第二条 NapCat WebSocket：NapCat/OneBot 端常见单连接，
        临时连接会挤掉正在收消息的主连接，随后 stop 临时连接会让后续收不到消息。
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._adapter_test_status.setText("⚠ 事件循环未就绪")
            return

        self._adapter_test_status.setText("正在测试……")
        self._adapter_test_progress.setVisible(True)
        self._adapter_test_progress.setRange(0, 0)
        if button is not None:
            button.setEnabled(False)
            button.setText("测试中")

        async def _do_test() -> None:
            try:
                running = self._running_adapter_for_current_page()
                if running is not None:
                    if getattr(running, "is_connected", False):
                        self._adapter_test_status.setText("✓ 当前 Runtime 渠道已连接")
                    else:
                        self._adapter_test_status.setText("⚠ 当前 Runtime 渠道未连接，后台会继续重连")
                elif cfg.mode == "client":
                    ok = await self._probe_tcp_port(cfg.host, cfg.port)
                    if ok:
                        self._adapter_test_status.setText(
                            f"✓ TCP 端口可达 ws://{cfg.host}:{cfg.port}{cfg.path}（未建立 WS 会话）"
                        )
                    else:
                        self._adapter_test_status.setText("✗ TCP 端口不可达，检查 NapCat 是否启动 / 地址端口")
                else:
                    available = await asyncio.to_thread(
                        self._can_bind_adapter_port, cfg.host, cfg.port
                    )
                    if available:
                        self._adapter_test_status.setText(
                            f"✓ 本机端口可监听 ws://{cfg.host}:{cfg.port}{cfg.path}；保存重启后等待 NapCat 连入"
                        )
                    else:
                        self._adapter_test_status.setText(
                            "⚠ 本机端口已被占用；如果 Debata 正在运行，这是正常的"
                        )
            except Exception as e:  # noqa: BLE001
                self._adapter_test_status.setText(f"✗ 未能完成：{e}")
            finally:
                self._adapter_test_progress.setRange(0, 100)
                self._adapter_test_progress.setValue(100)
                self._adapter_test_progress.setVisible(False)
                if button is not None:
                    button.setEnabled(True)
                    button.setText("测试连接")

        loop.create_task(_do_test())

    def _running_adapter_for_current_page(self):
        adapter = getattr(self._runtime, "adapter", None)
        if adapter is None:
            return None
        if getattr(adapter, "name", "") != getattr(self, "_adapter_name", ""):
            return None
        return adapter

    @staticmethod
    async def _probe_tcp_port(host: str, port: int, *, timeout: float = 2.0) -> bool:
        writer = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            return True
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    @staticmethod
    def _can_bind_adapter_port(host: str, port: int) -> bool:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
                return True
        except OSError:
            return False

    # ============================================================
    # 人格节
    # ============================================================

    def _build_persona_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_persona"],
            subtitle="切换角色请到左侧「角色」页（涉及人格档案复制 / 激活 / 导入导出）。",
        )
        card.add_content(QLabel(f"当前：{self._cfg().persona.active}"))
        return card

    # ============================================================
    # 外观节
    # ============================================================

    def _build_emoji_section(self) -> QWidget:
        from .emoji_section import EmojiSection

        emoji_dir = self._runtime.paths.EMOJI_DIR if self._runtime and self._runtime.paths else None
        if emoji_dir is None:
            # 占位
            card = SectionCard(title="表情包", subtitle="（运行时未就绪）")
            return card
        return EmojiSection(emoji_dir)

    def _build_appearance_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_appearance"],
            subtitle="主题切换立即生效，并会保存到配置。",
        )
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)

        rb_auto = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_auto"])
        rb_auto.setProperty("theme_value", "auto")
        rb_light = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_light"])
        rb_light.setProperty("theme_value", "light")
        rb_dark = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_dark"])
        rb_dark.setProperty("theme_value", "dark")
        self._theme_group.addButton(rb_auto)
        self._theme_group.addButton(rb_light)
        self._theme_group.addButton(rb_dark)

        rb_auto.toggled.connect(lambda on: on and self._on_theme_rb_changed("auto"))
        rb_light.toggled.connect(lambda on: on and self._on_theme_rb_changed("light"))
        rb_dark.toggled.connect(lambda on: on and self._on_theme_rb_changed("dark"))

        self._current_theme = self._cfg().app.theme
        if self._current_theme == "auto":
            rb_auto.setChecked(True)
        elif self._current_theme == "dark":
            rb_dark.setChecked(True)
        else:
            rb_light.setChecked(True)

        row = QHBoxLayout()
        row.addWidget(rb_auto)
        row.addWidget(rb_light)
        row.addWidget(rb_dark)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        card.add_content(wrap)
        return card

    def _on_theme_rb_changed(self, target: str) -> None:
        if self._suppress_signals:
            return
        if self._cfg().app.theme == target:
            self._current_theme = target
            return
        self._current_theme = target
        self._cfg().app.theme = target
        self._save_now(needs_restart=False, change_desc=f"app.theme={target} (hot)")
        self.theme_changed.emit(target)

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
