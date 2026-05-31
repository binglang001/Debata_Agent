"""AutoSizeStack —— sizeHint 只反映当前页的 QStackedWidget。

默认 QStackedWidget 的 sizeHint 取所有子页面的最大值，
配合 QScrollArea 会导致小页面也能滚到空白区域。
本类让 sizeHint / minimumSizeHint 只跟当前页走，
切页时立即 updateGeometry 让外层 ScrollArea 重新决定是否显示滚动条。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import QSizePolicy, QStackedWidget


class AutoSizeStack(QStackedWidget):
    """sizeHint 只看当前页的 QStackedWidget。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.currentChanged.connect(self._on_current_changed)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        cur = self.currentWidget()
        if cur is None:
            return super().sizeHint()
        return cur.sizeHint()

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        cur = self.currentWidget()
        if cur is None:
            return super().minimumSizeHint()
        return cur.minimumSizeHint()

    def sync_current_size(self) -> None:
        """按当前页刷新几何信息，避免 QStackedWidget 沿用历史最大 sizeHint。"""
        cur = self.currentWidget()
        if cur is None:
            self.setMinimumHeight(0)
            return
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()

    def _on_current_changed(self, _idx: int) -> None:
        # 让外层 ScrollArea 立即重新评估是否需要纵向滚动条
        self.sync_current_size()
        QTimer.singleShot(0, self.sync_current_size)


__all__ = ["AutoSizeStack"]
