"""设置页 —— 全字段可改 + 即时保存 + 重启提示。

6 节：模型 / 功能 / 渠道 / 角色 / 外观 / 高级。
每个字段改动立即写入磁盘；hot 字段（白名单 / log 级别 / 主题）立即生效；
其它字段标记 needs_restart，顶部状态条提示用户重启 Debata 服务。
"""

from __future__ import annotations

import logging
import asyncio
from copy import deepcopy
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
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
from ..widgets.wheel_freeze import install_wheel_freeze
from .copy import DASHBOARD_COPY

logger = logging.getLogger(__name__)


def _path_picker_row(
    edit: QLineEdit,
    *,
    parent: QWidget,
    title: str,
    directory: bool,
    file_filter: str = "所有文件 (*)",
) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(Spacing.SM)
    lay.addWidget(edit, 1)
    btn = QPushButton("浏览")
    btn.setProperty("role", "secondary")

    def _pick() -> None:
        start = edit.text().strip()
        if directory:
            path = QFileDialog.getExistingDirectory(parent, title, start)
        else:
            path, _ = QFileDialog.getOpenFileName(parent, title, start, file_filter)
        if path:
            edit.setText(path)

    btn.clicked.connect(_pick)
    lay.addWidget(btn)
    return row


def _set_form_field_visible(form: QFormLayout, field: QWidget, visible: bool) -> None:
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)
    field.setVisible(visible)


def _format_tool_result_overrides(value: dict[str, int]) -> str:
    return ", ".join(f"{name}={tokens}" for name, tokens in sorted(value.items()))


def _parse_tool_result_overrides(text: str) -> dict[str, int]:
    text = text.strip()
    if not text:
        return {}
    result: dict[str, int] = {}
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("工具软阈值覆盖格式应为 tool=token，用逗号分隔")
        name, raw_value = item.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError("工具名不能为空")
        try:
            tokens = int(raw_value.strip())
        except ValueError as e:
            raise ValueError(f"{name} 的 token 阈值不是整数") from e
        if tokens < 64:
            raise ValueError(f"{name} 的 token 阈值不能小于 64")
        result[name] = tokens
    return result


def _progress_slot(progress: QProgressBar, *, width: int | None = None, height: int = Spacing.SM) -> QWidget:
    """固定进度条占位，避免忙碌动画出现时挤动表单控件。"""
    progress.setFixedHeight(4)
    slot = QWidget()
    slot.setFixedHeight(height)
    if width is not None:
        slot.setFixedWidth(width)
    lay = QVBoxLayout(slot)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addStretch(1)
    lay.addWidget(progress)
    lay.addStretch(1)
    return slot


# ============================================================
# 状态条：底部「已保存 N 项；需重启 M 项 + 重启按钮」
# ============================================================


class _SaveStatusBar(QFrame):
    """设置页底部状态条。改动项数由外部 set_changes() 注入。"""

    restart_requested = Signal()
    restore_requested = Signal()

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

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        lay.addWidget(_progress_slot(self._progress, width=150))

        self._restart_btn = QPushButton("重启 Debata 服务")
        self._restart_btn.setProperty("role", "primary")
        self._restart_btn.setEnabled(False)
        self._restart_btn.clicked.connect(self.restart_requested.emit)
        lay.addWidget(self._restart_btn)

        self._restore_btn = QPushButton("恢复打开时配置")
        self._restore_btn.setProperty("role", "secondary")
        self._restore_btn.setToolTip("撤销本次打开设置页以来已经即时保存的配置改动")
        self._restore_btn.clicked.connect(self.restore_requested.emit)
        lay.addWidget(self._restore_btn)

        self._changed_count = 0
        self._needs_restart = False

    def set_changes(self, count: int, *, needs_restart: bool) -> None:
        self._progress.setVisible(False)
        self._progress.setRange(0, 100)
        self._changed_count = count
        if needs_restart:
            self._needs_restart = True
        self._render()

    def mark_error(self, msg: str) -> None:
        self._progress.setVisible(False)
        self._info.setText(f"⚠ {msg}")
        self._info.setProperty("role", "error")
        self._restyle()

    def mark_busy(self, msg: str) -> None:
        self._info.setText(msg)
        self._info.setProperty("role", "secondary")
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._restart_btn.setEnabled(False)
        self._restyle()

    def mark_restart_done(self) -> None:
        self._progress.setVisible(False)
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._needs_restart = False
        self._changed_count = 0
        self._info.setText("Debata 服务已重启。")
        self._info.setProperty("role", "success")
        self._restyle()
        self._restart_btn.setEnabled(False)

    def _render(self) -> None:
        self._progress.setVisible(False)
        if self._changed_count == 0:
            if self._needs_restart:
                self._info.setText("所有设置已保存 · 部分需重启生效")
                self._info.setProperty("role", "warning")
            else:
                self._info.setText("所有设置与保存时一致。")
                self._info.setProperty("role", "secondary")
            self._restart_btn.setEnabled(self._needs_restart)
        elif self._needs_restart:
            self._info.setText(f"已修改 {self._changed_count} 项 · 部分需重启生效")
            self._info.setProperty("role", "warning")
            self._restart_btn.setEnabled(True)
        else:
            self._info.setText(f"已修改 {self._changed_count} 项（即时生效）")
            self._info.setProperty("role", "success")
            self._restart_btn.setEnabled(False)
        self._restyle()

    def _restyle(self) -> None:
        self._info.style().unpolish(self._info)
        self._info.style().polish(self._info)


# ============================================================
# 添加提供商对话框
# ============================================================

_FALLBACK_PROVIDER_PRESETS = [
    ("deepseek", "DeepSeek", "deepseek-v4-flash"),
    ("anthropic", "Anthropic Claude", "claude-sonnet-4-6"),
    ("openai", "OpenAI", "gpt-5.5"),
]


def _load_provider_presets_for_dialog(presets_dir) -> list[tuple[str, str, str]]:
    """从 providers/presets 加载设置页新增 provider 选项。"""
    try:
        from providers.presets_loader import load_all_presets

        presets = load_all_presets(presets_dir)
        if presets:
            return [
                (
                    pid,
                    preset.display_name,
                    preset.models[0].id if preset.models else "",
                )
                for pid, preset in presets.items()
            ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"加载 provider presets 失败，使用 fallback：{e}")
    return list(_FALLBACK_PROVIDER_PRESETS)


class _AddProviderDialog(FramelessDialog):
    """新增 provider 弹窗。"""

    def __init__(
        self,
        existing_ids: set[str],
        presets: list[tuple[str, str, str]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("添加提供商", parent)
        self.setMinimumWidth(520)
        self._existing = existing_ids
        self._presets = list(presets or _FALLBACK_PROVIDER_PRESETS)
        self._presets.append(("custom", "自行填一个（自定义）", ""))
        self.result_data: dict | None = None

        body = self.body_layout()

        form = QFormLayout()
        self._form = form
        form.setSpacing(Spacing.SM)

        self._id_edit = QLineEdit()
        self._id_edit.setPlaceholderText("如 deepseek_main、anthropic_alt（仅小写下划线）")
        form.addRow(QLabel("Provider ID"), self._id_edit)

        self._preset_combo = QComboBox()
        for key, label, _ in self._presets:
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
        info = next((p for p in self._presets if p[0] == preset), None)
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


class _ASREditDialog(FramelessDialog):
    """ASR 配置编辑弹窗：本地（device/language/model_dir）或 API（provider+key+专有字段）。"""

    _API_PROVIDERS = ["baidu", "xfyun", "volcengine"]

    def __init__(self, feat, parent=None) -> None:
        super().__init__("配置语音识别（ASR）", parent)
        self.setMinimumWidth(520)
        self.result_data: dict | None = None

        body = self.body_layout()
        intro = QLabel("QQ 语音识别现在使用 NapCat 内置转写；此配置仅为兼容旧配置保留。")
        intro.setProperty("role", "secondary")
        intro.setWordWrap(True)
        body.addWidget(intro)

        form = QFormLayout()
        self._form = form
        form.setSpacing(Spacing.SM)

        self._type_combo = QComboBox()
        self._type_combo.addItem("NapCat 内置转写", "local")
        self._type_combo.addItem("云端 API", "api")
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("运行方式"), self._type_combo)

        # 本地
        self._device = QComboBox()
        self._device.addItems(["auto", "cuda", "cpu"])
        if feat.device:
            idx = self._device.findText(feat.device)
            if idx >= 0:
                self._device.setCurrentIndex(idx)
        form.addRow(QLabel("设备"), self._device)

        self._lang = QLineEdit(feat.language or "zh")
        form.addRow(QLabel("默认语言"), self._lang)

        self._model_dir = QLineEdit(feat.model_dir or "")
        self._model_dir.setPlaceholderText("NapCat 内置转写无需模型目录")
        self._model_dir_row = _path_picker_row(
            self._model_dir,
            parent=self,
            title="选择 ASR 模型目录",
            directory=True,
        )
        form.addRow(QLabel("模型目录"), self._model_dir_row)

        # API 模式
        self._prov = QComboBox()
        for p in self._API_PROVIDERS:
            self._prov.addItem(p, p)
        self._prov.currentIndexChanged.connect(self._on_type_changed)
        if feat.provider:
            idx = self._prov.findData(feat.provider)
            if idx >= 0:
                self._prov.setCurrentIndex(idx)
        form.addRow(QLabel("API Provider"), self._prov)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("API Key（留空保留现有）")
        form.addRow(QLabel("API Key"), self._api_key)

        # 百度专有：secret_key
        self._baidu_secret = QLineEdit()
        self._baidu_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._baidu_secret.setText(feat.extra_credentials.get("secret_key", "") if feat.extra_credentials else "")
        self._baidu_secret.setPlaceholderText("百度语音 Secret Key")
        form.addRow(QLabel("Secret Key（百度）"), self._baidu_secret)

        # 讯飞专有：app_id + api_secret
        self._xfyun_appid = QLineEdit()
        self._xfyun_appid.setText(feat.extra_credentials.get("app_id", "") if feat.extra_credentials else "")
        self._xfyun_appid.setPlaceholderText("控制台获取")
        form.addRow(QLabel("App ID（讯飞）"), self._xfyun_appid)
        self._xfyun_secret = QLineEdit()
        self._xfyun_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._xfyun_secret.setText(feat.extra_credentials.get("api_secret", "") if feat.extra_credentials else "")
        self._xfyun_secret.setPlaceholderText("API Secret（讯飞）")
        form.addRow(QLabel("API Secret（讯飞）"), self._xfyun_secret)

        # 火山引擎专有：app_id
        self._volc_appid = QLineEdit()
        self._volc_appid.setText(feat.extra_credentials.get("app_id", "") if feat.extra_credentials else "")
        self._volc_appid.setPlaceholderText("火山引擎 App ID")
        form.addRow(QLabel("App ID（火山）"), self._volc_appid)

        body.addLayout(form)

        idx_t = self._type_combo.findData(feat.type or "local")
        if idx_t >= 0:
            self._type_combo.setCurrentIndex(idx_t)
        self._on_type_changed()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setProperty("role", "secondary")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        body.addLayout(btn_row)

    def _on_type_changed(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        _set_form_field_visible(self._form, self._device, is_local)
        _set_form_field_visible(self._form, self._lang, is_local)
        _set_form_field_visible(self._form, self._model_dir_row, is_local)
        is_api = not is_local
        _set_form_field_visible(self._form, self._prov, is_api)
        _set_form_field_visible(self._form, self._api_key, is_api)
        # 专有字段按 provider 显示
        prov = self._prov.currentData() if is_api else ""
        _set_form_field_visible(self._form, self._baidu_secret, is_api and prov == "baidu")
        _set_form_field_visible(self._form, self._xfyun_appid, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._xfyun_secret, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._volc_appid, is_api and prov == "volcengine")

    def _on_ok(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        extra: dict[str, str] = {}
        if not is_local:
            prov = self._prov.currentData()
            if prov == "baidu":
                extra["secret_key"] = self._baidu_secret.text().strip()
            elif prov == "xfyun":
                extra["app_id"] = self._xfyun_appid.text().strip()
                extra["api_secret"] = self._xfyun_secret.text().strip()
            elif prov == "volcengine":
                extra["app_id"] = self._volc_appid.text().strip()
        self.result_data = {
            "type": "local" if is_local else "api",
            "device": self._device.currentText(),
            "language": self._lang.text().strip() or "zh",
            "model_dir": self._model_dir.text().strip(),
            "provider": self._prov.currentData() if not is_local else None,
            "api_key": self._api_key.text(),
            "extra_credentials": extra,
        }
        self.accept()


class _TTSEditDialog(FramelessDialog):
    """TTS 配置编辑弹窗：本地（可选参考音频/语气/目录）或 API（provider+key+专有字段）。"""

    _API_PROVIDERS = ["baidu", "xfyun", "volcengine"]

    def __init__(self, feat, parent=None) -> None:
        super().__init__("配置语音合成（TTS）", parent)
        self.setMinimumWidth(520)
        self.result_data: dict | None = None

        body = self.body_layout()
        intro = QLabel("本地模式用 VoxCPM2 合成语音：可只填音色/语气描述，也可加参考音频做音色克隆；API 模式用百度/讯飞/火山引擎云端服务。")
        intro.setProperty("role", "secondary")
        intro.setWordWrap(True)
        body.addWidget(intro)

        form = QFormLayout()
        self._form = form
        form.setSpacing(Spacing.SM)

        self._type_combo = QComboBox()
        self._type_combo.addItem("本地（推荐 · VoxCPM2）", "local")
        self._type_combo.addItem("云端 API", "api")
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("运行方式"), self._type_combo)

        # 本地
        self._device = QComboBox()
        self._device.addItems(["auto", "cuda", "cpu"])
        idx = self._device.findText(getattr(feat, "device", "auto") or "auto")
        if idx >= 0:
            self._device.setCurrentIndex(idx)
        form.addRow(QLabel("设备"), self._device)

        self._ref_audio = QLineEdit(feat.reference_audio or "")
        self._ref_audio.setPlaceholderText("可选：data/models/VoxCPM2/ref.wav")
        self._ref_audio_row = _path_picker_row(
            self._ref_audio,
            parent=self,
            title="选择 TTS 参考音频",
            directory=False,
            file_filter="音频文件 (*.wav *.mp3 *.flac *.m4a *.ogg);;所有文件 (*)",
        )
        form.addRow(QLabel("参考音频（可选）"), self._ref_audio_row)

        self._prompt = QLineEdit(feat.default_prompt or "")
        self._prompt.setPlaceholderText("可选，如「年轻女性，温柔语气」")
        form.addRow(QLabel("默认音色/语气"), self._prompt)

        self._model_dir = QLineEdit(feat.model_dir or "data/models/VoxCPM2")
        self._model_dir_row = _path_picker_row(
            self._model_dir,
            parent=self,
            title="选择 TTS 模型目录",
            directory=True,
        )
        form.addRow(QLabel("模型目录"), self._model_dir_row)

        self._load_denoiser = QCheckBox("启用")
        self._load_denoiser.setChecked(bool(getattr(feat, "load_denoiser", False)))
        self._load_denoiser.setToolTip("默认关闭；开启前请确认降噪模型已手动准备好，否则 VoxCPM2 可能尝试额外下载。")
        form.addRow(QLabel("降噪器"), self._load_denoiser)

        self._cfg_value = QDoubleSpinBox()
        self._cfg_value.setRange(0.1, 20.0)
        self._cfg_value.setSingleStep(0.1)
        self._cfg_value.setValue(float(getattr(feat, "cfg_value", 2.0)))
        form.addRow(QLabel("CFG 强度"), self._cfg_value)

        self._timesteps = QSpinBox()
        self._timesteps.setRange(1, 100)
        self._timesteps.setValue(int(getattr(feat, "inference_timesteps", 10)))
        form.addRow(QLabel("推理步数"), self._timesteps)

        # API 模式
        self._prov = QComboBox()
        for p in self._API_PROVIDERS:
            self._prov.addItem(p, p)
        self._prov.currentIndexChanged.connect(self._on_type_changed)
        if feat.provider:
            idx = self._prov.findData(feat.provider)
            if idx >= 0:
                self._prov.setCurrentIndex(idx)
        form.addRow(QLabel("API Provider"), self._prov)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("API Key（留空保留现有）")
        form.addRow(QLabel("API Key"), self._api_key)

        # 专有字段
        self._baidu_secret = QLineEdit()
        self._baidu_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._baidu_secret.setText(feat.extra_credentials.get("secret_key", "") if feat.extra_credentials else "")
        self._baidu_secret.setPlaceholderText("百度语音 Secret Key")
        form.addRow(QLabel("Secret Key（百度）"), self._baidu_secret)

        self._xfyun_appid = QLineEdit()
        self._xfyun_appid.setText(feat.extra_credentials.get("app_id", "") if feat.extra_credentials else "")
        self._xfyun_appid.setPlaceholderText("控制台获取")
        form.addRow(QLabel("App ID（讯飞）"), self._xfyun_appid)
        self._xfyun_secret = QLineEdit()
        self._xfyun_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._xfyun_secret.setText(feat.extra_credentials.get("api_secret", "") if feat.extra_credentials else "")
        self._xfyun_secret.setPlaceholderText("API Secret（讯飞）")
        form.addRow(QLabel("API Secret（讯飞）"), self._xfyun_secret)

        self._volc_appid = QLineEdit()
        self._volc_appid.setText(feat.extra_credentials.get("app_id", "") if feat.extra_credentials else "")
        self._volc_appid.setPlaceholderText("火山引擎 App ID")
        form.addRow(QLabel("App ID（火山）"), self._volc_appid)

        body.addLayout(form)

        idx_t = self._type_combo.findData(feat.type or "local")
        if idx_t >= 0:
            self._type_combo.setCurrentIndex(idx_t)
        self._on_type_changed()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setProperty("role", "secondary")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        body.addLayout(btn_row)

    def _on_type_changed(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        _set_form_field_visible(self._form, self._device, is_local)
        _set_form_field_visible(self._form, self._ref_audio_row, is_local)
        _set_form_field_visible(self._form, self._prompt, is_local)
        _set_form_field_visible(self._form, self._model_dir_row, is_local)
        _set_form_field_visible(self._form, self._load_denoiser, is_local)
        _set_form_field_visible(self._form, self._cfg_value, is_local)
        _set_form_field_visible(self._form, self._timesteps, is_local)
        is_api = not is_local
        _set_form_field_visible(self._form, self._prov, is_api)
        _set_form_field_visible(self._form, self._api_key, is_api)
        prov = self._prov.currentData() if is_api else ""
        _set_form_field_visible(self._form, self._baidu_secret, is_api and prov == "baidu")
        _set_form_field_visible(self._form, self._xfyun_appid, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._xfyun_secret, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._volc_appid, is_api and prov == "volcengine")

    def _on_ok(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        extra: dict[str, str] = {}
        if not is_local:
            prov = self._prov.currentData()
            if prov == "baidu":
                extra["secret_key"] = self._baidu_secret.text().strip()
            elif prov == "xfyun":
                extra["app_id"] = self._xfyun_appid.text().strip()
                extra["api_secret"] = self._xfyun_secret.text().strip()
            elif prov == "volcengine":
                extra["app_id"] = self._volc_appid.text().strip()
        self.result_data = {
            "type": "local" if is_local else "api",
            "device": self._device.currentText(),
            "reference_audio": self._ref_audio.text().strip(),
            "default_prompt": self._prompt.text().strip(),
            "model_dir": self._model_dir.text().strip() or "data/models/VoxCPM2",
            "load_denoiser": self._load_denoiser.isChecked(),
            "cfg_value": self._cfg_value.value(),
            "inference_timesteps": self._timesteps.value(),
            "provider": self._prov.currentData() if not is_local else None,
            "api_key": self._api_key.text(),
            "extra_credentials": extra,
        }
        self.accept()


class _EmbeddingEditDialog(FramelessDialog):
    """Embedding 配置编辑弹窗：API（选 provider + 模型 + 密钥）或本地（quality + 目录）。"""

    def __init__(self, provider_ids: list[str], emb, parent=None) -> None:
        super().__init__("配置 Embedding（RAG 向量检索）", parent)
        self.setMinimumWidth(520)
        self.result_data: dict | None = None

        body = self.body_layout()
        intro = QLabel("Embedding 把文本转成数学向量用于语义检索。API 模式复用已有 provider 的 /embeddings 端点，本地模式完全离线。")
        intro.setProperty("role", "secondary")
        intro.setWordWrap(True)
        body.addWidget(intro)

        form = QFormLayout()
        self._form = form
        form.setSpacing(Spacing.SM)

        # type
        self._type_combo = QComboBox()
        self._type_combo.addItem("云端 API（推荐 · 复用已有 provider）", "api")
        self._type_combo.addItem("本地 sentence-transformers", "local")
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("Embedding 来源"), self._type_combo)

        # API 模式
        self._prov = QComboBox()
        for pid in provider_ids:
            self._prov.addItem(pid, pid)
        if emb.provider and emb.type == "api":
            idx = self._prov.findData(emb.provider)
            if idx >= 0:
                self._prov.setCurrentIndex(idx)
        form.addRow(QLabel("Provider"), self._prov)

        self._model = QLineEdit(emb.api_model or "")
        self._model.setPlaceholderText("如 text-embedding-v4 / embedding-3 / doubao-embedding-text-240715")
        form.addRow(QLabel("模型 ID"), self._model)

        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("留空保留现有；填了替换密钥")
        form.addRow(QLabel("API 密钥"), self._key)

        # 本地模式
        self._local_quality = QComboBox()
        self._local_quality.addItem("高性能（all-MiniLM-L6-v2 · 80MB · 英文为主）", "performance")
        self._local_quality.addItem("高质量（bge-large-zh-v1.5 · 1.3GB · 中文最佳）", "quality")
        self._local_quality.currentIndexChanged.connect(self._on_quality_changed)
        if emb.local_quality == "quality":
            self._local_quality.setCurrentIndex(1)
        form.addRow(QLabel("模型选择"), self._local_quality)

        self._local_dir = QLineEdit(emb.local_model_dir or self._default_local_dir())
        self._local_dir.setPlaceholderText("data/models/embedding/bge-large-zh-v1.5")
        self._local_dir_row = _path_picker_row(
            self._local_dir,
            parent=self,
            title="选择 Embedding 模型目录",
            directory=True,
        )
        form.addRow(QLabel("模型目录"), self._local_dir_row)

        body.addLayout(form)

        self._warning = QLabel("")
        self._warning.setProperty("role", "warning")
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        body.addWidget(self._warning)

        local_actions = QHBoxLayout()
        self._download_btn = QPushButton("安装指引")
        self._download_btn.setProperty("role", "secondary")
        self._download_btn.clicked.connect(self._on_download)
        local_actions.addWidget(self._download_btn)
        local_actions.addStretch(1)
        body.addLayout(local_actions)

        # 初始化 visibility
        if emb.type:
            idx_t = self._type_combo.findData(emb.type)
            if idx_t >= 0:
                self._type_combo.setCurrentIndex(idx_t)
        self._on_type_changed()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setProperty("role", "secondary")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setProperty("role", "primary")
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        body.addLayout(btn_row)

    def _on_type_changed(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        _set_form_field_visible(self._form, self._prov, not is_local)
        _set_form_field_visible(self._form, self._model, not is_local)
        _set_form_field_visible(self._form, self._key, not is_local)
        _set_form_field_visible(self._form, self._local_quality, is_local)
        _set_form_field_visible(self._form, self._local_dir_row, is_local)
        self._download_btn.setVisible(is_local)
        self._check_local_model()

    def _default_local_dir(self) -> str:
        return (
            "data/models/embedding/bge-large-zh-v1.5"
            if self._local_quality.currentData() == "quality"
            else "data/models/embedding/all-MiniLM-L6-v2"
        )

    def _on_quality_changed(self, *_args) -> None:
        current = self._local_dir.text().strip()
        known = {
            "data/models/embedding/all-MiniLM-L6-v2",
            "data/models/embedding/bge-large-zh-v1.5",
        }
        if not current or current in known:
            self._local_dir.setText(self._default_local_dir())
        self._check_local_model()

    def _check_local_model(self) -> bool:
        if self._type_combo.currentData() != "local":
            self._warning.setVisible(False)
            return True
        d = self._local_dir.text().strip() or self._default_local_dir()
        from ui.wizard.step_views.features import _directory_has_files

        ok = _directory_has_files(d)
        if ok:
            self._warning.setVisible(False)
        else:
            self._warning.setText(f"⚠ 模型目录未就绪：{d}")
            self._warning.setVisible(True)
        return ok

    def _on_download(self) -> None:
        from ui.wizard.step_views.features import _start_plugin_download

        quality = self._local_quality.currentData() or "performance"
        if quality == "quality":
            plugin_name = "embedding_bge_zh"
            display_name = "bge-large-zh-v1.5 中文向量模型"
        else:
            plugin_name = "embedding_minilm"
            display_name = "all-MiniLM-L6-v2 向量模型"
        _start_plugin_download(
            self,
            plugin_name,
            plugin_name,
            display_name,
            on_finished=self._check_local_model,
        )

    def _on_ok(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        if not is_local:
            pid = self._prov.currentData()
            if not pid:
                show_message(self, "缺 provider", "请选一个 provider")
                return
            if not self._model.text().strip():
                show_message(self, "缺模型 ID", "请填 embedding 模型 ID")
                return
            self.result_data = {
                "type": "api",
                "provider": pid,
                "model": self._model.text().strip(),
                "api_key": self._key.text(),
            }
        else:
            self.result_data = {
                "type": "local",
                "local_quality": self._local_quality.currentData() or "performance",
                "local_model_dir": self._local_dir.text().strip() or self._default_local_dir(),
            }
        self.accept()


class _VisionEditDialog(FramelessDialog):
    """视觉配置编辑弹窗：provider + model + 可选 key 替换。"""

    DEFAULT_MODELS = {
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-5.5",
        "gemini": "gemini-3-pro",
        "glm": "glm-5v-turbo",
        "qwen": "qwen3.6-plus",
        "volcengine": "doubao-seed-2-0-lite-260428",
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
        self._model.setPlaceholderText("如 doubao-seed-2-0-lite-260428 / glm-5v-turbo / gpt-5.5")
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

    theme_changed = Signal(str)  # "auto" / "light" / "dark"
    restart_runtime_requested = Signal()  # main.py 接此请求做 runtime hot restart

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._agent_provider_combos: list[QComboBox] = []
        self._suppress_signals = False
        # 基线配置快照（深拷贝），用于比对改动项数
        self._baseline = deepcopy(self._cfg())
        self._opened_snapshot = deepcopy(self._cfg())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 内部滚动区（所有 section）
        from PySide6.QtWidgets import QScrollArea
        inner_scroll = QScrollArea()
        inner_scroll.setWidgetResizable(True)
        inner_scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        sections = QWidget()
        sections_lay = QVBoxLayout(sections)
        sections_lay.setContentsMargins(0, 0, 0, 0)
        sections_lay.setSpacing(Spacing.MD)

        sections_lay.addWidget(self._build_model_section())
        sections_lay.addWidget(self._build_features_section())
        # adapter 节稍后通过 _rebuild_adapter_form 动态重建
        self._adapter_container = QVBoxLayout()
        self._adapter_container.setContentsMargins(0, 0, 0, 0)
        self._adapter_container.setSpacing(Spacing.MD)
        adapter_wrap = QWidget()
        adapter_wrap.setLayout(self._adapter_container)
        sections_lay.addWidget(adapter_wrap)
        sections_lay.addWidget(self._build_persona_section())
        sections_lay.addWidget(self._build_emoji_section())
        sections_lay.addWidget(self._build_appearance_section())
        sections_lay.addWidget(self._build_memory_section())
        sections_lay.addWidget(self._build_advanced_section())

        inner_scroll.setWidget(sections)
        outer.addWidget(inner_scroll, 1)

        # 底部状态条（始终可见，不滚动）
        self._status = _SaveStatusBar()
        self._status.restart_requested.connect(self._on_restart_clicked)
        self._status.restore_requested.connect(self._restore_opened_config)
        outer.addWidget(self._status)

        # 初始化 adapter 表单
        self._rebuild_adapter_form()

        # 滚轮冻结
        self._wheel_freeze_filter = install_wheel_freeze(self)

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
            self._baseline = deepcopy(self._cfg())
            self._status.set_changes(self._count_changes(), needs_restart=needs_restart)
        except Exception as e:  # noqa: BLE001
            logger.exception("保存设置失败")
            self._status.mark_error(f"保存失败：{e}")

    def _restore_opened_config(self) -> None:
        try:
            self._runtime.config = deepcopy(self._opened_snapshot)
            save_config(self._runtime.paths, self._runtime.config)
            self._baseline = deepcopy(self._runtime.config)
            self._status.set_changes(0, needs_restart=True)
            self.refresh()
            logger.info("设置已恢复到打开设置页时的配置")
        except Exception as e:  # noqa: BLE001
            logger.exception("恢复设置失败")
            self._status.mark_error(f"恢复失败：{e}")

    def _count_changes(self) -> int:
        """比对当前配置与基线，返回字段级差异数。"""
        try:
            import json
            cur = json.loads(self._cfg().model_dump_json(exclude_none=True))
            base = json.loads(self._baseline.model_dump_json(exclude_none=True))
        except Exception:
            return 0

        missing = object()

        def _leaf_count(value) -> int:
            if value is missing:
                return 0
            if isinstance(value, dict):
                return sum(_leaf_count(v) for v in value.values()) or 1
            if isinstance(value, list):
                return sum(_leaf_count(v) for v in value) or 1
            return 1

        def _diff(a, b) -> int:
            if a == b:
                return 0
            if a is missing:
                return _leaf_count(b)
            if b is missing:
                return _leaf_count(a)
            if isinstance(a, dict) and isinstance(b, dict):
                n = 0
                for k in set(a.keys()) | set(b.keys()):
                    n += _diff(a.get(k, missing), b.get(k, missing))
                return n
            if isinstance(a, list) and isinstance(b, list):
                n = 0
                for i in range(max(len(a), len(b))):
                    va = a[i] if i < len(a) else missing
                    vb = b[i] if i < len(b) else missing
                    n += _diff(va, vb)
                return n
            return 1

        return _diff(cur, base)

    def _set_secret(self, sid: str, value: str) -> None:
        if not value:
            return
        try:
            self._runtime.secrets.set(sid, value)
        except Exception as e:  # noqa: BLE001
            logger.exception("写入 secrets 失败")
            self._status.mark_error(f"密钥写入失败：{e}")

    def _on_restart_clicked(self) -> None:
        app = QApplication.instance()
        focus = app.focusWidget() if app is not None else None
        if focus is not None:
            focus.clearFocus()
        ok = show_message(
            self,
            "重启 Debata 服务",
            "将停止当前 Runtime 并重新启动，使所有需要重启生效的修改生效。\n\n"
            "短暂期间 NapCat 会断开几秒钟，没收到的消息会在重连后补上。",
            confirm_text="重启",
            cancel_text="再想想",
        )
        if ok:
            self._status.mark_busy("正在重启 Debata 服务……")
            self.restart_runtime_requested.emit()

    def on_runtime_restart_finished(self, ok: bool, message: str = "") -> None:
        if ok:
            self._status.mark_restart_done()
            return
        self._status.mark_error(message or "重启失败，请查看日志。")

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
            return "启动检测中或尚无检测结果"
        if getattr(item, "status", "") == "ok":
            latency = getattr(item, "latency_ms", 0)
            return f"可用" + (f" · {latency}ms" if latency else "")
        return getattr(item, "message", "无响应")

    def _agent_model_for_provider(self, provider_name: str) -> str:
        for _agent_name, agent in self._cfg()._iter_agents():
            if agent.provider == provider_name:
                return agent.model
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
                        status.setText("没有 Agent 或 RAG 使用该 provider，无法自动选择模型")
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
        model_edit = QLineEdit(agent_cfg.model)
        model_edit.setPlaceholderText("如 deepseek-v4-flash / claude-sonnet-4-6")
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
        card.add_content(self._build_tts_card())
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
        chk = QCheckBox("用声音说话（TTS · VoxCPM2）")
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
            if data.get("api_key"):
                sid = t.api_key_id or "tts_key"
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
        return f"API · provider={t.provider or '?'}"

    def _build_embedding_card(self) -> QWidget:
        """RAG embedding 配置卡片。"""
        wrap = QFrame()
        wrap.setObjectName("Card")
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        emb = self._cfg().features.embedding
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
        mode_combo.addItem("server（程序监听 NapCat 反向连入）", "server")
        idx = mode_combo.findData(_cfg().mode)
        if idx >= 0:
            mode_combo.setCurrentIndex(idx)
        mode_combo.currentIndexChanged.connect(
            lambda *_: self._on_adapter_field_changed(_cfg(), "mode", mode_combo.currentData())
        )
        form.addRow(QLabel("模式"), mode_combo)

        host_edit = QLineEdit(_cfg().host)
        host_edit.editingFinished.connect(
            lambda h=host_edit: self._on_adapter_field_changed(_cfg(), "host", h.text().strip() or "127.0.0.1")
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

        # 白名单（hot，立即生效）
        sep = QFrame(); sep.setProperty("role", "separator")
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
        """复用向导测试逻辑：client 模式真测；server 模式起监听 3s。"""
        import asyncio
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
                self._adapter_test_progress.setRange(0, 100)
                self._adapter_test_progress.setValue(100)
                self._adapter_test_progress.setVisible(False)
                if button is not None:
                    button.setEnabled(True)
                    button.setText("测试连接")

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
            self.theme_changed.emit(target)
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
            lambda: self._on_behavior_field("merge_window_seconds", merge_spin.value())
        )
        form.addRow(QLabel("消息合并窗口"), merge_spin)

        # 撤回合并
        recall_spin = QDoubleSpinBox()
        recall_spin.setRange(0.0, 60.0); recall_spin.setSingleStep(0.5); recall_spin.setValue(b.recall_merge_window_seconds)
        recall_spin.setSuffix(" 秒")
        recall_spin.editingFinished.connect(
            lambda: self._on_behavior_field("recall_merge_window_seconds", recall_spin.value())
        )
        form.addRow(QLabel("撤回合并窗口"), recall_spin)

        # 主动思考间隔
        proactive_spin = QDoubleSpinBox()
        proactive_spin.setRange(10.0, 86400.0); proactive_spin.setSingleStep(60.0); proactive_spin.setValue(b.proactive_think_interval_seconds)
        proactive_spin.setSuffix(" 秒")
        proactive_spin.editingFinished.connect(
            lambda: self._on_behavior_field("proactive_think_interval_seconds", proactive_spin.value())
        )
        form.addRow(QLabel("主动思考间隔"), proactive_spin)

        # 默认拉历史条数
        hist_spin = QSpinBox()
        hist_spin.setRange(1, 1000); hist_spin.setValue(b.default_history_fetch_count)
        hist_spin.editingFinished.connect(
            lambda: self._on_behavior_field("default_history_fetch_count", hist_spin.value())
        )
        form.addRow(QLabel("默认拉历史条数"), hist_spin)

        # Typing 速度
        chars_spin = QDoubleSpinBox()
        chars_spin.setRange(0.1, 50.0); chars_spin.setSingleStep(0.5); chars_spin.setValue(b.typing.chars_per_second)
        chars_spin.editingFinished.connect(
            lambda: self._on_behavior_nested("typing", "chars_per_second", chars_spin.value())
        )
        form.addRow(QLabel("打字速度（字/秒）"), chars_spin)

        # 限速
        rl_chk = QCheckBox("启用速率限制（非好友）")
        rl_chk.setChecked(b.rate_limit.enabled)
        rl_chk.toggled.connect(lambda on: self._on_behavior_nested("rate_limit", "enabled", on))
        form.addRow(QLabel("速率限制"), rl_chk)

        rl_window = QSpinBox(); rl_window.setRange(1, 3600); rl_window.setValue(b.rate_limit.window_seconds); rl_window.setSuffix(" 秒")
        rl_window.editingFinished.connect(lambda: self._on_behavior_nested("rate_limit", "window_seconds", rl_window.value()))
        form.addRow(QLabel("  窗口"), rl_window)
        rl_max = QSpinBox(); rl_max.setRange(1, 1000); rl_max.setValue(b.rate_limit.max_messages); rl_max.setSuffix(" 条")
        rl_max.editingFinished.connect(lambda: self._on_behavior_nested("rate_limit", "max_messages", rl_max.value()))
        form.addRow(QLabel("  最多条数"), rl_max)

        # Summarize
        sum_trigger = QSpinBox(); sum_trigger.setRange(10, 10000); sum_trigger.setValue(b.summarize.trigger_at_messages); sum_trigger.setSuffix(" 条")
        sum_trigger.editingFinished.connect(lambda: self._on_behavior_nested("summarize", "trigger_at_messages", sum_trigger.value()))
        form.addRow(QLabel("总结触发条数"), sum_trigger)

        sum_start = QSpinBox(); sum_start.setRange(1, 10000); sum_start.setValue(b.summarize.range_start_messages); sum_start.setSuffix(" 条")
        sum_start.editingFinished.connect(lambda: self._on_behavior_nested("summarize", "range_start_messages", sum_start.value()))
        form.addRow(QLabel("  保留下限"), sum_start)
        sum_end = QSpinBox(); sum_end.setRange(1, 10000); sum_end.setValue(b.summarize.range_end_messages); sum_end.setSuffix(" 条")
        sum_end.editingFinished.connect(lambda: self._on_behavior_nested("summarize", "range_end_messages", sum_end.value()))
        form.addRow(QLabel("  保留上限"), sum_end)

        sum_trigger_tokens = QSpinBox()
        sum_trigger_tokens.setRange(0, 1_000_000)
        sum_trigger_tokens.setValue(b.summarize.trigger_at_tokens or 0)
        sum_trigger_tokens.setSuffix(" token")
        sum_trigger_tokens.setSpecialValueText("自动")
        sum_trigger_tokens.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "summarize",
                "trigger_at_tokens",
                sum_trigger_tokens.value() or None,
            )
        )
        form.addRow(QLabel("总结触发 token"), sum_trigger_tokens)

        sum_target_tokens = QSpinBox()
        sum_target_tokens.setRange(0, 1_000_000)
        sum_target_tokens.setValue(b.summarize.target_after_tokens or 0)
        sum_target_tokens.setSuffix(" token")
        sum_target_tokens.setSpecialValueText("自动")
        sum_target_tokens.editingFinished.connect(
            lambda: self._on_behavior_nested(
                "summarize",
                "target_after_tokens",
                sum_target_tokens.value() or None,
            )
        )
        form.addRow(QLabel("压缩后目标 token"), sum_target_tokens)

        context_title = QLabel("上下文预算")
        context_title.setProperty("role", "title-3")
        form.addRow(context_title)

        ctx_max = QSpinBox()
        ctx_max.setRange(0, 1_000_000)
        ctx_max.setValue(b.context.max_context_tokens or 0)
        ctx_max.setSuffix(" token")
        ctx_max.setSpecialValueText("按模型自动")
        ctx_max.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "max_context_tokens", ctx_max.value() or None)
        )
        form.addRow(QLabel("工作上下文"), ctx_max)

        ctx_reserve = QSpinBox()
        ctx_reserve.setRange(1024, 500_000)
        ctx_reserve.setValue(b.context.reserve_output_tokens)
        ctx_reserve.setSuffix(" token")
        ctx_reserve.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "reserve_output_tokens", ctx_reserve.value())
        )
        form.addRow(QLabel("输出预留"), ctx_reserve)

        ctx_mem = QSpinBox()
        ctx_mem.setRange(256, 100_000)
        ctx_mem.setValue(b.context.memory_token_budget)
        ctx_mem.setSuffix(" token")
        ctx_mem.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "memory_token_budget", ctx_mem.value())
        )
        form.addRow(QLabel("重要记忆预算"), ctx_mem)

        ctx_sum = QSpinBox()
        ctx_sum.setRange(256, 100_000)
        ctx_sum.setValue(b.context.summary_token_budget)
        ctx_sum.setSuffix(" token")
        ctx_sum.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "summary_token_budget", ctx_sum.value())
        )
        form.addRow(QLabel("滚动摘要预算"), ctx_sum)

        ctx_tool_soft = QSpinBox()
        ctx_tool_soft.setRange(64, 100_000)
        ctx_tool_soft.setValue(b.context.tool_result_soft_limit_tokens)
        ctx_tool_soft.setSuffix(" token")
        ctx_tool_soft.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "tool_result_soft_limit_tokens", ctx_tool_soft.value())
        )
        form.addRow(QLabel("工具结果软阈值"), ctx_tool_soft)

        ctx_tool_hard = QSpinBox()
        ctx_tool_hard.setRange(128, 200_000)
        ctx_tool_hard.setValue(b.context.tool_result_hard_cap_tokens)
        ctx_tool_hard.setSuffix(" token")
        ctx_tool_hard.editingFinished.connect(
            lambda: self._on_behavior_nested("context", "tool_result_hard_cap_tokens", ctx_tool_hard.value())
        )
        form.addRow(QLabel("工具结果硬上限"), ctx_tool_hard)

        ctx_tool_overrides = QLineEdit()
        ctx_tool_overrides.setPlaceholderText("describe_image=900, read_file=2000")
        ctx_tool_overrides.setText(_format_tool_result_overrides(b.context.tool_result_soft_overrides))
        ctx_tool_overrides.editingFinished.connect(
            lambda: self._on_tool_result_overrides(ctx_tool_overrides.text())
        )
        form.addRow(QLabel("工具软阈值覆盖"), ctx_tool_overrides)

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

    # hot 字段（立即生效，无需重启）
    _HOT_FIELDS = {
        "chars_per_second", "max_delay_seconds",
        "window_seconds", "max_messages", "enabled",
        "trigger_at_messages", "range_start_messages", "range_end_messages",
        "trigger_at_tokens", "target_after_tokens",
        "max_context_tokens", "reserve_output_tokens", "memory_token_budget", "summary_token_budget",
        "tool_result_soft_limit_tokens", "tool_result_hard_cap_tokens", "tool_result_soft_overrides",
        "default_history_fetch_count",
    }

    def _on_behavior_field(self, field: str, value) -> None:
        if self._suppress_signals:
            return
        obj = self._cfg().behavior
        if getattr(obj, field) == value:
            return
        setattr(obj, field, value)
        needs = not (field in self._HOT_FIELDS)
        self._save_now(needs_restart=needs, change_desc=f"behavior.{field}")

    def _on_behavior_nested(self, section: str, field: str, value) -> None:
        if self._suppress_signals:
            return
        obj = getattr(self._cfg().behavior, section)
        if getattr(obj, field) == value:
            return
        setattr(obj, field, value)
        needs = not (field in self._HOT_FIELDS)
        self._save_now(needs_restart=needs, change_desc=f"behavior.{section}.{field}")

    def _on_tool_result_overrides(self, text: str) -> None:
        try:
            value = _parse_tool_result_overrides(text)
        except ValueError as e:
            self._status.mark_error(str(e))
            return
        self._on_behavior_nested("context", "tool_result_soft_overrides", value)

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
