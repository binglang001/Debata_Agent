"""Reusable widgets for the settings page."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...theme import Spacing
from .helpers import _progress_slot


class _SaveStatusBar(QFrame):
    """设置页底部状态条。改动项数由外部 set_changes() 注入。"""

    restart_requested = Signal()
    restore_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        lay.setSpacing(Spacing.MD)

        self._info = QLabel("修改后即时保存。")
        self._info.setProperty("role", "secondary")
        lay.addWidget(self._info)
        lay.addStretch(1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setVisible(False)
        lay.addWidget(_progress_slot(self._progress, width=150))

        self._restart_btn = QPushButton("重启 Debata 服务")
        self._restart_btn.setProperty("role", "primary")
        self._restart_btn.setEnabled(False)
        self._restart_btn.clicked.connect(self.restart_requested.emit)
        lay.addWidget(self._restart_btn)

        self._restore_btn = QPushButton("恢复打开时配置")
        self._restore_btn.setProperty("role", "secondary")
        self._restore_btn.setToolTip("撤销本次打开设置页以来已经即时保存的配置改动")
        self._restore_btn.clicked.connect(self.restore_requested.emit)
        lay.addWidget(self._restore_btn)

        self._changed_count = 0
        self._needs_restart = False

    def set_changes(self, count: int, *, needs_restart: bool) -> None:
        self._progress.setVisible(False)
        self._progress.setRange(0, 100)
        self._changed_count = count
        if needs_restart:
            self._needs_restart = True
        self._render()

    def mark_error(self, msg: str) -> None:
        self._progress.setVisible(False)
        self._info.setText(f"⚠ {msg}")
        self._info.setProperty("role", "error")
        self._restyle()

    def mark_busy(self, msg: str) -> None:
        self._info.setText(msg)
        self._info.setProperty("role", "secondary")
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._restart_btn.setEnabled(False)
        self._restyle()

    def mark_restart_done(self) -> None:
        self._progress.setVisible(False)
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._needs_restart = False
        self._changed_count = 0
        self._info.setText("Debata 服务已重启。")
        self._info.setProperty("role", "success")
        self._restyle()
        self._restart_btn.setEnabled(False)

    def _render(self) -> None:
        self._progress.setVisible(False)
        if self._changed_count == 0:
            if self._needs_restart:
                self._info.setText("所有设置已保存 · 部分需重启生效")
                self._info.setProperty("role", "warning")
            else:
                self._info.setText("所有设置与保存时一致。")
                self._info.setProperty("role", "secondary")
            self._restart_btn.setEnabled(self._needs_restart)
        elif self._needs_restart:
            self._info.setText(f"已修改 {self._changed_count} 项 · 部分需重启生效")
            self._info.setProperty("role", "warning")
            self._restart_btn.setEnabled(True)
        else:
            self._info.setText(f"已修改 {self._changed_count} 项（即时生效）")
            self._info.setProperty("role", "success")
            self._restart_btn.setEnabled(False)
        self._restyle()

    def _restyle(self) -> None:
        self._info.style().unpolish(self._info)
        self._info.style().polish(self._info)


class CollapsibleSection(QFrame):
    """设置页内的轻量折叠区。用于隐藏专业项但不销毁控件。"""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        *,
        expanded: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._expanded = bool(expanded)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.MD, Spacing.MD, Spacing.MD, Spacing.MD)
        outer.setSpacing(Spacing.SM)

        head = QHBoxLayout()
        head.setSpacing(Spacing.SM)
        self._toggle_btn = QPushButton()
        self._toggle_btn.setProperty("role", "collapse-toggle")
        self._toggle_btn.setFixedWidth(30)
        self._toggle_btn.setFixedHeight(30)
        self._toggle_btn.clicked.connect(self._toggle)
        head.addWidget(self._toggle_btn, 0, Qt.AlignmentFlag.AlignTop)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(Spacing.XS)
        title_lbl = QLabel(title)
        title_lbl.setProperty("role", "title-3")
        title_box.addWidget(title_lbl)
        if subtitle:
            subtitle_lbl = QLabel(subtitle)
            subtitle_lbl.setProperty("role", "secondary")
            subtitle_lbl.setWordWrap(True)
            title_box.addWidget(subtitle_lbl)
        head.addLayout(title_box, 1)
        outer.addLayout(head)

        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, Spacing.XS, 0, 0)
        self._body_lay.setSpacing(Spacing.SM)
        outer.addWidget(self._body)
        self._render_state()

    def add_content(self, widget: QWidget) -> None:
        self._body_lay.addWidget(widget)

    def add_layout(self, layout) -> None:
        self._body_lay.addLayout(layout)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._render_state()

    def _render_state(self) -> None:
        self._toggle_btn.setText("v" if self._expanded else ">")
        self._toggle_btn.setToolTip("收起" if self._expanded else "展开")
        self._body.setVisible(self._expanded)


__all__ = ["CollapsibleSection", "_SaveStatusBar"]
