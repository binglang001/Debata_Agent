"""Model/provider setting sections for SettingsPage.

This module is a mechanical split from `ui.dashboard.settings_page`. Keep
behavior equivalent; do not change provider rows, agent rows, model fetching,
or save logic while moving methods.
"""

from __future__ import annotations

import asyncio

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_config.schema import AgentConfig, ProviderConfig, ReasoningConfig

from ...theme import Spacing
from ...widgets import show_message
from ...widgets.model_combo import ModelComboBox
from ...widgets.wheel_freeze import install_wheel_freeze
from ...wizard.components import SectionCard
from ..copy import DASHBOARD_COPY
from .dialogs import _AddProviderDialog, _load_provider_presets_for_dialog
from .helpers import _progress_slot


class SettingsModelMixin:
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
