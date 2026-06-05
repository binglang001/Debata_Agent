"""Small helpers shared by settings page modules."""

from __future__ import annotations

from PySide6.QtWidgets import QProgressBar, QVBoxLayout, QWidget

from ...theme import Spacing


def _progress_slot(progress: QProgressBar, *, width: int | None = None, height: int = Spacing.SM) -> QWidget:
    """固定进度条占位，避免忙碌动画出现时挤动表单控件。"""
    progress.setFixedHeight(4)
    slot = QWidget()
    slot.setFixedHeight(height)
    if width is not None:
        slot.setFixedWidth(width)
    lay = QVBoxLayout(slot)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addStretch(1)
    lay.addWidget(progress)
    lay.addStretch(1)
    return slot


__all__ = ["_progress_slot"]
