"""AutoSizeStack —— sizeHint 只反映当前页的 QStackedWidget。

默认 QStackedWidget 的 sizeHint 取所有子页面的最大值，
配合 QScrollArea 会导致小页面也能滚到空白区域。
本类让 sizeHint / minimumSizeHint 只跟当前页走，
切页时立即 updateGeometry 让外层 ScrollArea 重新决定是否显示滚动条。
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QStackedWidget


class AutoSizeStack(QStackedWidget):
    """sizeHint 只看当前页的 QStackedWidget。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
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

    def _on_current_changed(self, _idx: int) -> None:
        # 让外层 ScrollArea 立即重新评估是否需要纵向滚动条
        self.updateGeometry()
        self.adjustSize()


__all__ = ["AutoSizeStack"]
