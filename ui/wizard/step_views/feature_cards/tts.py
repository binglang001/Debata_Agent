"""TTS feature card."""

from __future__ import annotations

from importlib import import_module

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ....theme import Spacing
from ...copy import COPY


def _features_module():
    # Tests monkeypatch helper names on ui.wizard.step_views.features.
    return import_module("ui.wizard.step_views.features")


class _TTSFeatureCard(QFrame):
    """用声音说话：开关 + 本地/API 选择 + 本地音色配置 + API provider。"""

    _API_PROVIDERS = [
        ("EdgeTTS（推荐 · 无需密钥）", "edge"),
        ("科大讯飞", "xfyun"),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        features = _features_module()
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(COPY["features.tts_title"])
        self._check.toggled.connect(self._toggle_body)
        head.addWidget(self._check)
        head.addStretch(1)
        features._add_guide_button(head, "tts_api", self)
        outer.addLayout(head)

        d = QLabel(COPY["features.tts_desc"])
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 0)
        outer.addWidget(d)

        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(24, Spacing.SM, 0, 0)
        body_layout.setSpacing(Spacing.SM)

        form = QFormLayout()
        self._form = form
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(Spacing.SM)

        self._type_combo = QComboBox()
        self._type_combo.addItem("云端 API（推荐 · EdgeTTS）", "api")
        self._type_combo.addItem("本地（VoxCPM2）", "local")
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("运行方式"), self._type_combo)

        self._device_combo = QComboBox()
        self._device_combo.addItems(["auto", "cuda", "cpu"])
        form.addRow(QLabel("设备"), self._device_combo)

        self._ref_audio_edit = QLineEdit()
        self._ref_audio_edit.setPlaceholderText("可选：data/models/VoxCPM2/ref.wav")
        self._ref_audio_row = features._path_picker_row(
            self._ref_audio_edit,
            parent=self,
            title="选择 TTS 参考音频",
            directory=False,
            file_filter="音频文件 (*.wav *.mp3 *.flac *.m4a *.ogg);;所有文件 (*)",
        )
        form.addRow(QLabel("参考音频（可选）"), self._ref_audio_row)

        self._prompt_edit = QLineEdit()
        self._prompt_edit.setPlaceholderText("可选，如「年轻女性，温柔语气」")
        form.addRow(QLabel("默认音色/语气"), self._prompt_edit)

        self._model_dir_edit = QLineEdit("data/models/VoxCPM2")
        self._model_dir_edit.textChanged.connect(lambda *_: self._check_model())
        self._model_dir_row = features._path_picker_row(
            self._model_dir_edit,
            parent=self,
            title="选择 TTS 模型目录",
            directory=True,
        )
        form.addRow(QLabel("模型目录"), self._model_dir_row)

        self._load_denoiser = QCheckBox("启用")
        self._load_denoiser.setToolTip("默认关闭；开启前请确认降噪模型已手动准备好。")
        form.addRow(QLabel("降噪器"), self._load_denoiser)

        self._cfg_value = QDoubleSpinBox()
        self._cfg_value.setRange(0.1, 20.0)
        self._cfg_value.setSingleStep(0.1)
        self._cfg_value.setValue(2.0)
        form.addRow(QLabel("CFG 强度"), self._cfg_value)

        self._timesteps = QSpinBox()
        self._timesteps.setRange(1, 100)
        self._timesteps.setValue(10)
        form.addRow(QLabel("推理步数"), self._timesteps)

        self._api_provider_combo = QComboBox()
        for label, value in self._API_PROVIDERS:
            self._api_provider_combo.addItem(label, value)
        self._api_provider_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("API Provider"), self._api_provider_combo)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("讯飞 API Key")
        form.addRow(QLabel("API 密钥"), self._api_key_edit)

        self._api_voice = QLineEdit()
        self._api_voice.setPlaceholderText("讯飞默认 x4_xiaoyan；Edge 默认 zh-CN-XiaoxiaoNeural")
        form.addRow(QLabel("说话人"), self._api_voice)

        self._xfyun_appid = QLineEdit()
        self._xfyun_appid.setPlaceholderText("讯飞控制台 AppID")
        form.addRow(QLabel("App ID"), self._xfyun_appid)
        self._xfyun_secret = QLineEdit()
        self._xfyun_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._xfyun_secret.setPlaceholderText("API Secret")
        form.addRow(QLabel("API Secret"), self._xfyun_secret)

        body_layout.addLayout(form)

        self._tts_warning = QLabel("")
        self._tts_warning.setProperty("role", "warning")
        self._tts_warning.setWordWrap(True)
        self._tts_warning.setVisible(False)
        body_layout.addWidget(self._tts_warning)

        local_actions = QHBoxLayout()
        self._download_btn = QPushButton("安装指引")
        self._download_btn.setProperty("role", "secondary")
        self._download_btn.clicked.connect(self._on_download)
        self._download_btn.setVisible(False)
        local_actions.addWidget(self._download_btn)
        self._open_dir_btn = QPushButton("打开目录")
        self._open_dir_btn.setProperty("role", "secondary")
        self._open_dir_btn.clicked.connect(lambda: features._open_directory(self._model_dir()))
        local_actions.addWidget(self._open_dir_btn)
        local_actions.addStretch(1)
        body_layout.addLayout(local_actions)

        hint = QLabel(
            "EdgeTTS 无需额外配置，但依赖微软在线服务，可能因网络或服务策略合成失败。"
            "讯飞需要先在控制台开通在线语音合成并填写密钥。"
        )
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)
        body_layout.addWidget(hint)

        outer.addWidget(self._body)
        self._body.setVisible(False)
        self._on_type_changed()

    def _toggle_body(self, on: bool) -> None:
        self._body.setVisible(on)

    def _on_type_changed(self) -> None:
        features = _features_module()
        is_local = self._type_combo.currentData() == "local"
        features._set_form_field_visible(self._form, self._device_combo, is_local)
        features._set_form_field_visible(self._form, self._ref_audio_row, is_local)
        features._set_form_field_visible(self._form, self._prompt_edit, is_local)
        features._set_form_field_visible(self._form, self._model_dir_row, is_local)
        features._set_form_field_visible(self._form, self._load_denoiser, is_local)
        features._set_form_field_visible(self._form, self._cfg_value, is_local)
        features._set_form_field_visible(self._form, self._timesteps, is_local)
        is_api = not is_local
        features._set_form_field_visible(self._form, self._api_provider_combo, is_api)
        prov = self._api_provider_combo.currentData() if is_api else ""
        features._set_form_field_visible(self._form, self._api_key_edit, is_api and prov == "xfyun")
        features._set_form_field_visible(self._form, self._api_voice, is_api)
        features._set_form_field_visible(self._form, self._xfyun_appid, is_api and prov == "xfyun")
        features._set_form_field_visible(self._form, self._xfyun_secret, is_api and prov == "xfyun")
        self._download_btn.setVisible(is_local)
        self._open_dir_btn.setVisible(is_local)
        self._check_model()

    def _model_dir(self) -> str:
        return self._model_dir_edit.text().strip() or "data/models/VoxCPM2"

    def _check_model(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        if not is_local:
            self._tts_warning.setVisible(False)
            return
        d = self._model_dir()
        if not _features_module()._directory_has_files(d):
            self._tts_warning.setText(f"⚠ 模型目录未就绪：{d}")
            self._tts_warning.setVisible(True)
        else:
            self._tts_warning.setVisible(False)

    def ensure_ready(self, parent: QWidget) -> bool:
        if not self._check.isChecked():
            return True
        st = self.state()
        features = _features_module()
        if st["type"] == "local":
            if features._directory_has_files(st["model_dir"]):
                return True
            features._prompt_download_model(
                parent,
                "TTS 模型未就绪",
                "你启用了本地语音合成，但模型目录还没有可用文件。\n\n"
                f"当前目录：{st['model_dir']}\n\n"
                "请按安装指引放置模型，或修复模型目录，然后再进入下一页。",
                self._on_download,
            )
            return False
        if st["provider"] == "edge":
            return True
        if not st["api_key"]:
            if hasattr(parent, "invalid_input"):
                parent.invalid_input.emit("开了「用声音说话」的 API 模式就要填 API 密钥")  # type: ignore[attr-defined]
            return False
        extra = st["extra_credentials"]
        provider = st["provider"]
        missing = []
        if provider == "xfyun":
            if not extra.get("app_id"):
                missing.append("App ID")
            if not extra.get("api_secret"):
                missing.append("API Secret")
        if missing:
            if hasattr(parent, "invalid_input"):
                parent.invalid_input.emit(
                    "开了「用声音说话」的 API 模式还需要填写：" + "、".join(missing)
                )  # type: ignore[attr-defined]
            return False
        return True

    def _on_download(self) -> None:
        """打开 VoxCPM2 模型安装指引。"""
        _features_module()._start_plugin_download(
            self,
            "voxcpm2",
            "voxcpm2",
            "VoxCPM2 语音合成模型",
            on_finished=self._check_model,
        )

    def is_enabled(self) -> bool:
        return self._check.isChecked()

    def state(self) -> dict:
        extra: dict[str, str] = {}
        if self._type_combo.currentData() == "api":
            prov = self._api_provider_combo.currentData()
            voice = self._api_voice.text().strip()
            if voice:
                extra["voice"] = voice
            if prov == "xfyun":
                extra["app_id"] = self._xfyun_appid.text().strip()
                extra["api_secret"] = self._xfyun_secret.text().strip()
        return {
            "enabled": self._check.isChecked(),
            "type": self._type_combo.currentData() or "api",
            "device": self._device_combo.currentText(),
            "reference_audio": self._ref_audio_edit.text().strip(),
            "default_prompt": self._prompt_edit.text().strip(),
            "model_dir": self._model_dir(),
            "load_denoiser": self._load_denoiser.isChecked(),
            "cfg_value": self._cfg_value.value(),
            "inference_timesteps": self._timesteps.value(),
            "provider": self._api_provider_combo.currentData() if self._type_combo.currentData() == "api" else "",
            "api_key": self._api_key_edit.text(),
            "extra_credentials": extra,
        }

    def set_state(self, choice) -> None:
        self._check.setChecked(choice.enabled)
        extra = choice.extra or {}
        tp = extra.get("type", "api")
        idx = self._type_combo.findData(tp)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._on_type_changed()
        if extra.get("device"):
            idx_d = self._device_combo.findText(extra["device"])
            if idx_d >= 0:
                self._device_combo.setCurrentIndex(idx_d)
        self._ref_audio_edit.setText(extra.get("reference_audio", ""))
        self._prompt_edit.setText(extra.get("default_prompt", ""))
        self._model_dir_edit.setText(extra.get("model_dir", "data/models/VoxCPM2"))
        self._load_denoiser.setChecked(bool(extra.get("load_denoiser", False)))
        self._cfg_value.setValue(float(extra.get("cfg_value", 2.0)))
        self._timesteps.setValue(int(extra.get("inference_timesteps", 10)))
        if extra.get("provider"):
            idx_p = self._api_provider_combo.findData(extra["provider"])
            if idx_p >= 0:
                self._api_provider_combo.setCurrentIndex(idx_p)
        if extra.get("api_key"):
            self._api_key_edit.setText(extra["api_key"])
        creds = extra.get("extra_credentials", {})
        self._api_voice.setText(creds.get("voice", ""))
        self._xfyun_appid.setText(creds.get("app_id", ""))
        self._xfyun_secret.setText(creds.get("api_secret", ""))
        self._on_type_changed()


__all__ = ["_TTSFeatureCard"]
