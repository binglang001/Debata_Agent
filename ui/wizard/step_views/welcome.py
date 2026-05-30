"""欢迎页 —— 选路径（推荐 / 自定义）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..components import SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ..flow import WIZARD_PATH_CUSTOM, WIZARD_PATH_RECOMMENDED
from ...theme import Spacing


class WelcomeStepView(BaseStepView):
    """欢迎 + 选路径。

    两个大卡片，点选后写入 context.path 并发 request_advance。
    """

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)
        self._selected_path: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.LG)

        # 顶部欢迎
        title = QLabel(COPY["welcome.title"])
        title.setProperty("role", "title-1")
        outer.addWidget(title)

        subtitle = QLabel(COPY["welcome.subtitle"])
        subtitle.setProperty("role", "secondary")
        outer.addWidget(subtitle)

        intro = QLabel(COPY["welcome.intro"])
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # 路径选择 —— 两张卡片
        choose = QLabel(COPY["welcome.choose_path"])
        choose.setProperty("role", "title-3")
        outer.addSpacing(Spacing.MD)
        outer.addWidget(choose)

        row = QHBoxLayout()
        row.setSpacing(Spacing.MD)

        self._rec_card = self._build_path_card(
            COPY["welcome.path_recommended"],
            COPY["welcome.path_recommended_desc"],
            WIZARD_PATH_RECOMMENDED,
        )
        self._cus_card = self._build_path_card(
            COPY["welcome.path_custom"],
            COPY["welcome.path_custom_desc"],
            WIZARD_PATH_CUSTOM,
        )
        row.addWidget(self._rec_card, 1)
        row.addWidget(self._cus_card, 1)
        outer.addLayout(row)

        outer.addStretch(1)
        self._path_cards = {
            WIZARD_PATH_RECOMMENDED: self._rec_card,
            WIZARD_PATH_CUSTOM: self._cus_card,
        }

    def _build_path_card(self, title: str, desc: str, path_value: str) -> QFrame:
        card = QFrame()
        card.setObjectName("SectionCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        layout.setSpacing(Spacing.SM)

        t = QLabel(title)
        t.setProperty("role", "title-3")
        layout.addWidget(t)

        d = QLabel(desc)
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        layout.addWidget(d)
        layout.addStretch(1)

        btn = QPushButton("就走这条")
        btn.setProperty("role", "primary")
        btn.clicked.connect(lambda: self._on_path_clicked(path_value))
        layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignRight)

        return card

    def _on_path_clicked(self, value: str) -> None:
        self._select_path(value)
        self.context.path = value
        self.request_advance.emit()

    def _select_path(self, value: str) -> None:
        self._selected_path = value
        for path_value, card in self._path_cards.items():
            if path_value == value:
                card.setStyleSheet("QFrame#SectionCard { border: 2px solid #6FA39A; }")
            else:
                card.setStyleSheet("")

    def refresh(self) -> None:
        pass  # welcome 不需要回填

    def save(self) -> bool:
        if self._selected_path not in (WIZARD_PATH_RECOMMENDED, WIZARD_PATH_CUSTOM):
            self.invalid_input.emit("请先选择推荐路径或自定义路径。")
            return False
        self.context.path = self._selected_path
        return True
