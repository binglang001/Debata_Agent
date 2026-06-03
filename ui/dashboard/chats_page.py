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
import re
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
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
        self._conversations: list[dict] = []
        self._current_key: str | None = None
        self._current_detail_html = ""

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
        self._list.currentItemChanged.connect(self._on_item_changed)
        splitter.addWidget(self._list)

        self._detail = QTextBrowser()
        self._detail.setPlaceholderText("点选左侧某个会话查看")
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
        selected_key = self._current_key
        list_scroll = self._list.verticalScrollBar().value()
        detail_bar = self._detail.verticalScrollBar()
        detail_scroll = detail_bar.value()
        detail_was_at_bottom = _scrollbar_near_bottom(detail_bar)

        self._conversations = _group_records_by_conversation(self._records)
        self._list.clear()
        if not self._conversations:
            self._show_empty(True)
            return
        self._show_empty(False)
        restore_row = -1
        for i, conv in enumerate(self._conversations):
            preview = (conv.get("preview") or "").replace("\n", " ")[:48]
            count = len(conv.get("records") or [])
            item = QListWidgetItem(f"{conv['label']}\n  {count} 条 · {preview}")
            item.setData(Qt.ItemDataRole.UserRole, conv["key"])
            self._list.addItem(item)
            if conv["key"] == selected_key:
                restore_row = i
        if restore_row < 0:
            restore_row = 0
        self._list.setCurrentRow(restore_row)
        self._list.verticalScrollBar().setValue(list_scroll)
        if self._current_key == selected_key:
            QTimer.singleShot(
                0,
                lambda: self._restore_detail_scroll(
                    detail_scroll,
                    stick_to_bottom=detail_was_at_bottom,
                ),
            )

    def _on_item_changed(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        conv = next((c for c in self._conversations if c["key"] == key), None)
        if conv is None:
            return
        previous_key = self._current_key
        self._current_key = key
        html = self._render_conversation(conv)
        if html == self._current_detail_html:
            return
        bar = self._detail.verticalScrollBar()
        old_scroll = bar.value()
        was_at_bottom = _scrollbar_near_bottom(bar)
        self._detail.setHtml(html)
        self._current_detail_html = html
        if previous_key == key:
            QTimer.singleShot(
                0,
                lambda: self._restore_detail_scroll(
                    old_scroll,
                    stick_to_bottom=was_at_bottom,
                ),
            )
        else:
            self._detail.moveCursor(QTextCursor.MoveOperation.Start)

    def _restore_detail_scroll(self, value: int, *, stick_to_bottom: bool) -> None:
        bar = self._detail.verticalScrollBar()
        if stick_to_bottom:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(min(value, bar.maximum()))

    def _render_conversation(self, conv: dict) -> str:
        parts = [f"<h3 style='margin:0 0 12px 0'>{_escape(conv['label'])}</h3>"]
        for rec in conv.get("records") or []:
            parts.append(self._render_record(rec))
            parts.append("<hr/>")
        return "".join(parts)

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

        parts = [f"<h4 style='margin:8px 0 4px 0'>{head}</h4>"]
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


_LEGACY_HEADER_RE = re.compile(
    r"^【(?P<timestamp>.*?) (?P<location>群聊 (?P<group_id>\S+)|私聊) "
    r"(?P<nickname>.*?)\((?P<user_id>.*?)\) msg_id=(?P<message_id>.*?)】",
    re.S,
)


def _group_records_by_conversation(records: list[dict]) -> list[dict]:
    """把线性 history 分组成会话。

    新记录优先读 metadata；旧记录从 MessagePipeline 写入的正文头部解析。
    assistant/tool/system 记录归入最近的 user 会话，避免刷新时只看到全局流水。
    """
    conversations: dict[str, dict] = {}
    order: list[str] = []
    current_key: str | None = None

    def _ensure(key: str, label: str) -> dict:
        if key not in conversations:
            conversations[key] = {
                "key": key,
                "label": label,
                "records": [],
                "preview": "",
            }
            order.append(key)
        return conversations[key]

    for rec in records:
        if rec.get("role") == "user":
            info = _conversation_info(rec)
            current_key = info["key"]
            conv = _ensure(info["key"], info["label"])
        else:
            if current_key is None:
                current_key = "system:global"
            conv = _ensure(current_key, "系统记录" if current_key == "system:global" else current_key)
        conv["records"].append(rec)
        content = (rec.get("content") or "").strip()
        if content:
            conv["preview"] = content

    # 最近活跃的会话排前面。
    return [conversations[k] for k in reversed(order)]


def _conversation_info(rec: dict) -> dict[str, str]:
    meta_messages = (rec.get("metadata") or {}).get("messages") or []
    first = meta_messages[0] if meta_messages else None
    if isinstance(first, dict):
        scope = first.get("scope") or "private"
        if scope == "group":
            group_id = str(first.get("group_id") or first.get("target_id") or "未知群")
            return {"key": f"group:{group_id}", "label": f"群聊 {group_id}"}
        user_id = str(first.get("user_id") or first.get("target_id") or "未知用户")
        nickname = str(first.get("nickname") or "私聊")
        return {"key": f"private:{user_id}", "label": f"私聊 {nickname}({user_id})"}

    content = rec.get("content") or ""
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        if match.group("group_id"):
            group_id = match.group("group_id")
            return {"key": f"group:{group_id}", "label": f"群聊 {group_id}"}
        user_id = match.group("user_id")
        nickname = match.group("nickname")
        return {"key": f"private:{user_id}", "label": f"私聊 {nickname}({user_id})"}
    return {"key": "unknown:history", "label": "未标记来源"}


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _scrollbar_near_bottom(bar: Any, threshold: int = 24) -> bool:
    return bar.maximum() - bar.value() <= threshold
