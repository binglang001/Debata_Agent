"""设置页 —— 全字段可改 + 即时保存 + 重启提示。

6 节：模型 / 功能 / 渠道 / 角色 / 外观 / 高级。
每个字段改动立即写入磁盘；hot 字段（白名单 / log 级别 / 主题）立即生效；
其它字段标记 needs_restart，顶部状态条提示用户重启 Diana 服务。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
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
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_config.loader import save_config
from app_config.schema import (
    ASRFeatureConfig,
    AgentConfig,
    EmbeddingFeatureConfig,
    LongTermMemoryConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    ReasoningConfig,
    TTSFeatureConfig,
    VisionFeatureConfig,
    WeatherFeatureConfig,
    WebSearchFeatureConfig,
    WhitelistConfig,
)

from ..theme import Spacing
from ..wizard.components import SectionCard, WhitelistEditor, WhitelistState
from ..widgets import FramelessDialog, show_message
from .copy import DASHBOARD_COPY

logger = logging.getLogger(__name__)


# ============================================================
# 状态条：底部「已保存 N 项；需重启 M 项 + 重启按钮」
# ============================================================


class _SaveStatusBar(QFrame):
    """设置页底部状态条。"""

    restart_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        lay.setSpacing(Spacing.MD)

        self._info = QLabel("修改后即时保存。")
        self._info.setProperty("role", "secondary")
        lay.addWidget(self._info)
        lay.addStretch(1)

        self._restart_btn = QPushButton("重启 Diana 服务")
        self._restart_btn.setProperty("role", "primary")
        self._restart_btn.setEnabled(False)
        self._restart_btn.clicked.connect(self.restart_requested.emit)
        lay.addWidget(self._restart_btn)

        self._saved_count = 0
        self._needs_restart = False

    def mark_saved(self, *, needs_restart: bool) -> None:
        self._saved_count += 1
        if needs_restart:
            self._needs_restart = True
        self._render()

    def mark_error(self, msg: str) -> None:
        self._info.setText(f"⚠ {msg}")
        self._info.setProperty("role", "error")
        self._restyle()

    def mark_restart_done(self) -> None:
        self._needs_restart = False
        self._saved_count = 0
        self._info.setText("Diana 服务已重启。")
        self._info.setProperty("role", "success")
        self._restyle()
        self._restart_btn.setEnabled(False)

    def _render(self) -> None:
        if self._needs_restart:
            self._info.setText(f"已即时保存 {self._saved_count} 项 · 部分需重启生效")
            self._info.setProperty("role", "warning")
            self._restart_btn.setEnabled(True)
        else:
            self._info.setText(f"已即时保存 {self._saved_count} 项")
            self._info.setProperty("role", "success")
            self._restart_btn.setEnabled(False)
        self._restyle()

    def _restyle(self) -> None:
        self._info.style().unpolish(self._info)
        self._info.style().polish(self._info)


# ============================================================
# 添加提供商对话框
# ============================================================


class _AddProviderDialog(FramelessDialog):
    """新增 provider 弹窗。"""

    PRESETS = [
        ("deepseek", "DeepSeek", "deepseek-v4-flash"),
        ("anthropic", "Anthropic Claude", "claude-sonnet-4-5"),
        ("openai", "OpenAI", "gpt-4o"),
        ("gemini", "Google Gemini", "gemini-2.0-flash"),
        ("glm", "智谱 GLM", "glm-4.7-flash"),
        ("qwen", "通义千问", "qwen3-plus"),
        ("moonshot", "Moonshot Kimi", "moonshot-v1-8k"),
        ("openrouter", "OpenRouter", "anthropic/claude-sonnet-4-6"),
        ("siliconflow", "硅基流动", "deepseek-ai/DeepSeek-V3"),
        ("volcengine", "火山方舟豆包", "doubao-seed-1-6-vision-250815"),
        ("custom", "自行填一个（自定义）", ""),
    ]

    def __init__(self, existing_ids: set[str], parent: QWidget | None = None) -> None:
        super().__init__("添加提供商", parent)
        self.setMinimumWidth(520)
        self._existing = existing_ids
        self.result_data: dict | None = None

        body = self.body_layout()

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        self._id_edit = QLineEdit()
        self._id_edit.setPlaceholderText("如 deepseek_main、anthropic_alt（仅小写下划线）")
        form.addRow(QLabel("Provider ID"), self._id_edit)

        self._preset_combo = QComboBox()
        for key, label, _ in self.PRESETS:
            self._preset_combo.addItem(label, key)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow(QLabel("preset"), self._preset_combo)

        self._dname_edit = QLineEdit()
        self._dname_edit.setPlaceholderText("显示名（如 DeepSeek、Claude 副号）")
        form.addRow(QLabel("显示名"), self._dname_edit)

        self._base_url_label = QLabel("Base URL")
        self._base_url_edit = QLineEdit()
        self._base_url_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow(self._base_url_label, self._base_url_edit)

        self._model_edit = QLineEdit()
        self._model_edit.setPlaceholderText("模型 ID")
        form.addRow(QLabel("默认模型 ID"), self._model_edit)

        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("API 密钥")
        form.addRow(QLabel("API 密钥"), self._key_edit)

        body.addLayout(form)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setProperty("role", "secondary")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("添加")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        body.addLayout(btn_row)

        self._on_preset_changed(0)

    def _on_preset_changed(self, idx: int) -> None:
        preset = self._preset_combo.itemData(idx) or "deepseek"
        info = next((p for p in self.PRESETS if p[0] == preset), None)
        is_custom = preset == "custom"
        self._base_url_label.setVisible(is_custom)
        self._base_url_edit.setVisible(is_custom)
        if info and info[2]:
            self._model_edit.setText(info[2])
        if info and not self._dname_edit.text():
            self._dname_edit.setText(info[1])
        if not self._id_edit.text():
            # 建议 ID
            suggestion = f"{preset}_main"
            n = 2
            while suggestion in self._existing:
                suggestion = f"{preset}_{n}"
                n += 1
            self._id_edit.setText(suggestion)

    def _on_ok(self) -> None:
        pid = self._id_edit.text().strip()
        preset = self._preset_combo.currentData()
        if not pid or not all(c.isalnum() or c == "_" for c in pid):
            show_message(self, "ID 不合法", "Provider ID 只能含字母数字下划线。")
            return
        if pid in self._existing:
            show_message(self, "ID 重复", f"已经有一个叫 {pid} 的 provider。")
            return
        if preset == "custom" and not self._base_url_edit.text().strip():
            show_message(self, "缺少 Base URL", "自定义模式必须填 Base URL。")
            return
        if not self._model_edit.text().strip():
            show_message(self, "缺少模型 ID", "至少填一个默认模型 ID。")
            return
        if not self._key_edit.text():
            show_message(self, "缺少密钥", "请填 API 密钥（后续可在设置页改）。")
            return
        self.result_data = {
            "id": pid,
            "preset": preset,
            "display_name": self._dname_edit.text().strip() or pid,
            "base_url": self._base_url_edit.text().strip(),
            "model": self._model_edit.text().strip(),
            "api_key": self._key_edit.text(),
        }
        self.accept()


class _VisionEditDialog(FramelessDialog):
    """视觉配置编辑弹窗：provider + model + 可选 key 替换。"""

    DEFAULT_MODELS = {
        "anthropic": "claude-sonnet-4-5",
        "openai": "gpt-4o",
        "gemini": "gemini-2.0-flash",
        "glm": "glm-4v-flash",
        "qwen": "qwen-vl-max",
        "volcengine": "doubao-seed-1-6-vision-250815",
        "openrouter": "anthropic/claude-sonnet-4-6",
    }

    def __init__(self, provider_ids: list[str], provider_presets: dict[str, str],
                 current_provider: str | None, current_model: str,
                 current_key_id: str | None, parent=None) -> None:
        super().__init__("配置视觉（看懂图片）", parent)
        self.setMinimumWidth(520)
        self.result_data: dict | None = None
        self._provider_presets = provider_presets

        body = self.body_layout()
        intro = QLabel(
            "选一个支持视觉的 provider + 填模型 ID。\n"
            "如要换密钥，填到下方密钥框；不填则保留现有。"
        )
        intro.setProperty("role", "secondary")
        intro.setWordWrap(True)
        body.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        self._prov = QComboBox()
        for pid in provider_ids:
            self._prov.addItem(pid, pid)
        if current_provider:
            idx = self._prov.findData(current_provider)
            if idx >= 0:
                self._prov.setCurrentIndex(idx)
        self._prov.currentIndexChanged.connect(self._on_prov_changed)
        form.addRow(QLabel("Provider"), self._prov)

        self._model = QLineEdit(current_model or "")
        self._model.setPlaceholderText("如 doubao-seed-1-6-vision-250815 / glm-4v-flash")
        form.addRow(QLabel("视觉模型 ID"), self._model)

        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText(
            f"留空 = 保留现有（id={current_key_id or '继承 provider'}）；填了就替换"
        )
        form.addRow(QLabel("替换密钥"), self._key)

        body.addLayout(form)

        if not current_model:
            self._on_prov_changed()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setProperty("role", "secondary")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存并启用")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        body.addLayout(btn_row)

    def _on_prov_changed(self) -> None:
        pid = self._prov.currentData()
        preset = self._provider_presets.get(pid, "")
        default = self.DEFAULT_MODELS.get(preset, "")
        if default and not self._model.text().strip():
            self._model.setText(default)

    def _on_ok(self) -> None:
        pid = self._prov.currentData()
        if not pid:
            show_message(self, "缺 provider", "请选一个 provider")
            return
        if not self._model.text().strip():
            show_message(self, "缺模型 ID", "请填视觉模型 ID")
            return
        self.result_data = {
            "provider": pid,
            "model": self._model.text().strip(),
            "api_key": self._key.text(),
        }
        self.accept()


class _WeatherEditDialog(FramelessDialog):
    """天气配置编辑弹窗：host + key 替换。"""

    def __init__(self, current_host: str, current_key_id: str | None, parent=None) -> None:
        super().__init__("配置天气（和风天气）", parent)
        self.setMinimumWidth(520)
        self.result_data: dict | None = None
        self._current_key_id = current_key_id

        body = self.body_layout()
        intro = QLabel(
            "和风天气从 2024 起每个开发者一个独立 API Host。\n"
            "登录 https://console.qweather.com → 项目管理 → 复制「API Host」。"
        )
        intro.setProperty("role", "secondary")
        intro.setWordWrap(True)
        body.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        self._host = QLineEdit(current_host or "")
        self._host.setPlaceholderText("yourdomain.qweatherapi.com")
        form.addRow(QLabel("API Host"), self._host)

        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText(
            f"留空 = 保留现有（id={current_key_id or '未设'}）；填了就替换"
        )
        form.addRow(QLabel("API 密钥"), self._key)

        body.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setProperty("role", "secondary")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存并启用")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        body.addLayout(btn_row)

    def _on_ok(self) -> None:
        host = self._host.text().strip()
        if not host:
            show_message(self, "缺 host", "和风天气 host 必填")
            return
        if not self._current_key_id and not self._key.text():
            show_message(self, "缺密钥", "首次配置需要填 API 密钥")
            return
        self.result_data = {
            "host": host,
            "api_key": self._key.text(),
        }
        self.accept()


# ============================================================
# 主 SettingsPage
# ============================================================


class SettingsPage(QWidget):
    """设置页。每字段即时保存；改完按需重启。"""

    theme_changed = Signal(str)  # "light" / "dark"
    restart_runtime_requested = Signal()  # main.py 接此请求做 runtime hot restart

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        # 控件引用，cfg.providers 变化时刷新所有 agent 的 ComboBox
        self._agent_provider_combos: list[QComboBox] = []
        # 缓存：避免重复 emit
        self._suppress_signals = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        # 占位：稍后挂状态条
        self._status = _SaveStatusBar()
        self._status.restart_requested.connect(self._on_restart_clicked)

        outer.addWidget(self._build_model_section())
        outer.addWidget(self._build_features_section())
        outer.addWidget(self._build_adapter_section())
        outer.addWidget(self._build_persona_section())
        outer.addWidget(self._build_appearance_section())
        outer.addWidget(self._build_advanced_section())

        outer.addWidget(self._status)

    # ============================================================
    # 公共辅助
    # ============================================================

    def _cfg(self):
        return self._runtime.config

    def _save_now(self, *, needs_restart: bool, change_desc: str = "") -> None:
        try:
            save_config(self._runtime.paths, self._cfg())
            if change_desc:
                logger.info(f"设置已保存: {change_desc}")
            self._status.mark_saved(needs_restart=needs_restart)
        except Exception as e:  # noqa: BLE001
            logger.exception("保存设置失败")
            self._status.mark_error(f"保存失败：{e}")

    def _set_secret(self, sid: str, value: str) -> None:
        if not value:
            return
        try:
            self._runtime.secrets.set(sid, value)
        except Exception as e:  # noqa: BLE001
            logger.exception("写入 secrets 失败")
            self._status.mark_error(f"密钥写入失败：{e}")

    def _on_restart_clicked(self) -> None:
        ok = show_message(
            self,
            "重启 Diana 服务",
            "将停止当前 Runtime 并重新启动，使所有需要重启生效的修改生效。\n\n"
            "短暂期间 NapCat 会断开几秒钟，没收到的消息会在重连后补上。",
            confirm_text="重启",
            cancel_text="再想想",
        )
        if ok:
            self.restart_runtime_requested.emit()
            self._status.mark_restart_done()

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
        dlg = _AddProviderDialog(existing, self)
        if dlg.exec() and dlg.result_data:
            data = dlg.result_data
            sid = f"{data['id']}_key"
            self._set_secret(sid, data["api_key"])
            new_p = ProviderConfig(
                preset=None if data["preset"] == "custom" else data["preset"],
                display_name=data["display_name"],
                protocol="openai_compat",
                base_url=data["base_url"] or None,
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
        key_wrap = QWidget()
        key_wrap.setLayout(key_row)
        form.addRow(QLabel("API 密钥"), key_wrap)

        outer.addLayout(form)
        return wrap

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
        model_edit = QLineEdit(agent_cfg.model)
        model_edit.setPlaceholderText("如 deepseek-v4-flash / claude-sonnet-4-5")
        model_edit.editingFinished.connect(
            lambda *_, an=agent_name, e=model_edit: self._on_agent_model_changed(an, e.text().strip())
        )
        form.addRow(QLabel("模型 ID"), model_edit)

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

    def _on_agent_model_changed(self, agent_name: str, model: str) -> None:
        if self._suppress_signals or not model:
            return
        a = getattr(self._cfg().agents, agent_name, None)
        if a is None or a.model == model:
            return
        a.model = model
        self._save_now(needs_restart=True, change_desc=f"agents.{agent_name}.model={model}")

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

    def _build_features_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_features"],
            subtitle="每项功能独立配置。开关即时保存，密钥/配置修改后需重启。",
        )
        card.add_content(self._build_vision_card())
        card.add_content(self._build_weather_card())
        card.add_content(self._build_websearch_card())
        card.add_content(self._build_simple_feature_card(
            "asr", "听懂语音（占位，P3）", "未实装：仅写入开关位",
        ))
        card.add_content(self._build_simple_feature_card(
            "tts", "用声音说话（占位，P3）", "未实装：仅写入开关位",
        ))
        card.add_content(self._build_longterm_memory_card())
        return card

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
                chk.setChecked(not on); v_now.enabled = not on
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
                chk.setChecked(not on); w_now.enabled = not on
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

    def _build_simple_feature_card(self, attr: str, title: str, hint: str) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        feat = getattr(self._cfg().features, attr)
        head = QHBoxLayout()
        chk = QCheckBox(title)
        chk.setChecked(feat.enabled)
        head.addWidget(chk)
        head.addStretch(1)
        outer.addLayout(head)

        h = QLabel(hint)
        h.setProperty("role", "secondary")
        h.setWordWrap(True)
        h.setContentsMargins(24, 0, 0, 0)
        outer.addWidget(h)

        def _on_toggle(on: bool) -> None:
            if self._suppress_signals:
                return
            feat.enabled = on
            self._save_now(needs_restart=True, change_desc=f"features.{attr}.enabled")

        chk.toggled.connect(_on_toggle)
        return wrap

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
        rb_rag = QRadioButton("RAG 向量检索（需 embedding · 当前 P2 占位）")
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

    def _build_adapter_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_adapter"],
            subtitle="NapCat 连接、白名单。白名单立即生效；其它字段改完需重启。",
        )

        cfg = next(iter(self._cfg().adapters.values()))

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        # 模式
        mode_combo = QComboBox()
        mode_combo.addItem("client（程序连 NapCat 正向 WS）", "client")
        mode_combo.addItem("server（程序监听 NapCat 反向连入）", "server")
        idx = mode_combo.findData(cfg.mode)
        if idx >= 0:
            mode_combo.setCurrentIndex(idx)
        mode_combo.currentIndexChanged.connect(
            lambda *_: self._on_adapter_field_changed(cfg, "mode", mode_combo.currentData())
        )
        form.addRow(QLabel("模式"), mode_combo)

        host_edit = QLineEdit(cfg.host)
        host_edit.editingFinished.connect(
            lambda: self._on_adapter_field_changed(cfg, "host", host_edit.text().strip() or "127.0.0.1")
        )
        form.addRow(QLabel("地址"), host_edit)

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(cfg.port)
        port_spin.editingFinished.connect(
            lambda: self._on_adapter_field_changed(cfg, "port", port_spin.value())
        )
        form.addRow(QLabel("端口"), port_spin)

        path_edit = QLineEdit(cfg.path)
        path_edit.editingFinished.connect(
            lambda: self._on_adapter_field_changed(cfg, "path", path_edit.text().strip() or "/")
        )
        form.addRow(QLabel("WebSocket 路径"), path_edit)

        # token 替换
        tok_edit = QLineEdit()
        tok_edit.setEchoMode(QLineEdit.EchoMode.Password)
        tok_edit.setPlaceholderText(
            f"留空 = 保留现有（id={cfg.access_token_id or '未设'}）；填写则替换"
        )
        tok_edit.editingFinished.connect(lambda: self._on_adapter_token_changed(cfg, tok_edit))
        form.addRow(QLabel("Access Token"), tok_edit)

        # 进程托管
        manage_chk = QCheckBox("由 Diana 托管 NapCat 进程")
        manage_chk.setChecked(cfg.manage_process)
        proc_edit = QLineEdit(cfg.process_path)
        proc_edit.setPlaceholderText("如 D:/NapCat/NapCatWinBootMain.exe")
        proc_edit.setVisible(cfg.manage_process)

        def _on_manage(on: bool) -> None:
            if self._suppress_signals:
                return
            proc_edit.setVisible(on)
            cfg.manage_process = on
            self._save_now(needs_restart=True, change_desc="adapter.manage_process")

        manage_chk.toggled.connect(_on_manage)
        proc_edit.editingFinished.connect(
            lambda: self._on_adapter_field_changed(cfg, "process_path", proc_edit.text().strip())
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
        test_btn.clicked.connect(lambda: self._on_test_adapter(cfg))
        test_row.addWidget(test_btn)
        test_row.addWidget(self._adapter_test_status, 1)
        test_wrap = QWidget()
        test_wrap.setLayout(test_row)
        form.addRow(QLabel(""), test_wrap)

        card.add_layout(form)

        # 白名单（hot，立即生效）
        sep = QFrame(); sep.setProperty("role", "separator")
        card.add_content(sep)
        wl_title = QLabel("白名单（立即生效）")
        wl_title.setProperty("role", "title-3")
        card.add_content(wl_title)

        current = WhitelistState(
            mode=cfg.whitelist.mode,
            qq_ids=[str(x) for x in cfg.whitelist.qq_ids],
            group_ids=[str(x) for x in cfg.whitelist.group_ids],
        )
        wl_editor = WhitelistEditor(
            initial=current,
            on_open_confirm=lambda: bool(show_message(
                self, "对所有人开放？",
                "陌生人也能让 Diana 回复，可能产生意外的 API 费用。",
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
            cfg.whitelist = wl
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

    def _on_test_adapter(self, cfg: NapCatAdapterConfig) -> None:
        """复用向导测试逻辑：client 模式真测；server 模式起监听 3s。"""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._adapter_test_status.setText("⚠ 事件循环未就绪")
            return

        self._adapter_test_status.setText("正在测试……")

        async def _do_test() -> None:
            from adapters.napcat.connection import (
                ForwardWSConnection, ReverseWSConnection,
            )
            token = self._runtime.secrets.get(cfg.access_token_id) if cfg.access_token_id else None
            conn = None
            try:
                if cfg.mode == "client":
                    ws_url = f"ws://{cfg.host}:{cfg.port}{cfg.path}"
                    conn = ReverseWSConnection(
                        ws_url=ws_url, access_token=token,
                        reconnect_interval=1.0, max_reconnect_attempts=1,
                        reconnect_backoff_max=1.0, ping_interval=20, ping_timeout=20,
                        initial_connect_timeout=3.0,
                    )
                    await conn.start()
                    for _ in range(8):
                        if conn.is_connected:
                            break
                        await asyncio.sleep(0.25)
                    if conn.is_connected:
                        self._adapter_test_status.setText(f"✓ 已连上 NapCat ({ws_url})")
                    else:
                        self._adapter_test_status.setText("✗ 连不上，检查 NapCat 是否启动 / 地址端口")
                else:
                    conn = ForwardWSConnection(
                        host=cfg.host, port=cfg.port, path=cfg.path,
                        access_token=token, ping_interval=20, ping_timeout=20,
                    )
                    try:
                        await conn.start()
                    except OSError as e:
                        self._adapter_test_status.setText(f"✗ 端口起不来：{e}")
                        return
                    for _ in range(12):
                        if conn.is_connected:
                            break
                        await asyncio.sleep(0.25)
                    if conn.is_connected:
                        self._adapter_test_status.setText(f"✓ NapCat 已连入 ws://{cfg.host}:{cfg.port}{cfg.path}")
                    else:
                        self._adapter_test_status.setText(
                            f"⚠ 端口可用已监听，但 NapCat 暂未连入"
                        )
            except Exception as e:  # noqa: BLE001
                self._adapter_test_status.setText(f"✗ 未能完成：{e}")
            finally:
                if conn is not None:
                    try:
                        await conn.stop()
                    except Exception:  # noqa: BLE001
                        pass

        loop.create_task(_do_test())

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

    def _build_appearance_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_appearance"],
            subtitle="主题切换立即生效。",
        )
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)

        rb_light = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_light"])
        rb_light.setProperty("theme_value", "light")
        rb_dark = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_dark"])
        rb_dark.setProperty("theme_value", "dark")
        self._theme_group.addButton(rb_light)
        self._theme_group.addButton(rb_dark)

        rb_light.toggled.connect(lambda on: on and self.theme_changed.emit("light"))
        rb_dark.toggled.connect(lambda on: on and self.theme_changed.emit("dark"))

        rb_light.setChecked(True)

        row = QHBoxLayout()
        row.addWidget(rb_light)
        row.addWidget(rb_dark)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        card.add_content(wrap)
        return card

    # ============================================================
    # 高级节：行为参数 + 日志级别
    # ============================================================

    def _build_advanced_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_advanced"],
            subtitle="行为参数 / 限速 / 总结阈值 / 日志级别。",
        )
        b = self._cfg().behavior

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        # 合并窗口
        merge_spin = QDoubleSpinBox()
        merge_spin.setRange(0.0, 60.0); merge_spin.setSingleStep(0.5); merge_spin.setValue(b.merge_window_seconds)
        merge_spin.setSuffix(" 秒")
        merge_spin.editingFinished.connect(
            lambda: self._on_behavior_field(b, "merge_window_seconds", merge_spin.value())
        )
        form.addRow(QLabel("消息合并窗口"), merge_spin)

        # 撤回合并
        recall_spin = QDoubleSpinBox()
        recall_spin.setRange(0.0, 60.0); recall_spin.setSingleStep(0.5); recall_spin.setValue(b.recall_merge_window_seconds)
        recall_spin.setSuffix(" 秒")
        recall_spin.editingFinished.connect(
            lambda: self._on_behavior_field(b, "recall_merge_window_seconds", recall_spin.value())
        )
        form.addRow(QLabel("撤回合并窗口"), recall_spin)

        # 主动思考间隔
        proactive_spin = QDoubleSpinBox()
        proactive_spin.setRange(10.0, 86400.0); proactive_spin.setSingleStep(60.0); proactive_spin.setValue(b.proactive_think_interval_seconds)
        proactive_spin.setSuffix(" 秒")
        proactive_spin.editingFinished.connect(
            lambda: self._on_behavior_field(b, "proactive_think_interval_seconds", proactive_spin.value())
        )
        form.addRow(QLabel("主动思考间隔"), proactive_spin)

        # 默认拉历史条数
        hist_spin = QSpinBox()
        hist_spin.setRange(1, 1000); hist_spin.setValue(b.default_history_fetch_count)
        hist_spin.editingFinished.connect(
            lambda: self._on_behavior_field(b, "default_history_fetch_count", hist_spin.value())
        )
        form.addRow(QLabel("默认拉历史条数"), hist_spin)

        # Typing 速度
        chars_spin = QDoubleSpinBox()
        chars_spin.setRange(0.1, 50.0); chars_spin.setSingleStep(0.5); chars_spin.setValue(b.typing.chars_per_second)
        chars_spin.editingFinished.connect(
            lambda: self._on_behavior_nested(b.typing, "chars_per_second", chars_spin.value())
        )
        form.addRow(QLabel("打字速度（字/秒）"), chars_spin)

        # 限速
        rl_chk = QCheckBox("启用速率限制（非好友）")
        rl_chk.setChecked(b.rate_limit.enabled)
        rl_chk.toggled.connect(lambda on: self._on_behavior_nested(b.rate_limit, "enabled", on))
        form.addRow(QLabel("速率限制"), rl_chk)

        rl_window = QSpinBox(); rl_window.setRange(1, 3600); rl_window.setValue(b.rate_limit.window_seconds); rl_window.setSuffix(" 秒")
        rl_window.editingFinished.connect(lambda: self._on_behavior_nested(b.rate_limit, "window_seconds", rl_window.value()))
        form.addRow(QLabel("  窗口"), rl_window)
        rl_max = QSpinBox(); rl_max.setRange(1, 1000); rl_max.setValue(b.rate_limit.max_messages); rl_max.setSuffix(" 条")
        rl_max.editingFinished.connect(lambda: self._on_behavior_nested(b.rate_limit, "max_messages", rl_max.value()))
        form.addRow(QLabel("  最多条数"), rl_max)

        # Summarize
        sum_trigger = QSpinBox(); sum_trigger.setRange(10, 10000); sum_trigger.setValue(b.summarize.trigger_at_messages); sum_trigger.setSuffix(" 条")
        sum_trigger.editingFinished.connect(lambda: self._on_behavior_nested(b.summarize, "trigger_at_messages", sum_trigger.value()))
        form.addRow(QLabel("总结触发条数"), sum_trigger)

        sum_start = QSpinBox(); sum_start.setRange(1, 10000); sum_start.setValue(b.summarize.range_start_messages); sum_start.setSuffix(" 条")
        sum_start.editingFinished.connect(lambda: self._on_behavior_nested(b.summarize, "range_start_messages", sum_start.value()))
        form.addRow(QLabel("  保留下限"), sum_start)
        sum_end = QSpinBox(); sum_end.setRange(1, 10000); sum_end.setValue(b.summarize.range_end_messages); sum_end.setSuffix(" 条")
        sum_end.editingFinished.connect(lambda: self._on_behavior_nested(b.summarize, "range_end_messages", sum_end.value()))
        form.addRow(QLabel("  保留上限"), sum_end)

        # Log level
        log_combo = QComboBox()
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR"):
            log_combo.addItem(lvl, lvl)
        idx = log_combo.findData(self._cfg().app.log_level)
        if idx >= 0:
            log_combo.setCurrentIndex(idx)
        log_combo.currentIndexChanged.connect(lambda *_: self._on_log_level_changed(log_combo.currentData()))
        form.addRow(QLabel("日志级别"), log_combo)

        card.add_layout(form)
        return card

    def _on_behavior_field(self, obj, field: str, value) -> None:
        if self._suppress_signals:
            return
        if getattr(obj, field) == value:
            return
        setattr(obj, field, value)
        self._save_now(needs_restart=True, change_desc=f"behavior.{field}")

    def _on_behavior_nested(self, obj, field: str, value) -> None:
        if self._suppress_signals:
            return
        if getattr(obj, field) == value:
            return
        setattr(obj, field, value)
        self._save_now(needs_restart=True, change_desc=f"behavior.*.{field}")

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
        pass
