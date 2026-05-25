"""记忆页 —— important_memory CRUD。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from ..wizard.components import EmptyState
from .copy import DASHBOARD_COPY

logger = logging.getLogger(__name__)


class MemoryPage(QWidget):
    """重要记忆列表。每条可移除。底部支持手动添加一条。"""

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        head = QHBoxLayout()
        title = QLabel(DASHBOARD_COPY["memory.important_section"])
        title.setProperty("role", "title-2")
        head.addWidget(title)
        head.addStretch(1)
        self._refresh_btn = QPushButton(DASHBOARD_COPY["button.refresh"])
        self._refresh_btn.setProperty("role", "text")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        outer.addLayout(head)

        self._list = QListWidget()
        outer.addWidget(self._list, 1)

        # 删除按钮
        action_row = QHBoxLayout()
        self._delete_btn = QPushButton(DASHBOARD_COPY["memory.delete_button"])
        self._delete_btn.setProperty("role", "danger")
        self._delete_btn.clicked.connect(self._on_delete)
        action_row.addWidget(self._delete_btn)
        action_row.addStretch(1)
        outer.addLayout(action_row)

        # 添加一条
        add_row = QHBoxLayout()
        self._new_edit = QLineEdit()
        self._new_edit.setPlaceholderText("手动添加一条重要记忆")
        self._new_edit.returnPressed.connect(self._on_add)
        add_row.addWidget(self._new_edit, 1)
        add_btn = QPushButton("记住")
        add_btn.setProperty("role", "primary")
        add_btn.clicked.connect(self._on_add)
        add_row.addWidget(add_btn)
        outer.addLayout(add_row)

        # 空状态
        self._empty = EmptyState(
            DASHBOARD_COPY["memory.empty_title"],
            DASHBOARD_COPY["memory.empty_subtitle"],
        )
        outer.addWidget(self._empty)
        self._empty.hide()

        self.refresh()

    def refresh(self) -> None:
        rt = self._runtime
        self._list.clear()
        if rt is None or rt.important is None:
            self._show_empty(True)
            return
        items = rt.important.items()
        if not items:
            self._show_empty(True)
            return
        self._show_empty(False)
        for item in items:
            ts = item.get("timestamp", "")
            content = item.get("content", "")
            line = f"{ts}  ·  {content}" if ts else content
            li = QListWidgetItem(line)
            li.setData(Qt.ItemDataRole.UserRole, content)
            self._list.addItem(li)

    def _show_empty(self, on: bool) -> None:
        self._list.setVisible(not on)
        self._delete_btn.setVisible(not on)
        self._empty.setVisible(on)

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        content = item.data(Qt.ItemDataRole.UserRole)
        if not content:
            return
        box = QMessageBox(self)
        box.setWindowTitle(DASHBOARD_COPY["memory.delete_confirm_title"])
        box.setText(DASHBOARD_COPY["memory.delete_confirm_body"])
        yes = box.addButton("移除", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("算了", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return

        rt = self._runtime
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        async def _do() -> None:
            try:
                deleted = await rt.important.delete_by_keyword(content)
                logger.info(f"重要记忆删除 {deleted} 条")
            except Exception as e:
                logger.warning(f"删除重要记忆失败: {e}")
            self.refresh()

        loop.create_task(_do())

    def _on_add(self) -> None:
        text = self._new_edit.text().strip()
        if not text:
            return
        rt = self._runtime
        if rt is None or rt.important is None:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        async def _do() -> None:
            try:
                await rt.important.save(text)
            except Exception as e:
                logger.warning(f"添加重要记忆失败: {e}")
            self._new_edit.clear()
            self.refresh()

        loop.create_task(_do())
