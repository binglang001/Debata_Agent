"""Settings page edit dialogs."""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from ...theme import Spacing
from ...widgets import FramelessDialog, show_message
from ...widgets.model_combo import ModelComboBox
from .helpers import _path_picker_row, _set_form_field_visible

logger = logging.getLogger(__name__)

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

        self._model_edit = ModelComboBox()
        self._model_edit.setPlaceholderText("模型 ID")
        model_row = QHBoxLayout()
        model_row.addWidget(self._model_edit, 1)
        self._fetch_models_btn = QPushButton("获取模型")
        self._fetch_models_btn.setProperty("role", "secondary")
        self._fetch_models_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self._fetch_models_btn)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        form.addRow(QLabel("默认模型 ID"), model_wrap)

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
        if not self._model_edit.current_model_id():
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
            "model": self._model_edit.current_model_id(),
            "api_key": self._key_edit.text(),
        }
        self.accept()

    def _on_fetch_models(self) -> None:
        preset = self._preset_combo.currentData()
        key = self._key_edit.text().strip()
        if not key:
            show_message(self, "缺少密钥", "请先填写 API 密钥。")
            return
        base_url = self._base_url_edit.text().strip()
        protocol = "anthropic" if preset == "anthropic" else "openai_compat"
        if preset != "custom":
            base_url = self._preset_base_url(str(preset))
            protocol = self._preset_protocol(str(preset))
        if not base_url:
            show_message(self, "缺少 Base URL", "请先填写 Base URL。")
            return
        self._fetch_models_btn.setEnabled(False)
        self._fetch_models_btn.setText("获取中")

        async def _do_fetch() -> None:
            try:
                from providers.model_fetcher import fetch_model_infos
                from providers.registry import normalize_base_url

                provider_id = "" if preset == "custom" else str(preset)
                models = await fetch_model_infos(
                    normalize_base_url(base_url, protocol),
                    key,
                    protocol,
                    provider_id=provider_id,
                    timeout=8.0,
                )
                self._model_edit.set_models(
                    [m.id for m in models],
                    provider_id=provider_id,
                    current=self._model_edit.current_model_id(),
                )
            except Exception as e:
                show_message(self, "获取模型失败", str(e))
            finally:
                self._fetch_models_btn.setEnabled(True)
                self._fetch_models_btn.setText("获取模型")

        try:
            asyncio.get_event_loop().create_task(_do_fetch())
        except RuntimeError:
            self._fetch_models_btn.setEnabled(True)
            self._fetch_models_btn.setText("获取模型")
            show_message(self, "获取模型失败", "事件循环未就绪")

    def _preset_base_url(self, preset: str) -> str:
        try:
            from providers.presets_loader import load_all_presets

            parent = self.parent()
            paths = getattr(getattr(parent, "_runtime", None), "paths", None)
            if paths is None:
                return ""
            info = load_all_presets(paths.PROVIDER_PRESETS_DIR).get(preset)
            return info.base_url if info else ""
        except Exception:
            return ""

    def _preset_protocol(self, preset: str) -> str:
        try:
            from providers.presets_loader import load_all_presets

            parent = self.parent()
            paths = getattr(getattr(parent, "_runtime", None), "paths", None)
            if paths is None:
                return "openai_compat"
            info = load_all_presets(paths.PROVIDER_PRESETS_DIR).get(preset)
            return info.protocol if info else "openai_compat"
        except Exception:
            return "openai_compat"


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
        if not hasattr(self, "_api_key"):
            return
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

    _API_PROVIDERS = [
        ("EdgeTTS（推荐 · 无需密钥）", "edge"),
        ("科大讯飞", "xfyun"),
    ]

    def __init__(self, feat, parent=None) -> None:
        super().__init__("配置语音合成（TTS）", parent)
        self.setMinimumWidth(520)
        self.result_data: dict | None = None

        body = self.body_layout()
        intro = QLabel(
            "本地模式用 VoxCPM2 合成语音；API 模式推荐 EdgeTTS（无需密钥，但可能因网络或微软服务策略失败），"
            "也可使用科大讯飞流式语音合成。"
        )
        intro.setProperty("role", "secondary")
        intro.setWordWrap(True)
        body.addWidget(intro)
        guide_row = QHBoxLayout()
        guide_row.addStretch(1)
        guide_btn = QPushButton("图文教程")
        guide_btn.setProperty("role", "secondary")
        guide_btn.clicked.connect(self._open_tts_guide)
        guide_row.addWidget(guide_btn)
        body.addLayout(guide_row)

        form = QFormLayout()
        self._form = form
        form.setSpacing(Spacing.SM)

        self._type_combo = QComboBox()
        self._type_combo.addItem("云端 API（推荐 · EdgeTTS）", "api")
        self._type_combo.addItem("本地（VoxCPM2）", "local")
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
        for label, value in self._API_PROVIDERS:
            self._prov.addItem(label, value)
        self._prov.currentIndexChanged.connect(self._on_type_changed)
        idx = self._prov.findData(feat.provider or "edge")
        if idx >= 0:
            self._prov.setCurrentIndex(idx)
        form.addRow(QLabel("API Provider"), self._prov)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("API Key（留空保留现有）")
        form.addRow(QLabel("API Key"), self._api_key)

        creds = feat.extra_credentials if feat.extra_credentials else {}
        self._api_voice = QLineEdit(creds.get("voice", ""))
        self._api_voice.setPlaceholderText("Edge 默认 zh-CN-XiaoxiaoNeural；讯飞默认 x4_xiaoyan")
        form.addRow(QLabel("说话人"), self._api_voice)

        self._xfyun_appid = QLineEdit()
        self._xfyun_appid.setText(creds.get("app_id", ""))
        self._xfyun_appid.setPlaceholderText("讯飞控制台 AppID")
        form.addRow(QLabel("App ID（讯飞）"), self._xfyun_appid)
        self._xfyun_secret = QLineEdit()
        self._xfyun_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._xfyun_secret.setText(creds.get("api_secret", ""))
        self._xfyun_secret.setPlaceholderText("API Secret（讯飞）")
        form.addRow(QLabel("API Secret（讯飞）"), self._xfyun_secret)

        body.addLayout(form)

        idx_t = self._type_combo.findData(feat.type or "api")
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
        prov = self._prov.currentData() if is_api else ""
        _set_form_field_visible(self._form, self._api_key, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._api_voice, is_api)
        _set_form_field_visible(self._form, self._xfyun_appid, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._xfyun_secret, is_api and prov == "xfyun")

    def _on_ok(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        extra: dict[str, str] = {}
        if not is_local:
            prov = self._prov.currentData()
            voice = self._api_voice.text().strip()
            if voice:
                extra["voice"] = voice
            if prov == "xfyun":
                extra["app_id"] = self._xfyun_appid.text().strip()
                extra["api_secret"] = self._xfyun_secret.text().strip()
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

    def _open_tts_guide(self) -> None:
        parent = self.parent()
        opener = getattr(parent, "_open_feature_guide", None)
        if callable(opener):
            opener("tts_api")


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

        self._model = ModelComboBox()
        if emb.api_model:
            self._model.setText(emb.api_model)
        self._model.setPlaceholderText("如 text-embedding-v4 / embedding-3 / doubao-embedding-text-240515")
        model_row = QHBoxLayout()
        model_row.addWidget(self._model, 1)
        self._fetch_btn = QPushButton("获取模型")
        self._fetch_btn.setProperty("role", "secondary")
        self._fetch_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self._fetch_btn)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        self._model_row = model_wrap
        form.addRow(QLabel("模型 ID"), model_wrap)

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
        _set_form_field_visible(self._form, self._model_row, not is_local)
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
            if not self._model.current_model_id():
                show_message(self, "缺模型 ID", "请填 embedding 模型 ID")
                return
            self.result_data = {
                "type": "api",
                "provider": pid,
                "model": self._model.current_model_id(),
                "api_key": self._key.text(),
            }
        else:
            self.result_data = {
                "type": "local",
                "local_quality": self._local_quality.currentData() or "performance",
                "local_model_dir": self._local_dir.text().strip() or self._default_local_dir(),
            }
        self.accept()

    def _on_fetch_models(self) -> None:
        pid = self._prov.currentData()
        if not pid:
            show_message(self, "缺 provider", "请先选择 provider")
            return
        parent = self.parent()
        if not hasattr(parent, "_fetch_models_for_provider"):
            show_message(self, "无法获取", "当前窗口不支持获取模型")
            return
        self._fetch_btn.setEnabled(False)
        self._fetch_btn.setText("获取中")

        async def _do_fetch() -> None:
            try:
                models = await parent._fetch_models_for_provider(str(pid))  # type: ignore[attr-defined]
                preset = parent._provider_preset_id(str(pid))  # type: ignore[attr-defined]
                ids = [getattr(m, "id", str(m)) for m in models]
                embedding_ids = self._filter_embedding_ids(preset, ids)
                self._model.set_models(
                    embedding_ids or ids,
                    provider_id=preset,
                    current=self._model.current_model_id(),
                )
                if not self._model.current_model_id():
                    recommended = self._recommended_embedding_model(preset)
                    if recommended:
                        self._model.setText(recommended)
            except Exception as e:
                show_message(self, "获取模型失败", str(e))
            finally:
                self._fetch_btn.setEnabled(True)
                self._fetch_btn.setText("获取模型")

        try:
            asyncio.get_event_loop().create_task(_do_fetch())
        except RuntimeError:
            self._fetch_btn.setEnabled(True)
            self._fetch_btn.setText("获取模型")
            show_message(self, "获取模型失败", "事件循环未就绪")

    def _filter_embedding_ids(self, preset: str, model_ids: list[str]) -> list[str]:
        try:
            from providers.model_capabilities import model_supports

            return [mid for mid in model_ids if model_supports(preset, mid, "embedding")]
        except Exception:
            return []

    def _recommended_embedding_model(self, preset: str) -> str:
        try:
            from providers.model_capabilities import recommended_model

            model = recommended_model(preset, "embedding")
            return model.id if model else ""
        except Exception:
            return ""


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

        self._model = ModelComboBox()
        if current_model:
            self._model.setText(current_model)
        self._model.setPlaceholderText("如 doubao-seed-2-0-lite-260428 / glm-5v-turbo / gpt-5.5")
        model_row = QHBoxLayout()
        model_row.addWidget(self._model, 1)
        self._fetch_btn = QPushButton("获取模型")
        self._fetch_btn.setProperty("role", "secondary")
        self._fetch_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self._fetch_btn)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        form.addRow(QLabel("视觉模型 ID"), model_wrap)

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
        default = self._recommended_vision_model(preset) or self.DEFAULT_MODELS.get(preset, "")
        if default and not self._model.current_model_id():
            self._model.setText(default)
        known = self._known_vision_models(preset)
        if known:
            self._model.set_models(known, provider_id=preset, current=self._model.current_model_id())

    def _on_ok(self) -> None:
        pid = self._prov.currentData()
        if not pid:
            show_message(self, "缺 provider", "请选一个 provider")
            return
        if not self._model.current_model_id():
            show_message(self, "缺模型 ID", "请填视觉模型 ID")
            return
        self.result_data = {
            "provider": pid,
            "model": self._model.current_model_id(),
            "api_key": self._key.text(),
        }
        self.accept()

    def _on_fetch_models(self) -> None:
        pid = self._prov.currentData()
        if not pid:
            show_message(self, "缺 provider", "请先选择 provider")
            return
        parent = self.parent()
        if not hasattr(parent, "_fetch_models_for_provider"):
            show_message(self, "无法获取", "当前窗口不支持获取模型")
            return
        self._fetch_btn.setEnabled(False)
        self._fetch_btn.setText("获取中")

        async def _do_fetch() -> None:
            try:
                models = await parent._fetch_models_for_provider(str(pid))  # type: ignore[attr-defined]
                preset = self._provider_presets.get(str(pid), "")
                ids = [getattr(m, "id", str(m)) for m in models]
                vision_ids = self._filter_vision_ids(preset, ids)
                self._model.set_models(
                    vision_ids or ids,
                    provider_id=preset,
                    current=self._model.current_model_id(),
                )
            except Exception as e:
                show_message(self, "获取模型失败", str(e))
            finally:
                self._fetch_btn.setEnabled(True)
                self._fetch_btn.setText("获取模型")

        try:
            asyncio.get_event_loop().create_task(_do_fetch())
        except RuntimeError:
            self._fetch_btn.setEnabled(True)
            self._fetch_btn.setText("获取模型")
            show_message(self, "获取模型失败", "事件循环未就绪")

    def _filter_vision_ids(self, preset: str, model_ids: list[str]) -> list[str]:
        try:
            from providers.model_capabilities import model_supports

            return [mid for mid in model_ids if model_supports(preset, mid, "vision")]
        except Exception:
            return []

    def _known_vision_models(self, preset: str) -> list[str]:
        try:
            from providers.model_capabilities import known_model_ids

            return known_model_ids(preset, capability="vision")
        except Exception:
            return []

    def _recommended_vision_model(self, preset: str) -> str:
        try:
            from providers.model_capabilities import recommended_model

            model = recommended_model(preset, "vision")
            return model.id if model else ""
        except Exception:
            return ""


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

