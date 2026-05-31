"""PERSONA_CREATE 子流程 —— 用主模型 + PersonaGenAgent 生成人格档案。

布局：
    左侧表单 (9 个字段) | 右侧预览
    底部 [生成] / 调整框 + [再生成一次] / [保存]
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from agents.persona_gen_agent import PersonaBrief, PersonaGenAgent
from app_config.schema import AgentConfig

from ..theme import Spacing
from .components import SectionCard
from .context import BaseStepView, WizardContext
from .copy import COPY

logger = logging.getLogger(__name__)


class PersonaCreatorStepView(BaseStepView):
    """左表单 + 右预览的人格生成界面。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        head_card = SectionCard(
            title="塑造你的角色",
            subtitle=(
                "回答几个问题，Debata 会和你一起把这个角色具象化。\n"
                "可以多轮调整，直到你满意为止。"
            ),
        )
        outer.addWidget(head_card)

        # 左右分栏
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：表单
        form_panel = self._build_form_panel()
        splitter.addWidget(form_panel)

        # 右：预览
        preview_panel = self._build_preview_panel()
        splitter.addWidget(preview_panel)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([500, 500])

        outer.addWidget(splitter, 1)

    def _build_form_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, Spacing.MD, 0)
        layout.setSpacing(Spacing.SM)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(COPY["persona_create.name_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.name_label"]))
        layout.addWidget(self._name_edit)

        admin_row = QHBoxLayout()
        self._admin_name_edit = QLineEdit()
        self._admin_name_edit.setPlaceholderText("管理员名称（可选）")
        admin_row.addWidget(self._admin_name_edit, 1)
        self._admin_qq_edit = QLineEdit()
        self._admin_qq_edit.setPlaceholderText("管理员 QQ（可选）")
        admin_row.addWidget(self._admin_qq_edit, 1)
        layout.addWidget(self._field_label("管理员信息"))
        layout.addLayout(admin_row)

        self._personality_edit = self._mk_textarea(COPY["persona_create.personality_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.personality_label"]))
        layout.addWidget(self._personality_edit)

        self._background_edit = self._mk_textarea(COPY["persona_create.background_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.background_label"]))
        layout.addWidget(self._background_edit)

        self._voice_edit = self._mk_textarea(COPY["persona_create.voice_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.voice_label"]))
        layout.addWidget(self._voice_edit)

        self._boundaries_edit = self._mk_textarea(COPY["persona_create.boundaries_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.boundaries_label"]))
        layout.addWidget(self._boundaries_edit)

        self._never_say_edit = self._mk_textarea(COPY["persona_create.never_say_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.never_say_label"]))
        layout.addWidget(self._never_say_edit)

        self._relation_matrix_edit = self._mk_textarea(COPY["persona_create.relation_matrix_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.relation_matrix_label"]))
        layout.addWidget(self._relation_matrix_edit)

        self._sensitive_edit = self._mk_textarea(COPY["persona_create.sensitive_topics_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.sensitive_topics_label"]))
        layout.addWidget(self._sensitive_edit)

        # 关系四选一
        layout.addWidget(self._field_label(COPY["persona_create.relation_label"]))
        rel_layout = QVBoxLayout()
        rel_layout.setSpacing(Spacing.XS)
        self._relation_group = QButtonGroup(self)
        self._relation_group.setExclusive(True)
        self._relation_buttons: dict[str, QRadioButton] = {}
        for value, label_key in [
            ("creator", "persona_create.relation_creator"),
            ("friend", "persona_create.relation_friend"),
            ("stranger", "persona_create.relation_stranger"),
            ("special", "persona_create.relation_special"),
        ]:
            rb = QRadioButton(COPY[label_key])
            rb.setProperty("rel_value", value)
            self._relation_group.addButton(rb)
            self._relation_buttons[value] = rb
            rel_layout.addWidget(rb)
        layout.addLayout(rel_layout)

        # 「其它」选中时的说明输入框
        self._special_edit = QLineEdit()
        self._special_edit.setPlaceholderText("说明你和这个角色的关系（如：我是 ta 的导师、网友、宠物主人……）")
        self._special_edit.setVisible(False)
        layout.addWidget(self._special_edit)

        def _on_rel_toggled(*_) -> None:
            self._special_edit.setVisible(self._relation_buttons["special"].isChecked())

        for rb in self._relation_group.buttons():
            rb.toggled.connect(_on_rel_toggled)

        # 默认 creator
        self._relation_buttons["creator"].setChecked(True)

        # 生成按钮 + 进度条
        btn_row = QHBoxLayout()
        self._generate_btn = QPushButton("生成人格")
        self._generate_btn.setProperty("role", "primary")
        self._generate_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self._generate_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        layout.addStretch(1)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(Spacing.MD, 0, 0, 0)
        layout.setSpacing(Spacing.SM)

        title = QLabel(COPY["persona_create.preview_title"])
        title.setProperty("role", "title-3")
        layout.addWidget(title)

        self._preview = QTextBrowser()
        self._preview.setPlaceholderText(COPY["persona_create.preview_empty"])
        self._preview.setMinimumWidth(360)
        layout.addWidget(self._preview, 1)

        self._status_label = QLabel("")
        self._status_label.setProperty("role", "secondary")
        layout.addWidget(self._status_label)

        # 调整框 + 再生成
        adj_label = QLabel("不满意的地方写在下面，让她再调一次：")
        adj_label.setProperty("role", "secondary")
        layout.addWidget(adj_label)

        self._adjust_edit = QPlainTextEdit()
        self._adjust_edit.setPlaceholderText(COPY["persona_create.adjust_placeholder"])
        self._adjust_edit.setFixedHeight(80)
        layout.addWidget(self._adjust_edit)

        action_row = QHBoxLayout()
        self._refine_btn = QPushButton(COPY["button.regenerate"])
        self._refine_btn.setProperty("role", "secondary")
        self._refine_btn.setEnabled(False)
        self._refine_btn.clicked.connect(self._on_refine)
        action_row.addWidget(self._refine_btn)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        return panel

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "title-3")
        return lbl

    def _mk_textarea(self, placeholder: str) -> QPlainTextEdit:
        e = QPlainTextEdit()
        e.setPlaceholderText(placeholder)
        e.setFixedHeight(80)
        return e

    def _current_brief(self) -> PersonaBrief:
        rel = "creator"
        for rb in self._relation_group.buttons():
            if isinstance(rb, QRadioButton) and rb.isChecked():
                rel = rb.property("rel_value") or "creator"
                break
        # 「其它」时把用户写的说明拼到 relation 字段（PersonaBrief 已是 free-text）
        if rel == "special":
            extra = self._special_edit.text().strip()
            if extra:
                rel = f"special:{extra}"
        return PersonaBrief(
            name=self._name_edit.text().strip(),
            admin_name=self._admin_name_edit.text().strip(),
            admin_qq=self._admin_qq_edit.text().strip(),
            personality=self._personality_edit.toPlainText().strip(),
            background=self._background_edit.toPlainText().strip(),
            voice_samples=self._voice_edit.toPlainText().strip(),
            boundaries=self._boundaries_edit.toPlainText().strip(),
            never_say=self._never_say_edit.toPlainText().strip(),
            relation_matrix=self._relation_matrix_edit.toPlainText().strip(),
            sensitive_topics=self._sensitive_edit.toPlainText().strip(),
            relation=rel,
        )

    # ---- 调用 LLM ----

    def _build_persona_agent(self) -> PersonaGenAgent | None:
        """根据 context.main 临时构造一个 PersonaGenAgent。"""
        from providers import AnthropicProvider, OpenAICompatProvider

        m = self.context.main
        if not m.api_key:
            return None

        if m.preset == "custom":
            base_url = m.base_url
        else:
            from .step_views.main_model_custom import _PRESET_DEFAULTS
            base_url = _PRESET_DEFAULTS.get(m.preset, {}).get("url", "")

        if not base_url:
            return None

        if m.protocol == "anthropic":
            provider = AnthropicProvider("wizard_persona", base_url=base_url, api_key=m.api_key, timeout=180.0)
        else:
            provider = OpenAICompatProvider("wizard_persona", base_url=base_url, api_key=m.api_key, timeout=180.0)

        cfg = AgentConfig(
            provider=m.preset or "deepseek",
            model=m.model,
            temperature=0.7,
            top_p=m.top_p,
            max_tokens=max(8192, m.max_tokens),
            first_token_timeout_seconds=60.0,
        )
        return PersonaGenAgent(provider, cfg)

    def _on_generate(self) -> None:
        brief = self._current_brief()
        if not brief.name:
            self._status_label.setText("先填一下角色名")
            self._status_label.setProperty("role", "error")
            self._restyle(self._status_label)
            return
        if brief.admin_qq and not brief.admin_qq.isdigit():
            self._status_label.setText("管理员 QQ 应该是纯数字")
            self._status_label.setProperty("role", "error")
            self._restyle(self._status_label)
            return
        # 本地校验：避免向导完成后才发现 persona 目录创不出来
        from agents.persona_loader import validate_persona_name
        try:
            validate_persona_name(brief.name)
        except ValueError as e:
            self._status_label.setText(
                f"角色名不能用：{e}。\n建议：用中英文 / 数字 / 下划线 / 连字符，"
                "避开 / \\ : * ? \" < > | 等字符"
            )
            self._status_label.setProperty("role", "error")
            self._restyle(self._status_label)
            return
        agent = self._build_persona_agent()
        if agent is None:
            self._status_label.setText("先在「选个主模型」一步配好密钥")
            self._status_label.setProperty("role", "error")
            self._restyle(self._status_label)
            return

        self._status_label.setText("正在请她想想……（流式生成中，通常 10-30 秒）")
        self._status_label.setProperty("role", "secondary")
        self._restyle(self._status_label)
        self._generate_btn.setEnabled(False)
        self._progress.setVisible(True)

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._status_label.setText("事件循环未就绪")
            return

        async def _do() -> None:
            try:
                result = await agent.generate(brief)
                self._render_result(result.persona_prompt, brief)
                self._status_label.setText("写完了。看看，需要调就在下面写一句。")
                self._status_label.setProperty("role", "success")
                self.context.persona.brief = brief
                self.context.persona.generated_xml = result.persona_prompt
                self.context.persona.active = brief.name
                self.context.persona.source = "create"
                self.context.admin_name = brief.admin_name
                self.context.admin_qq = brief.admin_qq
                self._refine_btn.setEnabled(True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"PersonaGen 失败: {e}")
                self._status_label.setText(f"未能完成：{e}")
                self._status_label.setProperty("role", "error")
            finally:
                self._restyle(self._status_label)
                self._generate_btn.setEnabled(True)
                self._progress.setVisible(False)
                # 显式关闭 provider
                try:
                    await agent.provider.aclose()
                except Exception:
                    pass

        loop.create_task(_do())

    def _on_refine(self) -> None:
        feedback = self._adjust_edit.toPlainText().strip()
        if not feedback:
            self._status_label.setText("写一句想调整的，再来一次")
            self._status_label.setProperty("role", "secondary")
            self._restyle(self._status_label)
            return
        if not self.context.persona.generated_xml:
            self._status_label.setText("先生成一次")
            return
        agent = self._build_persona_agent()
        if agent is None:
            return

        prev = self.context.persona.generated_xml
        self._refine_btn.setEnabled(False)
        self._status_label.setText("正在调整……（流式生成中）")
        self._status_label.setProperty("role", "secondary")
        self._restyle(self._status_label)
        self._progress.setVisible(True)

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        async def _do() -> None:
            try:
                result = await agent.refine(prev, feedback)
                brief = self._current_brief()
                self._render_result(result.persona_prompt, brief)
                self.context.persona.brief = brief
                self.context.persona.generated_xml = result.persona_prompt
                self.context.admin_name = brief.admin_name
                self.context.admin_qq = brief.admin_qq
                self._status_label.setText(f"调过了（第 {result.refined_count} 版）")
                self._status_label.setProperty("role", "success")
                self._adjust_edit.clear()
            except Exception as e:  # noqa: BLE001
                self._status_label.setText(f"未能完成：{e}")
                self._status_label.setProperty("role", "error")
            finally:
                self._restyle(self._status_label)
                self._refine_btn.setEnabled(True)
                self._progress.setVisible(False)
                try:
                    await agent.provider.aclose()
                except Exception:
                    pass

        loop.create_task(_do())

    def _render_result(self, xml: str, brief: PersonaBrief) -> None:
        # 用 plain text 显示 XML（保留结构便于人阅读）
        self._preview.setPlainText(xml)

    def _restyle(self, w) -> None:
        w.style().unpolish(w)
        w.style().polish(w)

    # ---- BaseStepView 接口 ----

    def refresh(self) -> None:
        p = self.context.persona
        self._admin_name_edit.setText(self.context.admin_name)
        self._admin_qq_edit.setText(self.context.admin_qq)
        if p.brief:
            self._name_edit.setText(p.brief.name)
            self._admin_name_edit.setText(p.brief.admin_name or self.context.admin_name)
            self._admin_qq_edit.setText(p.brief.admin_qq or self.context.admin_qq)
            self._personality_edit.setPlainText(p.brief.personality)
            self._background_edit.setPlainText(p.brief.background)
            self._voice_edit.setPlainText(p.brief.voice_samples)
            self._boundaries_edit.setPlainText(p.brief.boundaries)
            self._never_say_edit.setPlainText(p.brief.never_say)
            self._relation_matrix_edit.setPlainText(p.brief.relation_matrix)
            self._sensitive_edit.setPlainText(p.brief.sensitive_topics)
            for rb in self._relation_group.buttons():
                if isinstance(rb, QRadioButton) and rb.property("rel_value") == p.brief.relation:
                    rb.setChecked(True)
                    break
        if p.generated_xml:
            self._preview.setPlainText(p.generated_xml)
            self._refine_btn.setEnabled(True)

    def save(self) -> bool:
        if not self.context.persona.generated_xml:
            self.invalid_input.emit("先生成一次再继续")
            return False
        if not self.context.persona.active:
            self.invalid_input.emit("还没有角色名，无法保存")
            return False
        return True
