"""PERSONA_CREATE 子流程 —— 用主模型 + PersonaGenAgent 生成人格档案。

布局：
    顶部进度 | 左侧表单 | 右侧预览
    [生成] / 调整框 + [再生成一次] / [保存]
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
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
                "客观描述角色的稳定特征、背景、表达方式和边界。\n"
                "可以多轮调整，直到结果能直接保存使用。"
            ),
        )
        outer.addWidget(head_card)

        self._admin_rows: list[_AdminRow] = []

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        # 左右分栏
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：表单
        form_panel = self._wrap_scroll(self._build_form_panel())
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

        self._gender_combo = QComboBox()
        self._gender_combo.addItem("不指定", "")
        self._gender_combo.addItem("女", "female")
        self._gender_combo.addItem("男", "male")
        self._gender_combo.addItem("非二元 / 其他", "other")
        layout.addWidget(self._field_label("性别"))
        layout.addWidget(self._hint_label("本项为可选。若不填，则 AI 会根据其他信息自动推断，保存时不会硬编码称谓。"))
        layout.addWidget(self._gender_combo)

        layout.addWidget(self._field_label("熟悉的人（管理员）"))
        layout.addWidget(
            self._hint_label(
                "管理员能够允许她/他通过好友，请谨慎加入。一般情况下，应该填入你的信息。可添加多个人。"
            )
        )
        self._admins_box = QWidget()
        self._admins_layout = QVBoxLayout(self._admins_box)
        self._admins_layout.setContentsMargins(0, 0, 0, 0)
        self._admins_layout.setSpacing(Spacing.XS)
        layout.addWidget(self._admins_box)
        add_admin_btn = QPushButton("+")
        add_admin_btn.setProperty("role", "secondary")
        add_admin_btn.setToolTip("添加一个熟悉的人")
        add_admin_btn.clicked.connect(lambda: self._add_admin_row())
        add_admin_btn.setFixedWidth(42)
        layout.addWidget(add_admin_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._add_admin_row()

        self._personality_edit = self._mk_textarea(COPY["persona_create.personality_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.personality_label"]))
        layout.addWidget(self._hint_label("客观描述稳定特征，不要像聊天一样对角色说话。"))
        layout.addWidget(self._personality_edit)

        self._background_edit = self._mk_textarea(COPY["persona_create.background_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.background_label"]))
        layout.addWidget(self._hint_label("本项为可选。若不填，则 AI 会自动推断或保持简略。"))
        layout.addWidget(self._background_edit)

        self._voice_edit = self._mk_textarea(COPY["persona_create.voice_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.voice_label"]))
        layout.addWidget(self._hint_label("可贴原话、常用句式、消息长短、标点习惯。"))
        layout.addWidget(self._voice_edit)

        self._boundaries_edit = self._mk_textarea(COPY["persona_create.boundaries_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.boundaries_label"]))
        layout.addWidget(self._hint_label("本项为可选。写出角色不愿做、不擅长或会拒绝的事。"))
        layout.addWidget(self._boundaries_edit)

        self._never_say_edit = self._mk_textarea(COPY["persona_create.never_say_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.never_say_label"]))
        layout.addWidget(self._hint_label("本项为可选。若没有明确禁用词，可以留空。"))
        layout.addWidget(self._never_say_edit)

        self._relation_matrix_edit = self._mk_textarea(COPY["persona_create.relation_matrix_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.relation_matrix_label"]))
        layout.addWidget(self._hint_label("本项为可选。若不填，则 AI 会自动推断。"))
        layout.addWidget(self._relation_matrix_edit)

        self._sensitive_edit = self._mk_textarea(COPY["persona_create.sensitive_topics_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.sensitive_topics_label"]))
        layout.addWidget(self._hint_label("本项为可选。写触发沉默、跑题、回避或情绪变化的话题。"))
        layout.addWidget(self._sensitive_edit)

        self._extra_notes_edit = self._mk_textarea(COPY["persona_create.extra_notes_placeholder"])
        layout.addWidget(self._field_label(COPY["persona_create.extra_notes_label"]))
        layout.addWidget(self._hint_label("本项为可选。写给生成人格 AI 的补充要求，帮助调整性格和写法。"))
        layout.addWidget(self._extra_notes_edit)

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

        # 生成按钮
        btn_row = QHBoxLayout()
        self._generate_btn = QPushButton("生成人格")
        self._generate_btn.setProperty("role", "primary")
        self._generate_btn.clicked.connect(self._on_generate)
        btn_row.addWidget(self._generate_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

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
        adj_label = QLabel("不满意的地方写在下面，再调整一次：")
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

    def _hint_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "secondary")
        lbl.setWordWrap(True)
        return lbl

    def _mk_textarea(self, placeholder: str) -> QPlainTextEdit:
        e = QPlainTextEdit()
        e.setPlaceholderText(placeholder)
        e.setFixedHeight(80)
        return e

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        return scroll

    def _add_admin_row(
        self,
        *,
        name: str = "",
        qq: str = "",
        relation: str = "",
    ) -> None:
        row = _AdminRow(self._admins_box)
        row.name_edit.setText(name)
        row.qq_edit.setText(qq)
        row.relation_edit.setText(relation)
        row.remove_btn.clicked.connect(lambda: self._remove_admin_row(row))
        self._admin_rows.append(row)
        self._admins_layout.addWidget(row.widget)
        self._sync_admin_remove_buttons()

    def _remove_admin_row(self, row: _AdminRow) -> None:
        if len(self._admin_rows) <= 1:
            row.name_edit.clear()
            row.qq_edit.clear()
            row.relation_edit.clear()
            return
        self._admin_rows.remove(row)
        self._admins_layout.removeWidget(row.widget)
        row.widget.deleteLater()
        self._sync_admin_remove_buttons()

    def _sync_admin_remove_buttons(self) -> None:
        for row in self._admin_rows:
            row.remove_btn.setEnabled(len(self._admin_rows) > 1)

    def _admin_entries_from_form(self) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for row in self._admin_rows:
            name = row.name_edit.text().strip()
            qq = row.qq_edit.text().strip()
            relation = row.relation_edit.text().strip()
            if not name and not qq and not relation:
                continue
            entry = {"name": name, "qq": qq, "relation": relation}
            entries.append(entry)
        return entries

    def _set_admin_rows(self, entries: list[dict[str, str]]) -> None:
        for row in list(self._admin_rows):
            self._admins_layout.removeWidget(row.widget)
            row.widget.deleteLater()
        self._admin_rows.clear()
        for entry in entries:
            self._add_admin_row(
                name=str(entry.get("name", "")),
                qq=str(entry.get("qq", "")),
                relation=str(entry.get("relation", "")),
            )
        if not self._admin_rows:
            self._add_admin_row()

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
        admins = self._admin_entries_from_form()
        primary_admin = next((x for x in admins if x.get("qq") or x.get("name")), {})
        return PersonaBrief(
            name=self._name_edit.text().strip(),
            gender=self._gender_combo.currentData() or "",
            admins=admins,
            admin_name=primary_admin.get("name", ""),
            admin_qq=primary_admin.get("qq", ""),
            personality=self._personality_edit.toPlainText().strip(),
            background=self._background_edit.toPlainText().strip(),
            voice_samples=self._voice_edit.toPlainText().strip(),
            boundaries=self._boundaries_edit.toPlainText().strip(),
            never_say=self._never_say_edit.toPlainText().strip(),
            relation_matrix=self._relation_matrix_edit.toPlainText().strip(),
            sensitive_topics=self._sensitive_edit.toPlainText().strip(),
            relation=rel,
            extra_notes=self._extra_notes_edit.toPlainText().strip(),
        )

    # ---- 调用 LLM ----

    def _build_persona_agent(self) -> PersonaGenAgent | None:
        """根据 context.main 临时构造一个 PersonaGenAgent。"""
        from providers import AnthropicProvider, OpenAICompatProvider

        m = self.context.main
        if not m.api_key:
            return None

        if m.base_url:
            base_url = m.base_url
        elif m.preset == "custom":
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
            temperature=m.temperature,
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
        bad_qq = next((x.get("qq", "") for x in brief.admins if x.get("qq") and not x.get("qq", "").isdigit()), "")
        if bad_qq:
            self._status_label.setText(f"管理员 QQ 应该是纯数字：{bad_qq}")
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

        self._begin_busy("正在生成人格……（流式生成中，通常 10-30 秒）", generate=True)
        QApplication.processEvents()
        QTimer.singleShot(0, lambda: self._start_generate_task(agent, brief))

    def _begin_busy(self, status: str, *, generate: bool) -> None:
        self._status_label.setText("正在生成人格……（流式生成中，通常 10-30 秒）")
        if status:
            self._status_label.setText(status)
        self._status_label.setProperty("role", "secondary")
        self._restyle(self._status_label)
        self._generate_btn.setEnabled(False)
        if hasattr(self, "_refine_btn"):
            self._refine_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.raise_()

    def _finish_busy(self, *, generated: bool) -> None:
        self._generate_btn.setEnabled(True)
        self._refine_btn.setEnabled(generated)
        self._progress.setVisible(False)

    def _start_generate_task(self, agent: PersonaGenAgent, brief: PersonaBrief) -> None:

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._status_label.setText("事件循环未就绪")
            self._finish_busy(generated=bool(self.context.persona.generated_xml))
            return

        async def _do() -> None:
            try:
                result = await agent.generate(brief)
                self._render_result(result.persona_prompt, brief)
                self._status_label.setText("写完了。看看，需要调整就在下面写一句。")
                self._status_label.setProperty("role", "success")
                self.context.persona.brief = brief
                self.context.persona.generated_xml = result.persona_prompt
                self.context.persona.active = brief.name
                self.context.persona.source = "create"
                self.context.admin_name = brief.admin_name
                self.context.admin_qq = brief.admin_qq
            except Exception as e:  # noqa: BLE001
                logger.warning(f"PersonaGen 失败: {e}")
                self._status_label.setText(f"未能完成：{e}")
                self._status_label.setProperty("role", "error")
            finally:
                self._restyle(self._status_label)
                self._finish_busy(generated=bool(self.context.persona.generated_xml))
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
        self._begin_busy("正在调整……（流式生成中）", generate=False)
        QApplication.processEvents()
        QTimer.singleShot(0, lambda: self._start_refine_task(agent, prev, feedback))

    def _start_refine_task(self, agent: PersonaGenAgent, prev: str, feedback: str) -> None:

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._finish_busy(generated=bool(self.context.persona.generated_xml))
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
                self._finish_busy(generated=bool(self.context.persona.generated_xml))
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
        self._set_admin_rows(
            [{"name": self.context.admin_name, "qq": self.context.admin_qq, "relation": ""}]
            if self.context.admin_name or self.context.admin_qq
            else []
        )
        if p.brief:
            self._name_edit.setText(p.brief.name)
            gender_idx = self._gender_combo.findData(p.brief.gender)
            self._gender_combo.setCurrentIndex(gender_idx if gender_idx >= 0 else 0)
            admins = p.brief.admins or [
                {"name": p.brief.admin_name or self.context.admin_name, "qq": p.brief.admin_qq or self.context.admin_qq, "relation": ""}
            ]
            self._set_admin_rows(admins)
            self._personality_edit.setPlainText(p.brief.personality)
            self._background_edit.setPlainText(p.brief.background)
            self._voice_edit.setPlainText(p.brief.voice_samples)
            self._boundaries_edit.setPlainText(p.brief.boundaries)
            self._never_say_edit.setPlainText(p.brief.never_say)
            self._relation_matrix_edit.setPlainText(p.brief.relation_matrix)
            self._sensitive_edit.setPlainText(p.brief.sensitive_topics)
            self._extra_notes_edit.setPlainText(p.brief.extra_notes)
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


class _AdminRow:
    def __init__(self, parent: QWidget) -> None:
        self.widget = QWidget(parent)
        layout = QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("称呼")
        layout.addWidget(self.name_edit, 1)
        self.qq_edit = QLineEdit()
        self.qq_edit.setPlaceholderText("QQ 号")
        layout.addWidget(self.qq_edit, 1)
        self.relation_edit = QLineEdit()
        self.relation_edit.setPlaceholderText("关系描述（可选）")
        layout.addWidget(self.relation_edit, 2)
        self.remove_btn = QPushButton("−")
        self.remove_btn.setProperty("role", "secondary")
        self.remove_btn.setToolTip("移除这一行")
        self.remove_btn.setFixedWidth(42)
        layout.addWidget(self.remove_btn)
