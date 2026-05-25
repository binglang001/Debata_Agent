"""插件页 —— Phase 3 才会启用，当前显示 EmptyState。"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..theme import Spacing
from ..wizard.components import EmptyState


class PluginsPage(QWidget):
    """空状态：Phase 3 上线后可用。"""

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        self._empty = EmptyState(
            "暂无插件",
            "Phase 3 上线后这里可以装本地 Whisper / VoxCPM2 等模型插件。",
        )
        outer.addWidget(self._empty, 1)

    def refresh(self) -> None:
        pass
