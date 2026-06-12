"""Persona management dialogs used by the dashboard."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
)

from agents.persona_gen_agent import (
    PersonaBrief,
    PersonaGenAgent,
    build_persona_refine_user_message,
)
from ui.wizard.context import (
    WizardContext,
    append_persona_edit_history,
)
from ui.wizard.persona_creator import PersonaCreatorStepView

from ..widgets import FramelessDialog, show_message

logger = logging.getLogger(__name__)


class _PersonaCreatorDialog(FramelessDialog):
    """仪表盘里复用向导的人格生成界面。"""

    def __init__(self, context: WizardContext, runtime=None, parent=None) -> None:
        super().__init__("新建角色", parent)
        self.setMinimumSize(1100, 760)
        self._creator = PersonaCreatorStepView(context, self)
        self._creator.usage_recorder = getattr(runtime, "_record_model_usage", None)
        self._creator.status_callback = getattr(runtime, "_update_model_activity", None)
        self._creator.invalid_input.connect(
            lambda msg: show_message(self, "还没完成", msg, is_danger=True)
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._creator)
        self.body_layout().addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setProperty("role", "secondary")
        cancel_btn.clicked.connect(self.reject)
        actions.addWidget(cancel_btn)
        save_btn = QPushButton("保存角色")
        save_btn.setProperty("role", "primary")
        save_btn.clicked.connect(self._on_accept)
        actions.addWidget(save_btn)
        self.body_layout().addLayout(actions)
        self._creator.refresh()

    def _on_accept(self) -> None:
        if self._creator.save():
            self.accept()


class _PersonaRefineDialog(FramelessDialog):
    """修改已有人格。只输入调整要求，结果确认后由调用方覆盖文件。"""

    def __init__(
        self,
        persona_name: str,
        current_prompt: str,
        context: WizardContext,
        runtime=None,
        parent=None,
    ) -> None:
        super().__init__(f"修改角色 · {persona_name}", parent)
        self.setMinimumSize(900, 680)
        self.result_prompt = ""
        self._persona_name = persona_name
        self._current_prompt = current_prompt
        self._context = context
        self._runtime = runtime
        self._agent: PersonaGenAgent | None = self._build_persona_agent()
        self._refined_count = 0
        self._edit_history: list[dict[str, str]] = []

        title = QLabel(f"当前角色：{persona_name}")
        title.setProperty("role", "title-3")
        self.body_layout().addWidget(title)

        hint = QLabel("写明要调整的人格方向。请客观描述修改要求，不要像聊天一样对角色说话。")
        hint.setProperty("role", "secondary")
        hint.setWordWrap(True)
        self.body_layout().addWidget(hint)

        self._feedback = QPlainTextEdit()
        self._feedback.setPlaceholderText("例如：说话更冷一点，但熟人面前会多解释；减少客服口吻；保留偶尔长段解释。")
        self._feedback.setFixedHeight(100)
        self.body_layout().addWidget(self._feedback)

        self._status = QLabel("")
        self._status.setProperty("role", "secondary")
        self.body_layout().addWidget(self._status)

        self._preview = QPlainTextEdit()
        self._preview.setPlainText(current_prompt)
        self._preview.setReadOnly(True)
        self.body_layout().addWidget(self._preview, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setProperty("role", "secondary")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        self._generate_btn = QPushButton("生成修改版")
        self._generate_btn.setProperty("role", "secondary")
        self._generate_btn.clicked.connect(self._on_generate)
        row.addWidget(self._generate_btn)
        self._save_btn = QPushButton("使用此版本")
        self._save_btn.setProperty("role", "primary")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_accept)
        row.addWidget(self._save_btn)
        self.body_layout().addLayout(row)

    def _build_persona_agent(self) -> PersonaGenAgent | None:
        from app_config.schema import AgentConfig
        from providers import AnthropicProvider, OpenAICompatProvider

        m = self._context.main
        if not m.api_key:
            return None
        base_url = m.base_url
        if not base_url and m.preset != "custom":
            from ui.wizard.step_views.main_model_custom import _PRESET_DEFAULTS

            base_url = _PRESET_DEFAULTS.get(m.preset, {}).get("url", "")
        if not base_url:
            return None
        if m.protocol == "anthropic":
            provider = AnthropicProvider("dashboard_persona_refine", base_url=base_url, api_key=m.api_key, timeout=180.0)
        else:
            provider = OpenAICompatProvider("dashboard_persona_refine", base_url=base_url, api_key=m.api_key, timeout=180.0)
        reasoning = None
        if m.reasoning_enabled:
            from app_config.schema import ReasoningConfig

            reasoning = ReasoningConfig(
                enabled=True,
                budget=m.reasoning_budget,
                max_tokens=m.reasoning_max_tokens,
            )
        cfg = AgentConfig(
            provider=m.preset or "deepseek",
            model=m.model,
            temperature=m.temperature,
            top_p=m.top_p,
            max_tokens=max(8192, m.max_tokens),
            reasoning=reasoning,
            first_token_timeout_seconds=60.0,
        )
        return PersonaGenAgent(
            provider,
            cfg,
            usage_recorder=getattr(self._runtime, "_record_model_usage", None),
            status_callback=getattr(self._runtime, "_update_model_activity", None),
        )

    def _on_generate(self) -> None:
        import asyncio

        feedback = self._feedback.toPlainText().strip()
        if not feedback:
            self._status.setText("先写修改要求")
            return
        if self._agent is None:
            self._status.setText("人格生成模型未就绪")
            return
        self._status.setText("正在生成修改版……")
        self._generate_btn.setEnabled(False)
        self._save_btn.setEnabled(False)

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._status.setText("事件循环未就绪")
            self._generate_btn.setEnabled(True)
            return

        async def _do() -> None:
            try:
                current_prompt = self.result_prompt or self._current_prompt
                brief = PersonaBrief(name=self._persona_name)
                result = await self._agent.refine(
                    current_prompt,
                    feedback,
                    refined_count=self._refined_count,
                    edit_history=self._edit_history,
                    current_brief=brief,
                )
                user_message = build_persona_refine_user_message(feedback, current_prompt)
                self.result_prompt = result.persona_prompt
                self._refined_count = result.refined_count
                self._edit_history = append_persona_edit_history(
                    self._edit_history,
                    user_message,
                    result.raw_response,
                )
                self._preview.setPlainText(self.result_prompt)
                self._status.setText(f"修改版已生成（第 {self._refined_count} 版）。确认后会覆盖当前人格文件。")
                self._save_btn.setEnabled(True)
            except Exception as e:  # noqa: BLE001
                logger.warning("修改人格生成失败", exc_info=True)
                self._status.setText(f"未能完成：{e}")
            finally:
                self._generate_btn.setEnabled(True)

        loop.create_task(_do())

    def _on_accept(self) -> None:
        if self.result_prompt.strip():
            self.accept()
