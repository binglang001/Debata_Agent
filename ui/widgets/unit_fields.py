"""Helpers for numeric fields with fixed unit labels."""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractSpinBox, QHBoxLayout, QLabel, QWidget

from ..theme import Spacing


def unit_spinbox(
    spin: QAbstractSpinBox,
    unit: str,
    *,
    spin_min_width: int = 120,
    unit_min_width: int = 40,
    add_stretch: bool = True,
) -> QWidget:
    """把数字输入框和固定单位标签放在同一行。"""

    spin.setMinimumWidth(spin_min_width)

    unit_label = QLabel(unit)
    unit_label.setProperty("role", "secondary")
    unit_label.setMinimumWidth(unit_min_width)

    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(Spacing.XS)
    lay.addWidget(spin)
    lay.addWidget(unit_label)
    if add_stretch:
        lay.addStretch(1)
    return row


__all__ = ["unit_spinbox"]
