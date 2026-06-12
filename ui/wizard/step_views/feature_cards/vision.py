"""Vision feature card."""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ....theme import Spacing
from ....widgets.model_combo import ModelComboBox
from ...components import ApiKeyInput
from ...context import WizardContext
from ...copy import COPY
from .._shared import _add_guide_button

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
    "xai": {
        "display": "xAI Grok",
        "model": "grok-4.3",
        "url": "https://api.x.ai/v1",
        "hint": "Grok 4.3 支持多模态 vision；控制台 https://x.ai/api 获取 API Key。",
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


class _VisionFeatureCard(QFrame):
    """看懂图片：开关 + provider 选择 + model + API 密钥（custom 时含 base_url）。"""

    def __init__(self, context: WizardContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._context = context
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(COPY["features.vision_title"])
        self._check.toggled.connect(self._on_enabled_changed)
        head.addWidget(self._check)
        head.addStretch(1)
        _add_guide_button(head, "vision", self)
        outer.addLayout(head)

        d = QLabel(COPY["features.vision_desc"])
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

        self._preset_combo = QComboBox()
        for key, info in _VISION_PRESETS.items():
            self._preset_combo.addItem(info["display"], key)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow(QLabel("视觉提供商"), self._preset_combo)

        self._base_url_label = QLabel("Base URL")
        self._base_url_edit = QLineEdit()
        self._base_url_edit.setPlaceholderText("https://api.example.com/v1")
        form.addRow(self._base_url_label, self._base_url_edit)

        model_row = QHBoxLayout()
        self._model_edit = ModelComboBox()
        self._model_edit.setPlaceholderText("如 doubao-seed-2-0-lite-260428 / glm-5v-turbo / gpt-5.5")
        model_row.addWidget(self._model_edit, 1)
        self._fetch_models_btn = QPushButton("获取模型")
        self._fetch_models_btn.setProperty("role", "secondary")
        self._fetch_models_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self._fetch_models_btn)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        form.addRow(QLabel("视觉模型 ID"), model_wrap)

        self._key_input = ApiKeyInput(placeholder="该 provider 的 API 密钥")
        self._key_input.test_requested.connect(self._on_test)
        form.addRow(QLabel("API 密钥"), self._key_input)

        body_layout.addLayout(form)

        self._hint_lbl = QLabel("")
        self._hint_lbl.setProperty("role", "secondary")
        self._hint_lbl.setWordWrap(True)
        body_layout.addWidget(self._hint_lbl)

        outer.addWidget(self._body)
        self._body.setVisible(False)

        idx = self._preset_combo.findData(self._recommended_vision_preset())
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)
        self._on_preset_changed(idx if idx >= 0 else 0)

    def _toggle_body(self, on: bool) -> None:
        self._body.setVisible(on)

    def _on_enabled_changed(self, on: bool) -> None:
        if on:
            self._apply_smart_default()
        self._toggle_body(on)

    def _on_preset_changed(self, idx: int) -> None:
        preset = self._preset_combo.itemData(idx) or "volcengine"
        info = _VISION_PRESETS.get(preset, {})

        is_custom = preset == "custom"
        self._base_url_label.setVisible(is_custom)
        self._base_url_edit.setVisible(is_custom)

        is_main = preset == "main"
        self._key_input.setEnabled(not is_main)
        self._fetch_models_btn.setEnabled(not is_main)

        cur = self._model_edit.current_model_id()
        known_defaults = {p["model"] for p in _VISION_PRESETS.values() if p["model"]}
        if not cur or cur in known_defaults:
            if is_main:
                self._model_edit.setEditText(self._context.main.model)
            else:
                self._model_edit.setEditText(info.get("model", ""))

        self._hint_lbl.setText(self._vision_hint(preset, info.get("hint", "")))

    def _apply_smart_default(self) -> None:
        if self._main_supports_vision():
            idx = self._preset_combo.findData("main")
            if idx >= 0:
                self._preset_combo.setCurrentIndex(idx)
            self._model_edit.setEditText(self._context.main.model)
            return
        preset = self._recommended_vision_preset()
        idx = self._preset_combo.findData(preset)
        if idx >= 0:
            self._preset_combo.setCurrentIndex(idx)
        model = self._recommended_vision_model(preset)
        if model:
            self._model_edit.setEditText(model)

    def _main_supports_vision(self) -> bool:
        try:
            from providers.model_capabilities import model_supports

            return model_supports(self._context.main.preset, self._context.main.model, "vision")
        except Exception:
            return False

    def _recommended_vision_preset(self) -> str:
        try:
            from providers.model_capabilities import recommended_provider

            provider = recommended_provider("vision")
            if provider and provider.id in _VISION_PRESETS:
                return provider.id
        except Exception:
            pass
        return "volcengine"

    def _recommended_vision_model(self, preset: str) -> str:
        try:
            from providers.model_capabilities import recommended_model

            model = recommended_model(preset, "vision")
            if model:
                return model.id
        except Exception:
            pass
        return _VISION_PRESETS.get(preset, {}).get("model", "")

    def _vision_hint(self, preset: str, fallback: str) -> str:
        if preset == "main":
            if self._main_supports_vision():
                return "主模型已知支持图像理解，可复用同一个 provider 和密钥。"
            return "当前主模型未在能力文件中标记为支持图像理解。若你确认它支持，可手动使用。"
        return fallback

    def _on_fetch_models(self) -> None:
        preset = self._preset_combo.currentData() or "volcengine"
        if preset == "main":
            self._hint_lbl.setText("复用主模型时无需单独获取视觉模型。")
            return
        base_url = (
            self._base_url_edit.text().strip()
            if preset == "custom"
            else _VISION_PRESETS.get(preset, {}).get("url", "")
        )
        key = self._key_input.text().strip()
        if not base_url:
            self._hint_lbl.setText("请先填写 Base URL。")
            return
        if not key:
            self._key_input.set_test_state("error", "请先填写 API 密钥")
            return

        self._fetch_models_btn.setEnabled(False)
        self._fetch_models_btn.setText("获取中...")

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._fetch_models_btn.setEnabled(True)
            self._fetch_models_btn.setText("获取模型")
            self._hint_lbl.setText("事件循环未就绪")
            return

        async def _do_fetch() -> None:
            try:
                from providers.model_fetcher import fetch_model_infos
                from providers.registry import normalize_base_url

                protocol = "anthropic" if preset == "anthropic" else "openai_compat"
                models = await fetch_model_infos(
                    normalize_base_url(base_url, protocol),
                    key,
                    protocol,
                    provider_id="" if preset == "custom" else str(preset),
                    timeout=8.0,
                )
                self._model_edit.set_models(
                    [m.id for m in models],
                    provider_id="" if preset == "custom" else str(preset),
                )
                self._key_input.set_test_state("success", f"已获取 {len(models)} 个模型")
            except Exception as e:
                self._key_input.set_test_state("error", f"获取失败：{e}")
            finally:
                self._fetch_models_btn.setEnabled(preset != "main")
                self._fetch_models_btn.setText("获取模型")

        loop.create_task(_do_fetch())

    async def _test_current(self) -> tuple[bool, str]:
        preset = self._preset_combo.currentData() or "volcengine"
        if preset == "main":
            return False, "复用主模型时请在主模型页面测试连接"
        model = self._model_edit.current_model_id()
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
            "model": self._model_edit.current_model_id(),
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
            self._model_edit.setEditText(extra["model"])
        if extra.get("base_url"):
            self._base_url_edit.setText(extra["base_url"])
        if choice.api_key:
            self._key_input.set_text(choice.api_key)


__all__ = ["_VISION_PRESETS", "_VisionFeatureCard"]
