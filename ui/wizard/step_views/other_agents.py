"""子 Agent：proactive / summary 的配置（自定义路径专属）。"""

from __future__ import annotations

import asyncio

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ...theme import Spacing
from ..components import ApiKeyInput, SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from .main_model_custom import _load_presets


class _SubAgentBlock(QWidget):
    """单个子 Agent 配置块：标题 + 「和主模型一样」复选 + 详细字段（取消复选时显示）。"""

    def __init__(self, title: str, desc: str, parent=None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.SM)

        t = QLabel(title)
        t.setProperty("role", "title-3")
        outer.addWidget(t)

        d = QLabel(desc)
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        outer.addWidget(d)

        self._use_main = QCheckBox(COPY["other_agents.use_main_model"])
        self._use_main.setChecked(True)
        self._use_main.toggled.connect(self._update_visibility)
        outer.addWidget(self._use_main)

        self._enabled = QCheckBox("启用此 Agent")
        self._enabled.setChecked(True)
        outer.addWidget(self._enabled)

        # 详细字段（取消复选才显示）
        self._detail = QWidget()
        form = QFormLayout(self._detail)
        form.setSpacing(Spacing.SM)

        self._provider = QComboBox()
        presets = _load_presets()
        for preset, info in presets.items():
            self._provider.addItem(info["display"], preset)
        form.addRow(QLabel("提供商"), self._provider)

        self._model = QLineEdit()
        self._model.setPlaceholderText("模型 ID")
        form.addRow(QLabel("模型"), self._model)

        self._key = ApiKeyInput(placeholder="API 密钥")
        form.addRow(QLabel("密钥"), self._key)
        self._provider.currentIndexChanged.connect(lambda *_: self._key.set_test_state("idle"))
        self._model.textChanged.connect(lambda *_: self._key.set_test_state("idle"))
        self._key.test_requested.connect(self._on_test)

        outer.addWidget(self._detail)

        # 思考（reasoning）配置 —— 不论 use_main 都可以独立调
        rea_row = QHBoxLayout()
        rea_row.setSpacing(Spacing.SM)
        self._reasoning_check = QCheckBox("启用思考")
        rea_row.addWidget(self._reasoning_check)
        self._budget_combo = QComboBox()
        self._budget_combo.addItem("默认", None)
        self._budget_combo.addItem("低", "low")
        self._budget_combo.addItem("中", "medium")
        self._budget_combo.addItem("高", "high")
        self._budget_combo.setEnabled(False)
        self._reasoning_check.toggled.connect(self._budget_combo.setEnabled)
        rea_row.addWidget(QLabel("思考深度"))
        rea_row.addWidget(self._budget_combo)
        rea_row.addStretch(1)
        outer.addLayout(rea_row)

        self._update_visibility(True)

    def _update_visibility(self, use_main: bool) -> None:
        self._detail.setVisible(not use_main)

    def state(self) -> dict:
        return {
            "use_main": self._use_main.isChecked(),
            "enabled": self._enabled.isChecked(),
            "preset": self._provider.currentData(),
            "model": self._model.text().strip(),
            "api_key": self._key.text(),
            "reasoning_enabled": self._reasoning_check.isChecked(),
            "reasoning_budget": self._budget_combo.currentData() if self._reasoning_check.isChecked() else None,
        }

    def is_test_success(self) -> bool:
        return self._use_main.isChecked() or not self._enabled.isChecked() or self._key.is_test_success()

    async def validate(self) -> tuple[bool, str]:
        if self.is_test_success():
            return True, ""
        self._key.set_test_state("testing", "正在自动测试连接……")
        ok, message = await self._test_current()
        self._key.set_test_state("success" if ok else "error", message)
        return ok, message

    async def _test_current(self) -> tuple[bool, str]:
        preset = self._provider.currentData()
        model = self._model.text().strip()
        key = self._key.text().strip()
        if not model:
            return False, "请先填模型 ID"
        if not key:
            return False, "请先填 API 密钥"
        try:
            from providers import probe_provider_endpoint
            from providers.registry import normalize_base_url

            from .main_model_custom import _PRESET_DEFAULTS

            info = _PRESET_DEFAULTS.get(preset, {})
            protocol = info.get("protocol", "anthropic" if preset == "anthropic" else "openai_compat")
            base_url = normalize_base_url(info.get("url", ""), protocol)
            result = await probe_provider_endpoint(
                protocol=protocol,
                base_url=base_url,
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
            self._key.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            ok, message = await self._test_current()
            self._key.set_test_state("success" if ok else "error", message)

        loop.create_task(_do_test())

    def set_state(self, choice) -> None:
        self._enabled.setChecked(choice.enabled)
        self._use_main.setChecked(choice.use_main)
        if choice.preset:
            idx = self._provider.findData(choice.preset)
            if idx >= 0:
                self._provider.setCurrentIndex(idx)
        if choice.model:
            self._model.setText(choice.model)
        if choice.api_key:
            self._key.set_text(choice.api_key)
        self._reasoning_check.setChecked(choice.reasoning_enabled)
        idx2 = self._budget_combo.findData(choice.reasoning_budget)
        if idx2 >= 0:
            self._budget_combo.setCurrentIndex(idx2)
        self._budget_combo.setEnabled(choice.reasoning_enabled)


class OtherAgentsStepView(BaseStepView):
    """proactive + summary 配置。本步可跳过（默认都用主模型）。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="其它模型",
            subtitle=(
                "Debata 内部用了三类模型：主聊天（你刚才配的）、主动思考（小模型）、"
                "历史总结（中型模型）。\n本步可以跳过，默认都用主模型。"
            ),
        )
        outer.addWidget(card)

        self._proactive = _SubAgentBlock(
            COPY["other_agents.proactive_title"],
            COPY["other_agents.proactive_desc"],
        )
        card.add_content(self._proactive)

        card.add_content(self._separator())

        self._summary = _SubAgentBlock(
            COPY["other_agents.summary_title"],
            COPY["other_agents.summary_desc"],
        )
        card.add_content(self._summary)

        outer.addStretch(1)

    def _separator(self):
        from PySide6.QtWidgets import QFrame
        sep = QFrame()
        sep.setProperty("role", "separator")
        return sep

    def refresh(self) -> None:
        self._proactive.set_state(self.context.proactive)
        self._summary.set_state(self.context.summary)

    def save(self) -> bool:
        p = self._proactive.state()
        s = self._summary.state()
        # 校验：如果不复用主模型且 enabled，则要填 model / key
        for label, st in [("主动思考", p), ("历史总结", s)]:
            if st["enabled"] and not st["use_main"]:
                if not st["model"] or not st["api_key"]:
                    self.invalid_input.emit(f"「{label}」未选「和主模型一样」，请填模型 ID 和密钥")
                    return False
        self.context.proactive.enabled = p["enabled"]
        self.context.proactive.use_main = p["use_main"]
        if not p["use_main"]:
            self.context.proactive.preset = p["preset"]
            self.context.proactive.model = p["model"]
            self.context.proactive.api_key = p["api_key"]
        self.context.proactive.temperature = 0.3
        self.context.proactive.max_tokens = 64
        self.context.proactive.reasoning_enabled = p["reasoning_enabled"]
        self.context.proactive.reasoning_budget = p["reasoning_budget"]

        self.context.summary.enabled = s["enabled"]
        self.context.summary.use_main = s["use_main"]
        if not s["use_main"]:
            self.context.summary.preset = s["preset"]
            self.context.summary.model = s["model"]
            self.context.summary.api_key = s["api_key"]
        self.context.summary.temperature = 0.2
        self.context.summary.max_tokens = 8192
        self.context.summary.reasoning_enabled = s["reasoning_enabled"]
        self.context.summary.reasoning_budget = s["reasoning_budget"]
        return True

    async def validate_before_next(self) -> bool:
        for label, block in [("主动思考", self._proactive), ("历史总结", self._summary)]:
            ok, message = await block.validate()
            if not ok:
                self.invalid_input.emit(f"「{label}」连接检测失败：{message}")
                return False
        return True
