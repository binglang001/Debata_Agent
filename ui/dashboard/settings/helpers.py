"""Small helpers shared by settings page modules."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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


def _path_picker_row(
    edit: QLineEdit,
    *,
    parent: QWidget,
    title: str,
    directory: bool,
    file_filter: str = "所有文件 (*)",
) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(Spacing.SM)
    lay.addWidget(edit, 1)
    btn = QPushButton("浏览")
    btn.setProperty("role", "secondary")

    def _pick() -> None:
        start = edit.text().strip()
        if directory:
            path = QFileDialog.getExistingDirectory(parent, title, start)
        else:
            path, _ = QFileDialog.getOpenFileName(parent, title, start, file_filter)
        if path:
            edit.setText(path)

    btn.clicked.connect(_pick)
    lay.addWidget(btn)
    return row


def _set_form_field_visible(form: QFormLayout, field: QWidget, visible: bool) -> None:
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)
    field.setVisible(visible)


__all__ = ["_path_picker_row", "_progress_slot", "_set_form_field_visible"]
