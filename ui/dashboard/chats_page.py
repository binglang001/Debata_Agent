"""对话页 —— 显示当前 active persona 的 history。

历史按时间顺序排列。每条根据 role 用不同颜色：
    user      默认正文
    assistant 青瓷青
    tool      汝窑蓝（小字 + mono）
    system    次要文字
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from ..wizard.components import EmptyState
from .copy import DASHBOARD_COPY

logger = logging.getLogger(__name__)


class ChatsPage(QWidget):
    """列表 + 详情。"""

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._records: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        head = QHBoxLayout()
        title = QLabel(DASHBOARD_COPY["chats.list_title"])
        title.setProperty("role", "title-2")
        head.addWidget(title)
        head.addStretch(1)

        self._refresh_btn = QPushButton(DASHBOARD_COPY["button.refresh"])
        self._refresh_btn.setProperty("role", "text")
        self._refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self._refresh_btn)
        outer.addLayout(head)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._list = QListWidget()
        self._list.itemClicked.connect(self._on_item_clicked)
        splitter.addWidget(self._list)

        self._detail = QTextBrowser()
        self._detail.setPlaceholderText("点选左侧某一条查看")
        splitter.addWidget(self._detail)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([320, 640])

        # 空状态覆盖层
        self._empty = EmptyState(
            DASHBOARD_COPY["chats.empty_title"],
            DASHBOARD_COPY["chats.empty_subtitle"],
        )

        self._splitter = splitter
        outer.addWidget(splitter, 1)
        outer.addWidget(self._empty)
        self._empty.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(8000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        rt = self._runtime
        if rt is None or rt.history is None:
            self._show_empty(True)
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

        async def _load() -> None:
            try:
                records = await rt.history.records()
            except Exception as e:
                logger.warning(f"加载 history 失败: {e}")
                return
            # 最近 200 条够看了
            self._records = records[-200:]
            self._render_list()

        loop.create_task(_load())

    def _show_empty(self, on: bool) -> None:
        self._splitter.setVisible(not on)
        self._empty.setVisible(on)

    def _render_list(self) -> None:
        self._list.clear()
        if not self._records:
            self._show_empty(True)
            return
        self._show_empty(False)
        for i, rec in enumerate(self._records):
            role = rec.get("role", "?")
            content = (rec.get("content") or "")[:40].replace("\n", " ")
            prefix = {
                "user": "你",
                "assistant": "她",
                "tool": "工具",
                "system": "系统",
            }.get(role, role)
            item = QListWidgetItem(f"{prefix} · {content}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._list.addItem(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or idx >= len(self._records):
            return
        rec = self._records[idx]
        html = self._render_record(rec)
        self._detail.setHtml(html)

    def _render_record(self, rec: dict) -> str:
        role = rec.get("role", "?")
        content = rec.get("content") or ""
        reasoning = rec.get("reasoning_content") or ""
        tool_calls = rec.get("tool_calls") or []
        tool_call_id = rec.get("tool_call_id")

        head = {
            "user": "你",
            "assistant": "她",
            "tool": f"工具结果 · {tool_call_id or ''}",
            "system": "系统",
        }.get(role, role)

        parts = [f"<h3 style='margin:0'>{head}</h3>"]
        if reasoning:
            parts.append(
                "<details><summary style='color:#6B635A'>思考过程</summary>"
                f"<pre style='white-space:pre-wrap;color:#5A7A99'>{_escape(reasoning)}</pre></details>"
            )
        if content:
            parts.append(f"<pre style='white-space:pre-wrap'>{_escape(content)}</pre>")
        if tool_calls:
            parts.append("<h4>调用了：</h4><ul>")
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "?")
                args = func.get("arguments", "")
                parts.append(f"<li><b>{name}</b><pre style='white-space:pre-wrap'>{_escape(args)}</pre></li>")
            parts.append("</ul>")
        return "".join(parts)


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
