"""记忆页 —— important_memory CRUD。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from ..widgets import show_message
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
        self._title = QLabel(DASHBOARD_COPY["memory.important_section"])
        self._title.setProperty("role", "title-2")
        head.addWidget(self._title)
        head.addStretch(1)
        self._refresh_btn = QPushButton(DASHBOARD_COPY["button.refresh"])
        self._refresh_btn.setProperty("role", "text")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        outer.addLayout(head)

        self._rag_status = QLabel("")
        self._rag_status.setProperty("role", "secondary")
        self._rag_status.setWordWrap(True)
        outer.addWidget(self._rag_status)

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        outer.addWidget(self._list, 1)

        # 删除按钮
        self._action_row_widget = QWidget()
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        self._delete_btn = QPushButton(DASHBOARD_COPY["memory.delete_button"])
        self._delete_btn.setProperty("role", "danger")
        self._delete_btn.clicked.connect(self._on_delete)
        action_row.addWidget(self._delete_btn)
        action_row.addStretch(1)
        self._action_row_widget.setLayout(action_row)
        outer.addWidget(self._action_row_widget)

        # scope / pinned 元数据
        self._metadata_row_widget = QWidget()
        metadata_row = QHBoxLayout()
        metadata_row.setContentsMargins(0, 0, 0, 0)
        metadata_row.addWidget(QLabel("Scope"))
        self._scope_edit = QLineEdit()
        self._scope_edit.setPlaceholderText("global / user:QQ / group:群号")
        metadata_row.addWidget(self._scope_edit, 1)
        self._pinned_check = QCheckBox("置顶")
        metadata_row.addWidget(self._pinned_check)
        self._metadata_btn = QPushButton("保存范围")
        self._metadata_btn.setProperty("role", "secondary")
        self._metadata_btn.clicked.connect(self._on_update_metadata)
        metadata_row.addWidget(self._metadata_btn)
        self._metadata_row_widget.setLayout(metadata_row)
        outer.addWidget(self._metadata_row_widget)

        # 添加一条
        self._add_row_widget = QWidget()
        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        self._new_edit = QLineEdit()
        self._new_edit.setPlaceholderText("手动添加一条重要记忆")
        self._new_edit.returnPressed.connect(self._on_add)
        add_row.addWidget(self._new_edit, 1)
        self._add_btn = QPushButton("记住")
        self._add_btn.setProperty("role", "primary")
        self._add_btn.clicked.connect(self._on_add)
        add_row.addWidget(self._add_btn)
        self._add_row_widget.setLayout(add_row)
        outer.addWidget(self._add_row_widget)

        # 空状态
        self._empty = EmptyState(
            DASHBOARD_COPY["memory.empty_title"],
            DASHBOARD_COPY["memory.empty_subtitle"],
        )
        outer.addWidget(self._empty)
        self._empty.hide()
        self._rag_empty = EmptyState(
            "暂无 RAG 索引",
            "有长期记忆并完成向量化后会显示在这里",
        )
        outer.addWidget(self._rag_empty)
        self._rag_empty.hide()

        self.refresh()

    def refresh(self) -> None:
        rt = self._runtime
        self._list.clear()
        if rt is None or rt.important is None:
            self._show_empty(True)
            return
        if self._is_rag_mode():
            self._refresh_rag_mode(rt)
            return

        self._title.setText(DASHBOARD_COPY["memory.important_section"])
        self._rag_status.hide()
        self._action_row_widget.show()
        self._add_row_widget.show()
        items = rt.important.items()
        if not items:
            self._show_empty(True)
            return
        self._show_empty(False)
        for item in items:
            self._add_memory_item(item)
        self._on_selection_changed()

    def _refresh_rag_mode(self, rt: Any) -> None:
        self._title.setText("RAG 记忆索引")
        self._action_row_widget.hide()
        self._add_row_widget.hide()
        self._metadata_row_widget.show()
        self._rag_status.show()

        rag_store = getattr(rt, "rag_store", None)
        embedding = getattr(rt, "embedding_service", None)
        entries = rag_store.all_entries() if rag_store is not None else []
        items = rt.important.items() if rt.important is not None else []
        fallback_count = len(items)
        if rag_store is None or embedding is None:
            self._rag_status.setText(
                f"RAG 当前未就绪，运行时会暂时退回全文记忆。已有文件记忆 {fallback_count} 条。"
            )
        else:
            self._rag_status.setText(
                f"按语义向量检索长期记忆。索引 {len(entries)} 条，原始记忆 {fallback_count} 条。"
            )

        if not items:
            self._show_empty(True)
            return
        self._show_empty(False)
        for item in items:
            self._add_memory_item(item, rag_mode=True)
        self._on_selection_changed()

    def _is_rag_mode(self) -> bool:
        try:
            return self._runtime.config.features.long_term_memory.mode == "rag"
        except Exception:
            return False

    def _show_empty(self, on: bool) -> None:
        self._list.setVisible(not on)
        if self._is_rag_mode():
            self._action_row_widget.hide()
            self._add_row_widget.hide()
            self._metadata_row_widget.setVisible(not on)
        else:
            self._delete_btn.setVisible(not on)
            self._metadata_row_widget.setVisible(not on)
        self._empty.setVisible(on and not self._is_rag_mode())
        self._rag_empty.setVisible(on and self._is_rag_mode())

    def _add_memory_item(self, item: dict, *, rag_mode: bool = False) -> None:
        ts = item.get("timestamp", "")
        content = item.get("content", "")
        scope = item.get("scope", "global")
        pinned = bool(item.get("pinned"))
        flags = f"[{scope}{' / 置顶' if pinned else ''}]"
        line = f"{ts}  ·  {flags}  {content}" if ts else f"{flags}  {content}"
        li = QListWidgetItem(line)
        if rag_mode:
            li.setToolTip("RAG 模式下编辑原始重要记忆的 scope / pinned，保存后会重建索引。")
        li.setData(
            Qt.ItemDataRole.UserRole,
            {
                "id": str(item.get("id") or item.get("timestamp") or ""),
                "content": content,
                "scope": scope,
                "pinned": pinned,
            },
        )
        self._list.addItem(li)

    def _on_selection_changed(self) -> None:
        item = self._list.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else {}
        data = data or {}
        self._scope_edit.setText(str(data.get("scope") or "global"))
        self._pinned_check.setChecked(bool(data.get("pinned")))
        has_item = bool(data.get("id"))
        self._scope_edit.setEnabled(has_item)
        self._pinned_check.setEnabled(has_item)
        self._metadata_btn.setEnabled(has_item)

    def _on_delete(self) -> None:
        item = self._list.currentItem()
        if item is None:
            show_message(self, "先选一条", "请先在列表里选中要移除的记忆。")
            return
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        item_id = data.get("id", "")
        content = data.get("content", "")
        if not item_id:
            show_message(self, "无法移除", "这条记忆缺少唯一时间戳，请刷新后再试。")
            return
        if not show_message(
            self,
            DASHBOARD_COPY["memory.delete_confirm_title"],
            DASHBOARD_COPY["memory.delete_confirm_body"],
            confirm_text="移除",
            cancel_text="算了",
            is_danger=True,
        ):
            return

        rt = self._runtime
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        async def _do() -> None:
            self._set_busy(True)
            try:
                deleted = await rt.important.delete_by_id(item_id)
                if deleted:
                    logger.info(f"重要记忆删除 1 条: {content[:40]}")
                else:
                    show_message(self, "没有移除", "未找到这条记忆，可能已经被刷新。")
            except Exception as e:
                logger.warning(f"删除重要记忆失败: {e}")
                show_message(self, "移除失败", str(e), is_danger=True)
            finally:
                self._set_busy(False)
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
            self._set_busy(True)
            try:
                await rt.important.save(text)
                self._new_edit.clear()
            except Exception as e:
                logger.warning(f"添加重要记忆失败: {e}")
                show_message(self, "添加失败", str(e), is_danger=True)
            finally:
                self._set_busy(False)
                self.refresh()

        loop.create_task(_do())

    def _on_update_metadata(self) -> None:
        item = self._list.currentItem()
        if item is None:
            show_message(self, "先选一条", "请先在列表里选中要修改的记忆。")
            return
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        item_id = data.get("id", "")
        if not item_id:
            show_message(self, "无法保存", "这条记忆缺少唯一时间戳，请刷新后再试。")
            return
        rt = self._runtime
        if rt is None or rt.important is None:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        async def _do() -> None:
            self._set_busy(True)
            try:
                ok = await rt.important.update_metadata(
                    item_id,
                    scope=self._scope_edit.text().strip(),
                    pinned=self._pinned_check.isChecked(),
                )
                if not ok:
                    show_message(self, "没有保存", "未找到这条记忆，可能已经被刷新。")
            except Exception as e:
                logger.warning(f"更新重要记忆元数据失败: {e}")
                show_message(self, "保存失败", str(e), is_danger=True)
            finally:
                self._set_busy(False)
                self.refresh()

        loop.create_task(_do())

    def _set_busy(self, busy: bool) -> None:
        self._delete_btn.setEnabled(not busy)
        self._add_btn.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy)
        self._new_edit.setEnabled(not busy)
        current = self._list.currentItem() is not None
        self._scope_edit.setEnabled(not busy and current)
        self._pinned_check.setEnabled(not busy and current)
        self._metadata_btn.setEnabled(not busy and current)
