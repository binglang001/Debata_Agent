"""可选功能开关 + 详细配置。

5 个 feature 卡片：
    - vision：provider + model + 密钥
    - weather：和风主机 + 密钥
    - asr：QQ 语音识别使用 NapCat 内置转写，不再配置本地模型
    - tts：本地 VoxCPM2（可选参考音频/语气/目录）或 API
    - web_search：开关，无需密钥
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..components import ApiKeyInput, SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ...theme import Spacing


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


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


def _open_directory(path: str) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))


def _resolve_project_path(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (_PROJECT_ROOT / p).resolve()


def _directory_has_files(path: str) -> bool:
    p = _resolve_project_path(path)
    if not p.exists() or not p.is_dir():
        return False
    try:
        return any(child.is_file() for child in p.rglob("*"))
    except OSError:
        return False


def _prompt_download_model(
    parent: QWidget,
    title: str,
    message: str,
    on_download: Callable[[], None],
) -> None:
    from ...widgets.window_chrome import show_message

    if show_message(
        parent,
        title,
        message,
        confirm_text="查看安装指引",
        cancel_text="先不处理",
    ):
        on_download()


def _start_plugin_download(
    parent,
    plugin_name: str,
    plugin_dir: str,
    display_name: str,
    on_finished: Callable[[], None] | None = None,
) -> None:
    """在向导中打开模型安装指引，不再执行自动下载。"""
    from pathlib import Path
    from plugins import PluginManager
    from ...widgets import show_model_install_guide
    from ...widgets.window_chrome import show_message

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    plugins_root = project_root / "plugins"

    if not plugins_root.exists():
        show_message(parent, "目录不存在", f"未找到 plugins 目录：{plugins_root}")
        return

    pm = PluginManager(plugins_root)
    try:
        pm.scan()
    except Exception as e:
        show_message(parent, "扫描失败", str(e))
        return

    record = pm.get(plugin_name)
    if record is None:
        # 按目录名查找
        for r in pm.list_all():
            if r.module_path and r.module_path.parent.name == plugin_dir:
                record = r
                break
    if record is None:
        show_message(parent, "未找到插件", f"未找到名为 {plugin_name} 的模型插件。")
        return

    show_model_install_guide(parent, record)
    if on_finished is not None:
        on_finished()


# 各 provider preset 默认的视觉模型 ID
_VISION_PRESETS: dict[str, dict] = {
    "main": {
        "display": "和主模型同 provider（同一个密钥）",
        "model": "",
        "url": "",
        "hint": "适合主模型选了 GLM-4V / Qwen-VL / Gemini / Claude 等本身支持图像的；模型 ID 填可视觉的那一款。",
    },
    "volcengine": {
        "display": "火山方舟 · 豆包 Vision",
        "model": "doubao-seed-2-0-lite-260428",
        "url": "https://ark.cn-beijing.volces.com/api/v3",
        "hint": "国内可直连。控制台 https://www.volcengine.com/product/ark 申请 API Key。",
    },
    "glm": {
        "display": "智谱 GLM-5V",
        "model": "glm-5v-turbo",
        "url": "https://open.bigmodel.cn/api/paas/v4",
        "hint": "智谱开放平台，控制台 https://open.bigmodel.cn 获取 API Key。",
    },
    "qwen": {
        "display": "通义千问 · Qwen3-VL",
        "model": "qwen3.6-plus",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint": "阿里百炼控制台 https://bailian.console.aliyun.com 获取 API Key。",
    },
    "gemini": {
        "display": "Google Gemini",
        "model": "gemini-3-pro",
        "url": "https://generativelanguage.googleapis.com/v1beta",
        "hint": "国内访问需要代理；Gemini 3 Pro 已自带视觉能力。",
    },
    "openai": {
        "display": "OpenAI GPT-5.5",
        "model": "gpt-5.5",
        "url": "https://api.openai.com/v1",
        "hint": "需国际信用卡 + 代理。",
    },
    "anthropic": {
        "display": "Anthropic Claude",
        "model": "claude-sonnet-4-6",
        "url": "https://api.anthropic.com/v1",
        "hint": "走 Anthropic 协议；密钥从 console.anthropic.com 获取。",
    },
    "openrouter": {
        "display": "OpenRouter",
        "model": "anthropic/claude-sonnet-4-6",
        "url": "https://openrouter.ai/api/v1",
        "hint": "一份 OpenRouter 密钥可走多家上游模型。",
    },
    "custom": {
        "display": "自行填一个（自定义 base_url）",
        "model": "",
        "url": "",
        "hint": "完全自定义：要填 Base URL + 模型 ID + 密钥。",
    },
}


def _add_guide_button(layout, guide_name: str, parent_widget) -> None:
    """在某个 head layout 末尾加「📖 教程」按钮，点击打开 feature_guide。"""
    from ..components import open_feature_guide

    btn = QPushButton("教程")
    btn.setFlat(True)
    btn.setProperty("role", "ghost")
    btn.clicked.connect(lambda: open_feature_guide(guide_name, parent_widget))
    layout.addWidget(btn)


class _SimpleFeatureToggle(QFrame):
    """简易开关：复选框 + 描述。无密钥输入（用于 asr/tts/web_search）。"""

    def __init__(
        self,
        title: str,
        desc: str,
        guide_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(title)
        head.addWidget(self._check)
        head.addStretch(1)
        if guide_name:
            _add_guide_button(head, guide_name, self)
        outer.addLayout(head)

        d = QLabel(desc)
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 0)
        outer.addWidget(d)

    def is_enabled(self) -> bool:
        return self._check.isChecked()

    def set_enabled(self, on: bool) -> None:
        self._check.setChecked(on)


class _VisionFeatureCard(QFrame):
    """看懂图片：开关 + provider 选择 + model + API 密钥（custom 时含 base_url）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        # 头：复选框 + 描述
        head = QHBoxLayout()
        self._check = QCheckBox(COPY["features.vision_title"])
        self._check.toggled.connect(self._toggle_body)
        head.addWidget(self._check)
        head.addStretch(1)
        _add_guide_button(head, "vision", self)
        outer.addLayout(head)

        d = QLabel(COPY["features.vision_desc"])
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 0)
        outer.addWidget(d)

        # 详情体（开关 on 时展开）
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(24, Spacing.SM, 0, 0)
        body_layout.setSpacing(Spacing.SM)

        form = QFormLayout()
        self._form = form
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(Spacing.SM)

        # provider 选择
        self._preset_combo = QComboBox()
        for key, info in _VISION_PRESETS.items():
            self._preset_combo.addItem(info["display"], key)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow(QLabel("视觉提供商"), self._preset_combo)

        # base_url（仅 custom）
        self._base_url_label = QLabel("Base URL")
        self._base_url_edit = QLineEdit()
        self._base_url_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow(self._base_url_label, self._base_url_edit)

        # model
        self._model_edit = QLineEdit()
        self._model_edit.setPlaceholderText("如 doubao-seed-2-0-lite-260428 / glm-5v-turbo / gpt-5.5")
        form.addRow(QLabel("视觉模型 ID"), self._model_edit)

        # 密钥
        self._key_input = ApiKeyInput(placeholder="该 provider 的 API 密钥")
        self._key_input.test_requested.connect(self._on_test)
        form.addRow(QLabel("API 密钥"), self._key_input)

        body_layout.addLayout(form)

        # 提示
        self._hint_lbl = QLabel("")
        self._hint_lbl.setProperty("role", "secondary")
        self._hint_lbl.setWordWrap(True)
        body_layout.addWidget(self._hint_lbl)

        outer.addWidget(self._body)
        self._body.setVisible(False)

        # 初始化默认到 volcengine（默认条目；refresh 时再覆盖）
        idx = self._preset_combo.findData("volcengine")
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)
        self._on_preset_changed(idx if idx >= 0 else 0)

    def _toggle_body(self, on: bool) -> None:
        self._body.setVisible(on)

    def _on_preset_changed(self, idx: int) -> None:
        preset = self._preset_combo.itemData(idx) or "volcengine"
        info = _VISION_PRESETS.get(preset, {})

        # base_url 仅 custom 时可填
        is_custom = preset == "custom"
        self._base_url_label.setVisible(is_custom)
        self._base_url_edit.setVisible(is_custom)

        # main 模式：model + key 都可继承主模型 —— 仍允许编辑
        is_main = preset == "main"
        self._key_input.setEnabled(not is_main)

        # 自动填默认 model（仅当用户没改过）
        cur = self._model_edit.text().strip()
        known_defaults = {p["model"] for p in _VISION_PRESETS.values() if p["model"]}
        if not cur or cur in known_defaults:
            self._model_edit.setText(info.get("model", ""))

        self._hint_lbl.setText(info.get("hint", ""))

    async def _test_current(self) -> tuple[bool, str]:
        preset = self._preset_combo.currentData() or "volcengine"
        if preset == "main":
            return False, "复用主模型时请在主模型页面测试连接"
        model = self._model_edit.text().strip()
        key = self._key_input.text().strip()
        base_url = (
            self._base_url_edit.text().strip()
            if preset == "custom"
            else _VISION_PRESETS.get(preset, {}).get("url", "")
        )
        if not model:
            return False, "请先填写视觉模型 ID"
        if not key:
            return False, "请先填写 API 密钥"
        if not base_url:
            return False, "缺少 Base URL"
        try:
            from providers import probe_provider_endpoint
            from providers.registry import normalize_base_url

            protocol = "anthropic" if preset == "anthropic" else "openai_compat"
            result = await probe_provider_endpoint(
                protocol=protocol,
                base_url=normalize_base_url(base_url, protocol),
                api_key=key,
                model=model,
                timeout_seconds=8.0,
            )
            if result.status == "ok":
                return True, f"已就位（{result.latency_ms}ms）"
            return False, result.message
        except Exception as e:  # noqa: BLE001
            return False, f"未能完成：{e}"

    def _on_test(self, _key: str) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._key_input.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            ok, message = await self._test_current()
            self._key_input.set_test_state("success" if ok else "error", message)

        loop.create_task(_do_test())

    def is_enabled(self) -> bool:
        return self._check.isChecked()

    def state(self) -> dict:
        return {
            "enabled": self._check.isChecked(),
            "preset": self._preset_combo.currentData() or "volcengine",
            "model": self._model_edit.text().strip(),
            "base_url": self._base_url_edit.text().strip(),
            "api_key": self._key_input.text(),
        }

    def set_state(self, choice) -> None:
        self._check.setChecked(choice.enabled)
        extra = choice.extra or {}
        preset = extra.get("preset", "volcengine")
        idx = self._preset_combo.findData(preset)
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)
        self._on_preset_changed(self._preset_combo.currentIndex())
        if extra.get("model"):
            self._model_edit.setText(extra["model"])
        if extra.get("base_url"):
            self._base_url_edit.setText(extra["base_url"])
        if choice.api_key:
            self._key_input.set_text(choice.api_key)


class _WeatherFeatureCard(QFrame):
    """查天气：开关 + 和风 API 主机 + 密钥。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(COPY["features.weather_title"])
        self._check.toggled.connect(self._toggle_body)
        head.addWidget(self._check)
        head.addStretch(1)
        _add_guide_button(head, "weather", self)
        outer.addLayout(head)

        d = QLabel(COPY["features.weather_desc"])
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

        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("yourdomain.qweatherapi.com")
        form.addRow(QLabel("API 主机"), self._host_edit)

        self._key_input = ApiKeyInput(placeholder="和风天气 API 密钥")
        self._key_input.test_requested.connect(self._on_test)
        form.addRow(QLabel("API 密钥"), self._key_input)

        body_layout.addLayout(form)

        hint = QLabel(
            "和风天气从 2024 起给每个账号分配独立 API Host，"
            "登录 https://console.qweather.com → 项目管理 → 复制「API Host」。"
            "免费开发版老主机 devapi.qweather.com 仅供历史项目使用，新账号已不再分配。"
        )
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)
        body_layout.addWidget(hint)

        outer.addWidget(self._body)
        self._body.setVisible(False)

    def _toggle_body(self, on: bool) -> None:
        self._body.setVisible(on)

    def state(self) -> dict:
        return {
            "enabled": self._check.isChecked(),
            "host": self._host_edit.text().strip(),
            "api_key": self._key_input.text(),
        }

    async def _test_current(self) -> tuple[bool, str]:
        host = self._host_edit.text().strip()
        key = self._key_input.text().strip()
        if not host:
            return False, "请先填写 API 主机"
        if not key:
            return False, "请先填写 API 密钥"
        try:
            from features.weather import WeatherService

            service = WeatherService(
                api_key=key,
                host=host,
                timeout_seconds=8.0,
            )
            result = await service.query("北京", days=1)
            if "失败" in result or "错误" in result or "未找到城市" in result:
                return False, result
            return True, "已就位"
        except Exception as e:  # noqa: BLE001
            return False, f"未能完成：{e}"

    def _on_test(self, _key: str) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._key_input.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            ok, message = await self._test_current()
            self._key_input.set_test_state("success" if ok else "error", message)

        loop.create_task(_do_test())

    def set_state(self, choice) -> None:
        self._check.setChecked(choice.enabled)
        extra = choice.extra or {}
        host = extra.get("host", "")
        if host:
            self._host_edit.setText(host)
        if choice.api_key:
            self._key_input.set_text(choice.api_key)


class _ASRFeatureCard(QFrame):
    """听懂语音：开关 + 本地/API 选择 + 本地模型配置 + API provider。"""

    _API_PROVIDERS = ["baidu", "xfyun", "volcengine"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(COPY["features.asr_title"])
        self._check.toggled.connect(self._toggle_body)
        head.addWidget(self._check)
        head.addStretch(1)
        _add_guide_button(head, "adapter", self)
        outer.addLayout(head)

        d = QLabel(COPY["features.asr_desc"])
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

        # type：本地 / API
        self._type_combo = QComboBox()
        self._type_combo.addItem("本地（推荐 · 完全离线）", "local")
        self._type_combo.addItem("云端 API", "api")
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("运行方式"), self._type_combo)

        # 本地模式字段
        self._device_combo = QComboBox()
        self._device_combo.addItems(["auto", "cuda", "cpu"])
        form.addRow(QLabel("设备"), self._device_combo)

        self._language_edit = QLineEdit()
        self._language_edit.setPlaceholderText("zh（中文）；空=自动检测")
        form.addRow(QLabel("默认语言"), self._language_edit)

        self._model_dir_edit = QLineEdit()
        self._model_dir_edit.setPlaceholderText("NapCat 内置转写无需模型目录")
        self._model_dir_edit.textChanged.connect(lambda *_: self._check_model())
        self._model_dir_row = _path_picker_row(
            self._model_dir_edit,
            parent=self,
            title="选择 ASR 模型目录",
            directory=True,
        )
        form.addRow(QLabel("模型目录"), self._model_dir_row)

        # API 模式字段
        self._api_provider_combo = QComboBox()
        for p in self._API_PROVIDERS:
            self._api_provider_combo.addItem(p, p)
        self._api_provider_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("API Provider"), self._api_provider_combo)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("API Key")
        form.addRow(QLabel("API 密钥"), self._api_key_edit)

        # 专有字段
        self._baidu_secret = QLineEdit()
        self._baidu_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._baidu_secret.setPlaceholderText("百度语音 Secret Key")
        form.addRow(QLabel("Secret Key"), self._baidu_secret)

        self._xfyun_appid = QLineEdit()
        self._xfyun_appid.setPlaceholderText("控制台获取")
        form.addRow(QLabel("App ID"), self._xfyun_appid)
        self._xfyun_secret = QLineEdit()
        self._xfyun_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._xfyun_secret.setPlaceholderText("API Secret")
        form.addRow(QLabel("API Secret"), self._xfyun_secret)

        self._volc_appid = QLineEdit()
        self._volc_appid.setPlaceholderText("火山引擎 App ID")
        form.addRow(QLabel("App ID"), self._volc_appid)

        body_layout.addLayout(form)

        # 模型存在性警告
        self._model_warning = QLabel("")
        self._model_warning.setProperty("role", "warning")
        self._model_warning.setWordWrap(True)
        self._model_warning.setVisible(False)
        body_layout.addWidget(self._model_warning)

        # 手动安装辅助按钮（本地模式）
        local_actions = QHBoxLayout()
        self._download_btn = QPushButton("安装指引")
        self._download_btn.setProperty("role", "secondary")
        self._download_btn.clicked.connect(self._on_download)
        self._download_btn.setVisible(False)
        local_actions.addWidget(self._download_btn)
        self._open_dir_btn = QPushButton("打开目录")
        self._open_dir_btn.setProperty("role", "secondary")
        self._open_dir_btn.clicked.connect(lambda: _open_directory(self._model_dir()))
        local_actions.addWidget(self._open_dir_btn)
        local_actions.addStretch(1)
        body_layout.addLayout(local_actions)

        hint = QLabel("QQ 语音识别使用 NapCat 内置 fetch_ptt_text，无需下载本地 ASR 模型。")
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)
        body_layout.addWidget(hint)

        outer.addWidget(self._body)
        self._body.setVisible(False)
        self._on_type_changed()

    def _toggle_body(self, on: bool) -> None:
        self._body.setVisible(on)

    def _on_type_changed(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        _set_form_field_visible(self._form, self._device_combo, is_local)
        _set_form_field_visible(self._form, self._language_edit, is_local)
        _set_form_field_visible(self._form, self._model_dir_row, is_local)
        is_api = not is_local
        _set_form_field_visible(self._form, self._api_provider_combo, is_api)
        _set_form_field_visible(self._form, self._api_key_edit, is_api)
        prov = self._api_provider_combo.currentData() if is_api else ""
        _set_form_field_visible(self._form, self._baidu_secret, is_api and prov == "baidu")
        _set_form_field_visible(self._form, self._xfyun_appid, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._xfyun_secret, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._volc_appid, is_api and prov == "volcengine")
        self._download_btn.setVisible(is_local)
        self._open_dir_btn.setVisible(is_local)
        self._check_model()

    def _model_dir(self) -> str:
        return self._model_dir_edit.text().strip()

    def _check_model(self) -> None:
        is_local = self._type_combo.currentData() == "local"
        if not is_local:
            self._model_warning.setVisible(False)
            return
        d = self._model_dir()
        if not _directory_has_files(d):
            self._model_warning.setText(f"⚠ 模型目录未就绪：{d}")
            self._model_warning.setVisible(True)
        else:
            self._model_warning.setVisible(False)

    def ensure_ready(self, parent: QWidget) -> bool:
        if not self._check.isChecked():
            return True
        st = self.state()
        if st["type"] == "local":
            if _directory_has_files(st["model_dir"]):
                return True
            _prompt_download_model(
                parent,
                "ASR 模型未就绪",
                "你启用了本地语音识别，但模型目录还没有可用文件。\n\n"
                f"当前目录：{st['model_dir']}\n\n"
                "请按安装指引放置模型，或修复模型目录，然后再进入下一页。",
                self._on_download,
            )
            return False
        if not st["api_key"]:
            parent_msg = "开了「听懂语音」的 API 模式就要填 API 密钥"
            if hasattr(parent, "invalid_input"):
                parent.invalid_input.emit(parent_msg)  # type: ignore[attr-defined]
            return False
        extra = st["extra_credentials"]
        provider = st["provider"]
        missing = []
        if provider == "baidu" and not extra.get("secret_key"):
            missing.append("Secret Key")
        elif provider == "xfyun":
            if not extra.get("app_id"):
                missing.append("App ID")
            if not extra.get("api_secret"):
                missing.append("API Secret")
        elif provider == "volcengine" and not extra.get("app_id"):
            missing.append("App ID")
        if missing:
            if hasattr(parent, "invalid_input"):
                parent.invalid_input.emit(
                    "开了「听懂语音」的 API 模式还需要填写：" + "、".join(missing)
                )  # type: ignore[attr-defined]
            return False
        return True

    def _on_download(self) -> None:
        """打开 Whisper 模型安装指引。"""
        _start_plugin_download(
            self,
            "napcat",
            "napcat-asr",
            "NapCat 内置语音识别",
            on_finished=self._check_model,
        )

    def is_enabled(self) -> bool:
        return self._check.isChecked()

    def state(self) -> dict:
        model_dir = self._model_dir()
        extra: dict[str, str] = {}
        if self._type_combo.currentData() == "api":
            prov = self._api_provider_combo.currentData()
            if prov == "baidu":
                extra["secret_key"] = self._baidu_secret.text().strip()
            elif prov == "xfyun":
                extra["app_id"] = self._xfyun_appid.text().strip()
                extra["api_secret"] = self._xfyun_secret.text().strip()
            elif prov == "volcengine":
                extra["app_id"] = self._volc_appid.text().strip()
        return {
            "enabled": self._check.isChecked(),
            "type": self._type_combo.currentData() or "local",
            "device": self._device_combo.currentText(),
            "language": self._language_edit.text().strip() or "zh",
            "model_dir": model_dir,
            "provider": self._api_provider_combo.currentData() if self._type_combo.currentData() == "api" else "",
            "api_key": self._api_key_edit.text(),
            "extra_credentials": extra,
        }

    def set_state(self, choice) -> None:
        self._check.setChecked(choice.enabled)
        extra = choice.extra or {}
        tp = extra.get("type", "local")
        idx = self._type_combo.findData(tp)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._on_type_changed()
        if extra.get("device"):
            idx_d = self._device_combo.findText(extra["device"])
            if idx_d >= 0:
                self._device_combo.setCurrentIndex(idx_d)
        self._language_edit.setText(extra.get("language", "zh"))
        if extra.get("model_dir"):
            self._model_dir_edit.setText(extra["model_dir"])
        if extra.get("provider"):
            idx_p = self._api_provider_combo.findData(extra["provider"])
            if idx_p >= 0:
                self._api_provider_combo.setCurrentIndex(idx_p)
        if extra.get("api_key"):
            self._api_key_edit.setText(extra["api_key"])
        creds = extra.get("extra_credentials", {})
        self._baidu_secret.setText(creds.get("secret_key", ""))
        self._xfyun_appid.setText(creds.get("app_id", ""))
        self._xfyun_secret.setText(creds.get("api_secret", ""))
        self._volc_appid.setText(creds.get("app_id", ""))
        self._on_type_changed()


class _TTSFeatureCard(QFrame):
    """用声音说话：开关 + 本地/API 选择 + 本地音色配置 + API provider。"""

    _API_PROVIDERS = ["baidu", "xfyun", "volcengine"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(COPY["features.tts_title"])
        self._check.toggled.connect(self._toggle_body)
        head.addWidget(self._check)
        head.addStretch(1)
        _add_guide_button(head, "tts_voxcpm", self)
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
        self._type_combo.addItem("本地（推荐 · VoxCPM2）", "local")
        self._type_combo.addItem("云端 API", "api")
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("运行方式"), self._type_combo)

        # 本地模式
        self._device_combo = QComboBox()
        self._device_combo.addItems(["auto", "cuda", "cpu"])
        form.addRow(QLabel("设备"), self._device_combo)

        self._ref_audio_edit = QLineEdit()
        self._ref_audio_edit.setPlaceholderText("可选：data/models/VoxCPM2/ref.wav")
        self._ref_audio_row = _path_picker_row(
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
        self._model_dir_row = _path_picker_row(
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

        # API 模式字段
        self._api_provider_combo = QComboBox()
        for p in self._API_PROVIDERS:
            self._api_provider_combo.addItem(p, p)
        self._api_provider_combo.currentIndexChanged.connect(self._on_type_changed)
        form.addRow(QLabel("API Provider"), self._api_provider_combo)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("API Key")
        form.addRow(QLabel("API 密钥"), self._api_key_edit)

        # 专有字段
        self._baidu_secret = QLineEdit()
        self._baidu_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._baidu_secret.setPlaceholderText("百度语音 Secret Key")
        form.addRow(QLabel("Secret Key"), self._baidu_secret)

        self._xfyun_appid = QLineEdit()
        self._xfyun_appid.setPlaceholderText("控制台获取")
        form.addRow(QLabel("App ID"), self._xfyun_appid)
        self._xfyun_secret = QLineEdit()
        self._xfyun_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._xfyun_secret.setPlaceholderText("API Secret")
        form.addRow(QLabel("API Secret"), self._xfyun_secret)

        self._volc_appid = QLineEdit()
        self._volc_appid.setPlaceholderText("火山引擎 App ID")
        form.addRow(QLabel("App ID"), self._volc_appid)

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
        self._open_dir_btn.clicked.connect(lambda: _open_directory(self._model_dir()))
        local_actions.addWidget(self._open_dir_btn)
        local_actions.addStretch(1)
        body_layout.addLayout(local_actions)

        hint = QLabel(
            "VoxCPM2 可以只靠音色/语气描述生成语音；填 3-10 秒参考音频时会按该音色克隆。"
            "模型约 3GB，若未就绪请点「安装指引」。"
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
        is_local = self._type_combo.currentData() == "local"
        _set_form_field_visible(self._form, self._device_combo, is_local)
        _set_form_field_visible(self._form, self._ref_audio_row, is_local)
        _set_form_field_visible(self._form, self._prompt_edit, is_local)
        _set_form_field_visible(self._form, self._model_dir_row, is_local)
        _set_form_field_visible(self._form, self._load_denoiser, is_local)
        _set_form_field_visible(self._form, self._cfg_value, is_local)
        _set_form_field_visible(self._form, self._timesteps, is_local)
        is_api = not is_local
        _set_form_field_visible(self._form, self._api_provider_combo, is_api)
        _set_form_field_visible(self._form, self._api_key_edit, is_api)
        prov = self._api_provider_combo.currentData() if is_api else ""
        _set_form_field_visible(self._form, self._baidu_secret, is_api and prov == "baidu")
        _set_form_field_visible(self._form, self._xfyun_appid, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._xfyun_secret, is_api and prov == "xfyun")
        _set_form_field_visible(self._form, self._volc_appid, is_api and prov == "volcengine")
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
        if not _directory_has_files(d):
            self._tts_warning.setText(f"⚠ 模型目录未就绪：{d}")
            self._tts_warning.setVisible(True)
        else:
            self._tts_warning.setVisible(False)

    def ensure_ready(self, parent: QWidget) -> bool:
        if not self._check.isChecked():
            return True
        st = self.state()
        if st["type"] == "local":
            if _directory_has_files(st["model_dir"]):
                return True
            _prompt_download_model(
                parent,
                "TTS 模型未就绪",
                "你启用了本地语音合成，但模型目录还没有可用文件。\n\n"
                f"当前目录：{st['model_dir']}\n\n"
                "请按安装指引放置模型，或修复模型目录，然后再进入下一页。",
                self._on_download,
            )
            return False
        if not st["api_key"]:
            if hasattr(parent, "invalid_input"):
                parent.invalid_input.emit("开了「用声音说话」的 API 模式就要填 API 密钥")  # type: ignore[attr-defined]
            return False
        extra = st["extra_credentials"]
        provider = st["provider"]
        missing = []
        if provider == "baidu" and not extra.get("secret_key"):
            missing.append("Secret Key")
        elif provider == "xfyun":
            if not extra.get("app_id"):
                missing.append("App ID")
            if not extra.get("api_secret"):
                missing.append("API Secret")
        elif provider == "volcengine" and not extra.get("app_id"):
            missing.append("App ID")
        if missing:
            if hasattr(parent, "invalid_input"):
                parent.invalid_input.emit(
                    "开了「用声音说话」的 API 模式还需要填写：" + "、".join(missing)
                )  # type: ignore[attr-defined]
            return False
        return True

    def _on_download(self) -> None:
        """打开 VoxCPM2 模型安装指引。"""
        _start_plugin_download(
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
            if prov == "baidu":
                extra["secret_key"] = self._baidu_secret.text().strip()
            elif prov == "xfyun":
                extra["app_id"] = self._xfyun_appid.text().strip()
                extra["api_secret"] = self._xfyun_secret.text().strip()
            elif prov == "volcengine":
                extra["app_id"] = self._volc_appid.text().strip()
        return {
            "enabled": self._check.isChecked(),
            "type": self._type_combo.currentData() or "local",
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
        tp = extra.get("type", "local")
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
        self._baidu_secret.setText(creds.get("secret_key", ""))
        self._xfyun_appid.setText(creds.get("app_id", ""))
        self._xfyun_secret.setText(creds.get("api_secret", ""))
        self._volc_appid.setText(creds.get("app_id", ""))
        self._on_type_changed()


class FeaturesStepView(BaseStepView):
    """5 个 feature toggle。长期记忆/RAG 在独立「记忆方式」页配置。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="选些可选的本领",
            subtitle=(
                "这些功能默认关闭。打开哪个，Debata 就拥有哪个能力。\n"
                "现在不开也没关系，之后随时能在设置里打开。"
            ),
            compact=True,
        )
        outer.addWidget(card)

        # vision
        self._vision = _VisionFeatureCard()
        card.add_content(self._vision)

        # weather
        self._weather = _WeatherFeatureCard()
        card.add_content(self._weather)

        # ASR 已改用 NapCat 内置语音转文字，不再暴露 Whisper 配置。
        self._asr = _ASRFeatureCard()
        self._tts = _TTSFeatureCard()
        card.add_content(self._tts)

        # web_search（简易开关）
        self._web_search = _SimpleFeatureToggle(
            COPY["features.web_search_title"], COPY["features.web_search_desc"],
            guide_name="web_search",
        )
        card.add_content(self._web_search)

    def refresh(self) -> None:
        self._vision.set_state(self.context.vision)
        self._weather.set_state(self.context.weather)
        self.context.asr.enabled = False
        self._tts.set_state(self.context.tts)
        self._web_search.set_enabled(self.context.web_search.enabled)

    def save(self) -> bool:
        v = self._vision.state()
        if v["enabled"]:
            if not v["model"]:
                self.invalid_input.emit("开了「看懂图片」就要填一下视觉模型 ID")
                return False
            if v["preset"] != "main" and not v["api_key"]:
                self.invalid_input.emit("开了「看懂图片」就要填一下视觉 provider 的 API 密钥")
                return False
            if v["preset"] == "custom" and not v["base_url"]:
                self.invalid_input.emit("自定义视觉 provider 需要填 Base URL")
                return False

        w = self._weather.state()
        if w["enabled"]:
            if not w["host"]:
                self.invalid_input.emit("开了「查天气」就要填一下 API 主机（控制台拷过来）")
                return False
            if not w["api_key"]:
                self.invalid_input.emit("开了「查天气」就要填一下和风天气 API 密钥")
                return False

        # TTS
        t = self._tts.state()
        if t["enabled"]:
            if not self._tts.ensure_ready(self):
                return False

        # 写回 context
        self.context.vision.enabled = v["enabled"]
        self.context.vision.api_key = v["api_key"]
        self.context.vision.extra = {
            "preset": v["preset"],
            "model": v["model"],
            "base_url": v["base_url"],
        }

        self.context.weather.enabled = w["enabled"]
        self.context.weather.api_key = w["api_key"]
        self.context.weather.extra = {"host": w["host"]}

        self.context.asr.enabled = False
        self.context.asr.extra = {}

        self.context.tts.enabled = t["enabled"]
        self.context.tts.extra = t

        self.context.web_search.enabled = self._web_search.is_enabled()
        return True
