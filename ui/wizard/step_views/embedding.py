"""Embedding 配置（仅 long_term_memory_mode=rag 时显示）。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
)

from ..components import ApiKeyInput, SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ...theme import Spacing


class EmbeddingStepView(BaseStepView):
    """RAG 模式的 embedding 服务配置。

    分支：选 API 还是本地。当前都只装配置（P2 实际不工作，schema 里有说明）。
    """

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="向量记忆",
            subtitle=COPY["embedding.type_title"]
            + "\n（注：embedding 服务在 Phase 2 是占位实现，配置会写入但当前不工作）",
        )
        outer.addWidget(card)

        self._type_group = QButtonGroup(self)
        self._type_group.setExclusive(True)

        self._rb_api = QRadioButton(COPY["embedding.type_api"])
        self._rb_local = QRadioButton(COPY["embedding.type_local"])
        self._type_group.addButton(self._rb_api)
        self._type_group.addButton(self._rb_local)
        self._rb_api.toggled.connect(self._refresh_visibility)

        card.add_content(self._rb_api)
        card.add_content(self._mk_secondary(COPY["embedding.type_api_desc"]))

        # API 子表单
        api_form = QFormLayout()
        self._api_provider = QComboBox()
        self._api_provider.addItem("火山引擎", "volcengine")
        self._api_provider.addItem("智谱 GLM", "glm")
        self._api_provider.addItem("OpenAI", "openai")
        self._api_provider.addItem("自定义", "custom")
        api_form.addRow(QLabel("提供商"), self._api_provider)

        self._api_model = QLineEdit()
        self._api_model.setPlaceholderText("doubao-embedding-text-240715")
        api_form.addRow(QLabel("模型 ID"), self._api_model)

        self._api_key = ApiKeyInput(placeholder="API 密钥")
        api_form.addRow(QLabel("密钥"), self._api_key)

        card.add_layout(api_form)

        card.add_content(self._rb_local)
        card.add_content(self._mk_secondary(COPY["embedding.type_local_desc"]))

        # 本地子表单
        self._local_group = QButtonGroup(self)
        self._local_group.setExclusive(True)
        self._rb_perf = QRadioButton(COPY["embedding.local_perf_title"])
        self._rb_qual = QRadioButton(COPY["embedding.local_qual_title"])
        self._local_group.addButton(self._rb_perf)
        self._local_group.addButton(self._rb_qual)
        card.add_content(self._rb_perf)
        card.add_content(self._mk_secondary(COPY["embedding.local_perf_desc"]))
        card.add_content(self._rb_qual)
        card.add_content(self._mk_secondary(COPY["embedding.local_qual_desc"]))

        outer.addStretch(1)
        self._rb_api.setChecked(True)
        self._rb_perf.setChecked(True)
        self._refresh_visibility(True)

    def _mk_secondary(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "secondary")
        lbl.setWordWrap(True)
        lbl.setContentsMargins(24, 0, 0, 0)
        return lbl

    def _refresh_visibility(self, api_on: bool) -> None:
        self._api_provider.setEnabled(api_on)
        self._api_model.setEnabled(api_on)
        self._api_key.setEnabled(api_on)
        self._rb_perf.setEnabled(not api_on)
        self._rb_qual.setEnabled(not api_on)

    def refresh(self) -> None:
        if self.context.embedding_type == "local":
            self._rb_local.setChecked(True)
        else:
            self._rb_api.setChecked(True)

        idx = self._api_provider.findData(self.context.embedding_provider)
        if idx >= 0:
            self._api_provider.setCurrentIndex(idx)
        if self.context.embedding_model:
            self._api_model.setText(self.context.embedding_model)
        if self.context.embedding_api_key:
            self._api_key.set_text(self.context.embedding_api_key)
        if self.context.embedding_local_quality == "quality":
            self._rb_qual.setChecked(True)
        else:
            self._rb_perf.setChecked(True)

    def save(self) -> bool:
        if self._rb_api.isChecked():
            if not self._api_model.text().strip():
                self.invalid_input.emit("请填模型 ID")
                return False
            if not self._api_key.text():
                self.invalid_input.emit("请填 API 密钥")
                return False
            self.context.embedding_type = "api"
            self.context.embedding_provider = self._api_provider.currentData()
            self.context.embedding_model = self._api_model.text().strip()
            self.context.embedding_api_key = self._api_key.text()
        else:
            self.context.embedding_type = "local"
            self.context.embedding_local_quality = (
                "quality" if self._rb_qual.isChecked() else "performance"
            )
        return True
