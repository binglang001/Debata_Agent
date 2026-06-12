"""Simple feature toggle card."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ....theme import Spacing
from .._shared import _add_guide_button


class _SimpleFeatureToggle(QFrame):
    """Simple toggle: checkbox plus description."""

    def __init__(
        self,
        title: str,
        desc: str,
        guide_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        self._check = QCheckBox(title)
        head.addWidget(self._check)
        head.addStretch(1)
        if guide_name:
            _add_guide_button(head, guide_name, self)
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


__all__ = ["_SimpleFeatureToggle"]
