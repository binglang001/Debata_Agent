"""可选功能开关。

5 个 feature toggle + 长期记忆模式二选一：
    - 看懂图片（vision）：开关 + provider 选择 + model + API 密钥（+ 自定义时 base_url）
    - 听懂语音 / 自己说话（asr/tts）：P3 占位，仅开关
    - 查天气：开关 + 和风天气 API 主机 + API 密钥
    - 查网页（web_search）：开关，无需密钥
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..components import ApiKeyInput, SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ...theme import Spacing


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
        "model": "doubao-seed-1-6-vision-250815",
        "url": "https://ark.cn-beijing.volces.com/api/v3",
        "hint": "国内可直连。控制台 https://www.volcengine.com/product/ark 申请 API Key。",
    },
    "glm": {
        "display": "智谱 GLM-4V",
        "model": "glm-4v-flash",
        "url": "https://open.bigmodel.cn/api/paas/v4",
        "hint": "智谱开放平台，免费额度足够 demo。",
    },
    "qwen": {
        "display": "通义千问 · Qwen-VL",
        "model": "qwen-vl-max",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "hint": "阿里灵积控制台获取 API Key。",
    },
    "gemini": {
        "display": "Google Gemini",
        "model": "gemini-2.0-flash",
        "url": "https://generativelanguage.googleapis.com/v1beta",
        "hint": "国内访问需要代理；2.0 Flash 已自带视觉能力。",
    },
    "openai": {
        "display": "OpenAI GPT-4o",
        "model": "gpt-4o",
        "url": "https://api.openai.com/v1",
        "hint": "需国际信用卡 + 代理。",
    },
    "anthropic": {
        "display": "Anthropic Claude",
        "model": "claude-sonnet-4-5",
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


class _SimpleFeatureToggle(QFrame):
    """简易开关：复选框 + 描述。无密钥输入（用于 asr/tts/web_search）。"""

    def __init__(self, title: str, desc: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(title)
        head.addWidget(self._check)
        head.addStretch(1)
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
        self._model_edit.setPlaceholderText("如 doubao-seed-1-6-vision-250815")
        form.addRow(QLabel("视觉模型 ID"), self._model_edit)

        # 密钥
        self._key_input = ApiKeyInput(placeholder="该 provider 的 API 密钥")
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
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(Spacing.SM)

        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("yourdomain.qweatherapi.com")
        form.addRow(QLabel("API 主机"), self._host_edit)

        self._key_input = ApiKeyInput(placeholder="和风天气 API 密钥")
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

    def set_state(self, choice) -> None:
        self._check.setChecked(choice.enabled)
        extra = choice.extra or {}
        host = extra.get("host", "")
        if host:
            self._host_edit.setText(host)
        if choice.api_key:
            self._key_input.set_text(choice.api_key)


class FeaturesStepView(BaseStepView):
    """5 个 feature toggle + 长期记忆模式二选一。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="选些可选的本领",
            subtitle=(
                "这些功能默认关闭。打开哪个，Diana 就拥有哪个能力。\n"
                "现在不开也没关系，之后随时能在设置里打开。"
            ),
        )
        outer.addWidget(card)

        # vision
        self._vision = _VisionFeatureCard()
        card.add_content(self._vision)

        # weather
        self._weather = _WeatherFeatureCard()
        card.add_content(self._weather)

        # 简易 toggle（仅开关）
        self._asr = _SimpleFeatureToggle(
            COPY["features.asr_title"], COPY["features.asr_desc"],
        )
        self._tts = _SimpleFeatureToggle(
            COPY["features.tts_title"], COPY["features.tts_desc"],
        )
        self._web_search = _SimpleFeatureToggle(
            COPY["features.web_search_title"], COPY["features.web_search_desc"],
        )
        for w in (self._asr, self._tts, self._web_search):
            card.add_content(w)

        # 长期记忆模式
        sep = QFrame()
        sep.setProperty("role", "separator")
        card.add_content(sep)

        lt_title = QLabel(COPY["features.long_term_memory_title"])
        lt_title.setProperty("role", "title-3")
        card.add_content(lt_title)

        lt_desc = QLabel(COPY["features.long_term_memory_desc"])
        lt_desc.setProperty("role", "secondary")
        lt_desc.setWordWrap(True)
        card.add_content(lt_desc)

        self._lt_group = QButtonGroup(self)
        self._lt_group.setExclusive(True)

        self._lt_file = self._make_lt_choice(
            "file",
            COPY["features.lt_memory_file_title"],
            COPY["features.lt_memory_file_pros"] + "\n" + COPY["features.lt_memory_file_cons"],
        )
        self._lt_rag = self._make_lt_choice(
            "rag",
            COPY["features.lt_memory_rag_title"],
            COPY["features.lt_memory_rag_pros"] + "\n" + COPY["features.lt_memory_rag_cons"],
        )
        card.add_content(self._lt_file)
        card.add_content(self._lt_rag)

        recommend = QLabel(COPY["features.lt_memory_recommend"])
        recommend.setProperty("role", "secondary")
        recommend.setWordWrap(True)
        card.add_content(recommend)

        outer.addStretch(1)

    def _make_lt_choice(self, value: str, title: str, body: str) -> QFrame:
        wrapper = QFrame()
        wrapper.setObjectName("Card")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        rb = QRadioButton(title)
        rb.setProperty("lt_value", value)
        self._lt_group.addButton(rb)
        wl.addWidget(rb)
        b = QLabel(body)
        b.setProperty("role", "secondary")
        b.setWordWrap(True)
        b.setContentsMargins(24, 0, 0, 0)
        wl.addWidget(b)
        return wrapper

    def refresh(self) -> None:
        self._vision.set_state(self.context.vision)
        self._weather.set_state(self.context.weather)
        self._asr.set_enabled(self.context.asr.enabled)
        self._tts.set_enabled(self.context.tts.enabled)
        self._web_search.set_enabled(self.context.web_search.enabled)
        target = self._lt_file if self.context.long_term_memory_mode == "file" else self._lt_rag
        for rb in target.findChildren(QRadioButton):
            rb.setChecked(True)
            break

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

        self.context.asr.enabled = self._asr.is_enabled()
        self.context.tts.enabled = self._tts.is_enabled()
        self.context.web_search.enabled = self._web_search.is_enabled()

        # 长期记忆模式
        for rb in self._lt_file.findChildren(QRadioButton):
            if rb.isChecked():
                self.context.long_term_memory_mode = "file"
                break
        for rb in self._lt_rag.findChildren(QRadioButton):
            if rb.isChecked():
                self.context.long_term_memory_mode = "rag"
                break
        return True
