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

from ...theme import Spacing
from ..components import ApiKeyInput, SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY


def _load_presets() -> dict[str, dict]:
    """从 providers/presets/ 加载所有预设元数据（display / model / url）。"""
    try:
        from providers.presets_loader import load_all_presets
        presets_dir = Path(__file__).resolve().parent.parent.parent.parent / "providers" / "presets"
        presets = load_all_presets(presets_dir)
        result: dict[str, dict] = {}
        for pid, p in presets.items():
            first_model = p.models[0].id if p.models else ""
            result[pid] = {
                "display": p.display_name,
                "model": first_model,
                "url": p.base_url,
                "protocol": p.protocol,
            }
        return result
    except Exception:
        # fallback: 少量预设
        return {
            "deepseek": {"display": "DeepSeek", "model": "", "url": "https://api.deepseek.com/v1", "protocol": "openai_compat"},
            "anthropic": {"display": "Anthropic Claude", "model": "", "url": "https://api.anthropic.com/v1", "protocol": "anthropic"},
            "openai": {"display": "OpenAI", "model": "", "url": "https://api.openai.com/v1", "protocol": "openai_compat"},
        }


_PRESET_DEFAULTS: dict[str, dict] = _load_presets()


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
        self._base_url_edit.textChanged.connect(lambda *_: self._key_input.set_test_state("idle") if hasattr(self, "_key_input") else None)
        self._base_url_label = QLabel("Base URL")
        form.addRow(self._base_url_label, self._base_url_edit)

        # 模型（可编辑下拉 + 获取按钮）
        model_row = QHBoxLayout()
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setMinimumWidth(280)
        self._model_combo.setPlaceholderText("点击「获取模型」或手动输入模型 ID")
        self._model_combo.currentTextChanged.connect(lambda *_: self._key_input.set_test_state("idle") if hasattr(self, "_key_input") else None)
        model_row.addWidget(self._model_combo, 1)
        self._fetch_btn = QPushButton("获取模型")
        self._fetch_btn.setProperty("role", "secondary")
        self._fetch_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self._fetch_btn)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        form.addRow(QLabel(COPY["main_model_custom.model_label"]), model_wrap)

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
        else:
            self._base_url_label.setVisible(False)
            self._base_url_edit.setVisible(False)
        # 切预设时清空模型下拉（用户需点「获取模型」）
        self._model_combo.clear()
        self._model_combo.setCurrentText("")

    def _on_fetch_models(self) -> None:
        """调用 provider API 获取可用模型列表。"""
        preset = self._provider_combo.currentData()
        if preset == "custom":
            url = self._base_url_edit.text().strip()
            if not url:
                self._key_input.set_test_state("error", "自定义模式需要先填 Base URL")
                return
        else:
            info = _PRESET_DEFAULTS.get(preset, {})
            url = info.get("url", "")
        key = self._key_input.text().strip()
        if not key:
            self._key_input.set_test_state("error", "请先填 API 密钥")
            return

        protocol = "anthropic" if preset == "anthropic" else "openai_compat"
        self._fetch_btn.setEnabled(False)
        self._fetch_btn.setText("获取中...")

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._fetch_btn.setEnabled(True)
            self._fetch_btn.setText("获取模型")
            return

        async def _do_fetch():
            try:
                from providers.model_fetcher import fetch_model_list
                from providers.registry import normalize_base_url
                normalized = normalize_base_url(url, protocol)
                models = await fetch_model_list(normalized, key, protocol, timeout=8.0)
                self._model_combo.clear()
                for m in models:
                    self._model_combo.addItem(m)
                if models:
                    self._model_combo.setCurrentIndex(0)
                self._key_input.set_test_state("success", f"已获取 {len(models)} 个模型")
            except Exception as e:
                self._key_input.set_test_state("error", f"获取失败：{e}")
            finally:
                self._fetch_btn.setEnabled(True)
                self._fetch_btn.setText("获取模型")

        loop.create_task(_do_fetch())

    def refresh(self) -> None:
        m = self.context.main
        idx = self._provider_combo.findData(m.preset or "custom")
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        if m.model:
            self._model_combo.setCurrentText(m.model)
        self._base_url_edit.setText(m.base_url)
        if m.api_key:
            self._key_input.set_text(m.api_key)
        self._temp_spin.setValue(m.temperature)
        self._top_p_spin.setValue(m.top_p)
        self._max_tokens_spin.setValue(m.max_tokens)
        self._reasoning_check.setChecked(m.reasoning_enabled)
        idx2 = self._budget_combo.findData(m.reasoning_budget)
        if idx2 >= 0:
            self._budget_combo.setCurrentIndex(idx2)
        self._reasoning_tokens_spin.setValue(m.reasoning_max_tokens or 0)
        self._on_reasoning_toggled(m.reasoning_enabled)

    def save(self) -> bool:
        preset = self._provider_combo.currentData()
        model = self._model_combo.currentText().strip()
        key = self._key_input.text().strip()
        base_url = self._base_url_edit.text().strip()

        if not model:
            self.invalid_input.emit("请选一个模型或手动输入模型 ID")
            return False
        if not key:
            self.invalid_input.emit("请填一下 API 密钥")
            return False
        if preset == "custom" and not base_url:
            self.invalid_input.emit("自定义模式需要 Base URL")
            return False

        # URL 规范化
        if preset != "custom" and preset != "anthropic":
            from providers.registry import normalize_base_url
            _ = normalize_base_url  # imported for use in window.py persist

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

    async def validate_before_next(self) -> bool:
        if self._key_input.is_test_success():
            return True
        self._key_input.set_test_state("testing", "正在自动测试主模型连接……")
        ok, message = await self._test_current()
        self._key_input.set_test_state("success" if ok else "error", message)
        if not ok:
            self.invalid_input.emit(message)
        return ok

    async def _test_current(self) -> tuple[bool, str]:
        preset = self._provider_combo.currentData()
        model = self._model_combo.currentText().strip() or "test"
        key = self._key_input.text().strip()
        info = _PRESET_DEFAULTS.get(preset, {})
        url = self._base_url_edit.text().strip() if preset == "custom" else info.get("url", "")
        if not key:
            return False, "请填一下 API 密钥"
        if not url:
            return False, "缺少 Base URL"
        try:
            from providers import probe_provider_endpoint
            from providers.registry import normalize_base_url

            protocol = info.get("protocol", "anthropic" if preset == "anthropic" else "openai_compat")
            normalized = normalize_base_url(url, protocol)
            result = await probe_provider_endpoint(
                protocol=protocol,
                base_url=normalized,
                api_key=key,
                model=model,
                timeout_seconds=8.0,
            )
            if result.status == "ok":
                return True, f"已就位（{result.latency_ms}ms）"
            return False, result.message
        except Exception as e:  # noqa: BLE001
            return False, f"未能完成：{e}"

    def _on_test(self, key: str) -> None:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._key_input.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            ok, message = await self._test_current()
            self._key_input.set_test_state("success" if ok else "error", message)

        loop.create_task(_do_test())


__all__ = ["MainModelCustomStepView"]
