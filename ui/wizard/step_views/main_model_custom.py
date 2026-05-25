"""主模型 - 自定义路径：选 provider / 模型 / 参数。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import Qt
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
    QVBoxLayout,
    QWidget,
)

from ..components import ApiKeyInput, SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ...theme import Spacing


_PRESET_DEFAULTS: dict[str, dict] = {
    "deepseek": {"display": "DeepSeek", "model": "deepseek-v4-flash", "url": "https://api.deepseek.com/v1"},
    "anthropic": {"display": "Anthropic Claude", "model": "claude-sonnet-4-5", "url": "https://api.anthropic.com/v1"},
    "openai": {"display": "OpenAI", "model": "gpt-4o", "url": "https://api.openai.com/v1"},
    "gemini": {"display": "Google Gemini", "model": "gemini-2.0-flash", "url": "https://generativelanguage.googleapis.com/v1beta"},
    "glm": {"display": "智谱 GLM", "model": "glm-4-flash", "url": "https://open.bigmodel.cn/api/paas/v4"},
    "qwen": {"display": "阿里通义", "model": "qwen-plus", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "moonshot": {"display": "Moonshot Kimi", "model": "moonshot-v1-8k", "url": "https://api.moonshot.cn/v1"},
    "openrouter": {"display": "OpenRouter", "model": "anthropic/claude-3.5-sonnet", "url": "https://openrouter.ai/api/v1"},
    "siliconflow": {"display": "硅基流动", "model": "deepseek-ai/DeepSeek-V3", "url": "https://api.siliconflow.cn/v1"},
    "volcengine": {"display": "火山引擎豆包", "model": "doubao-seed-1-6-vision", "url": "https://ark.cn-beijing.volces.com/api/v3"},
}


class MainModelCustomStepView(BaseStepView):
    """完整自定义：provider 选择 + 模型 + 温度/top_p/max_tokens + reasoning。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="选个主模型",
            subtitle="你可以选择内置预设，或填入完全自定义的提供商。",
        )
        outer.addWidget(card)

        form = QFormLayout()
        form.setSpacing(Spacing.MD)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Provider preset
        self._provider_combo = QComboBox()
        for preset, info in _PRESET_DEFAULTS.items():
            self._provider_combo.addItem(info["display"], preset)
        self._provider_combo.addItem("自行填一个（自定义）", "custom")
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow(QLabel(COPY["main_model_custom.provider_label"]), self._provider_combo)

        # 自定义 base_url（仅 custom 时显示）
        self._base_url_edit = QLineEdit()
        self._base_url_edit.setPlaceholderText("https://api.example.com/v1")
        self._base_url_label = QLabel("Base URL")
        form.addRow(self._base_url_label, self._base_url_edit)

        # 模型
        self._model_edit = QLineEdit()
        self._model_edit.setPlaceholderText("deepseek-v4-flash")
        form.addRow(QLabel(COPY["main_model_custom.model_label"]), self._model_edit)

        # 密钥（自带测试连接 - 但测试需要 base_url + model，所以这里用同一个组件）
        self._key_input = ApiKeyInput(placeholder="sk-... 或对应平台的密钥")
        self._key_input.test_requested.connect(self._on_test)
        form.addRow(QLabel("API 密钥"), self._key_input)

        # temperature
        self._temp_spin = QDoubleSpinBox()
        self._temp_spin.setRange(0.0, 2.0)
        self._temp_spin.setSingleStep(0.1)
        self._temp_spin.setValue(0.6)
        form.addRow(QLabel(COPY["main_model_custom.temperature_label"]), self._temp_spin)
        form.addRow(QLabel(""), self._hint(COPY["main_model_custom.temperature_hint"]))

        # top_p
        self._top_p_spin = QDoubleSpinBox()
        self._top_p_spin.setRange(0.0, 1.0)
        self._top_p_spin.setSingleStep(0.05)
        self._top_p_spin.setValue(1.0)
        form.addRow(QLabel(COPY["main_model_custom.top_p_label"]), self._top_p_spin)

        # max_tokens
        self._max_tokens_spin = QSpinBox()
        self._max_tokens_spin.setRange(64, 131072)
        self._max_tokens_spin.setSingleStep(1024)
        self._max_tokens_spin.setValue(16384)
        form.addRow(QLabel(COPY["main_model_custom.max_tokens_label"]), self._max_tokens_spin)

        # reasoning
        self._reasoning_check = QCheckBox(COPY["main_model_custom.reasoning_label"])
        self._reasoning_check.toggled.connect(self._on_reasoning_toggled)
        form.addRow(QLabel(""), self._reasoning_check)
        form.addRow(QLabel(""), self._hint(COPY["main_model_custom.reasoning_hint"]))

        # 思考深度 budget
        self._budget_combo = QComboBox()
        self._budget_combo.addItem("默认（不指定）", None)
        self._budget_combo.addItem("低 · 快但浅", "low")
        self._budget_combo.addItem("中 · 平衡", "medium")
        self._budget_combo.addItem("高 · 慢但深", "high")
        form.addRow(QLabel("思考深度"), self._budget_combo)
        form.addRow(QLabel(""), self._hint(
            "Claude / Gemini 用 budget 控制思考力度；DeepSeek-R1 等不读此项可留默认。"
        ))

        # 思考阶段 max_tokens（可选）
        self._reasoning_tokens_spin = QSpinBox()
        self._reasoning_tokens_spin.setRange(0, 65536)
        self._reasoning_tokens_spin.setSingleStep(1024)
        self._reasoning_tokens_spin.setValue(0)
        self._reasoning_tokens_spin.setSpecialValueText("不指定")
        form.addRow(QLabel("思考 token 上限"), self._reasoning_tokens_spin)
        form.addRow(QLabel(""), self._hint(
            "仅 Claude thinking 等需要单独控制思考阶段长度时填；留「不指定」即可。"
        ))

        card.add_layout(form)
        outer.addStretch(1)

        # 初始化：默认 deepseek
        self._provider_combo.setCurrentIndex(0)
        self._on_provider_changed(0)
        self._on_reasoning_toggled(False)

    def _hint(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "secondary")
        lbl.setWordWrap(True)
        return lbl

    def _on_reasoning_toggled(self, on: bool) -> None:
        self._budget_combo.setEnabled(on)
        self._reasoning_tokens_spin.setEnabled(on)

    def _on_provider_changed(self, idx: int) -> None:
        preset = self._provider_combo.itemData(idx)
        if preset == "custom":
            self._base_url_label.setVisible(True)
            self._base_url_edit.setVisible(True)
            if not self._model_edit.text():
                self._model_edit.setText("")
        else:
            info = _PRESET_DEFAULTS.get(preset, {})
            self._base_url_label.setVisible(False)
            self._base_url_edit.setVisible(False)
            # 切到预设时自动填默认 model（如果用户没改过）
            if not self._model_edit.text() or self._is_known_default(self._model_edit.text()):
                self._model_edit.setText(info.get("model", ""))

    def _is_known_default(self, model: str) -> bool:
        return any(info["model"] == model for info in _PRESET_DEFAULTS.values())

    def refresh(self) -> None:
        m = self.context.main
        # 选 provider
        idx = self._provider_combo.findData(m.preset or "custom")
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        self._model_edit.setText(m.model)
        self._base_url_edit.setText(m.base_url)
        if m.api_key:
            self._key_input.set_text(m.api_key)
        self._temp_spin.setValue(m.temperature)
        self._top_p_spin.setValue(m.top_p)
        self._max_tokens_spin.setValue(m.max_tokens)
        self._reasoning_check.setChecked(m.reasoning_enabled)
        # budget
        idx2 = self._budget_combo.findData(m.reasoning_budget)
        if idx2 >= 0:
            self._budget_combo.setCurrentIndex(idx2)
        # max tokens（0 = 不指定）
        self._reasoning_tokens_spin.setValue(m.reasoning_max_tokens or 0)
        self._on_reasoning_toggled(m.reasoning_enabled)

    def save(self) -> bool:
        preset = self._provider_combo.currentData()
        model = self._model_edit.text().strip()
        key = self._key_input.text().strip()
        base_url = self._base_url_edit.text().strip()

        if not model:
            self.invalid_input.emit("请填一下模型 ID（如 deepseek-chat）")
            return False
        if not key:
            self.invalid_input.emit("请填一下 API 密钥")
            return False
        if preset == "custom" and not base_url:
            self.invalid_input.emit("自定义模式需要 Base URL")
            return False

        info = _PRESET_DEFAULTS.get(preset, {})
        m = self.context.main
        m.preset = preset
        m.display_name = info.get("display", preset)
        m.api_key = key
        m.model = model
        m.base_url = base_url if preset == "custom" else ""
        m.protocol = "anthropic" if preset == "anthropic" else "openai_compat"
        m.temperature = self._temp_spin.value()
        m.top_p = self._top_p_spin.value()
        m.max_tokens = self._max_tokens_spin.value()
        m.reasoning_enabled = self._reasoning_check.isChecked()
        if m.reasoning_enabled:
            m.reasoning_budget = self._budget_combo.currentData()
            tok = self._reasoning_tokens_spin.value()
            m.reasoning_max_tokens = tok if tok > 0 else None
        else:
            m.reasoning_budget = None
            m.reasoning_max_tokens = None
        return True

    def _on_test(self, key: str) -> None:
        preset = self._provider_combo.currentData()
        model = self._model_edit.text().strip() or "test"
        info = _PRESET_DEFAULTS.get(preset, {})
        url = self._base_url_edit.text().strip() if preset == "custom" else info.get("url", "")
        if not url:
            self._key_input.set_test_state("error", "缺少 Base URL")
            return

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._key_input.set_test_state("error", "事件循环未就绪")
            return

        is_anthropic = preset == "anthropic"

        async def _do_test() -> None:
            try:
                if is_anthropic:
                    from providers import AnthropicProvider
                    provider = AnthropicProvider(
                        "wizard_test",
                        base_url=url, api_key=key, timeout=20.0,
                    )
                else:
                    from providers import OpenAICompatProvider
                    provider = OpenAICompatProvider(
                        "wizard_test",
                        base_url=url, api_key=key, timeout=20.0,
                    )
                try:
                    result = await provider.chat_completion(
                        messages=[{"role": "user", "content": "hi"}],
                        model=model,
                        tools=None,
                        temperature=0.1,
                        max_tokens=5,
                        stream=False,
                        timeout=20.0,
                    )
                finally:
                    await provider.aclose()
                self._key_input.set_test_state("success", "已就位")
            except Exception as e:  # noqa: BLE001
                self._key_input.set_test_state("error", f"未能完成：{e}")

        loop.create_task(_do_test())


__all__ = ["MainModelCustomStepView", "_PRESET_DEFAULTS"]
