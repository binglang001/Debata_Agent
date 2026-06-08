"""对话页 —— 显示当前 active persona 的 history。

当前实现仍使用 QTextBrowser 渲染，但先把原始 history 记录做轻量
归一化：真实用户、助手动作、工具结果和系统事件分开呈现，避免把
运行时 XML/JSON 当成普通聊天内容。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, unquote, urlsplit

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing, palette_for_theme, resolve_theme_name
from ..wizard.components import EmptyState
from .copy import DASHBOARD_COPY
from .tool_display import format_tool_call, format_tool_result

logger = logging.getLogger(__name__)

DEFAULT_VISIBLE_RECORD_LIMIT = 300
VISIBLE_RECORD_STEP = 300
ARCHIVE_FETCH_PAGE_SIZE = 500
COMPACT_TEXT_LIMIT = 1800
INLINE_PREVIEW_LIMIT = 80

DisplayKind = Literal[
    "inbound_message",
    "outbound_message",
    "assistant_note",
    "tool_call",
    "tool_result",
    "system_event",
    "runtime_receipt",
    "reasoning",
]
DisplaySeverity = Literal["normal", "info", "warning", "error"]


@dataclass(slots=True)
class DisplayItem:
    item_id: str
    conversation_id: str
    timestamp: str | None
    kind: DisplayKind
    speaker_label: str | None
    speaker_id: str | None
    role_label: str
    text: str
    summary: str
    raw: dict[str, Any]
    related_tool_call_id: str | None = None
    related_message_id: str | None = None
    collapsed_by_default: bool = False
    severity: DisplaySeverity = "normal"
    tool_results: list[DisplayItem] = field(default_factory=list)


@dataclass(slots=True)
class SendDisplayContext:
    accepted_by_send_id: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    real_msg_ids: set[str] = field(default_factory=set)
    real_send_orders: set[tuple[str, int]] = field(default_factory=set)
    generated_msg_ids: set[str] = field(default_factory=set)
    generated_send_orders: set[tuple[str, int]] = field(default_factory=set)


class ChatsPage(QWidget):
    """列表 + 详情。"""

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._records: list[dict] = []
        self._conversations: list[dict] = []
        self._list_signature: list[tuple[str, int, str]] = []
        self._current_key: str | None = None
        self._current_detail_html = ""
        self._visible_record_limits: dict[str, int] = {}
        self._search_text = ""
        self._expanded_item_ids: set[str] = set()
        self._collapsed_item_ids: set[str] = set()
        self._default_expanded_item_ids: set[str] = set()

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

        self._load_more_btn = QPushButton("加载更早")
        self._load_more_btn.setProperty("role", "text")
        self._load_more_btn.clicked.connect(self._load_more_current)
        self._load_more_btn.setEnabled(False)
        head.addWidget(self._load_more_btn)

        self._show_all_btn = QPushButton("显示全部")
        self._show_all_btn.setProperty("role", "text")
        self._show_all_btn.clicked.connect(self._show_all_current)
        self._show_all_btn.setEnabled(False)
        head.addWidget(self._show_all_btn)
        outer.addLayout(head)

        filters = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索当前会话")
        self._search_input.textChanged.connect(self._on_filter_changed)
        filters.addWidget(self._search_input, 1)

        self._show_chat_cb = QCheckBox("聊天")
        self._show_chat_cb.setChecked(True)
        self._show_chat_cb.stateChanged.connect(self._on_filter_changed)
        filters.addWidget(self._show_chat_cb)

        self._show_system_cb = QCheckBox("系统")
        self._show_system_cb.setChecked(True)
        self._show_system_cb.stateChanged.connect(self._on_filter_changed)
        filters.addWidget(self._show_system_cb)

        self._show_tools_cb = QCheckBox("工具")
        self._show_tools_cb.setChecked(True)
        self._show_tools_cb.stateChanged.connect(self._on_filter_changed)
        filters.addWidget(self._show_tools_cb)

        self._media_only_cb = QCheckBox("只看图片/文件")
        self._media_only_cb.setChecked(False)
        self._media_only_cb.stateChanged.connect(self._on_filter_changed)
        filters.addWidget(self._media_only_cb)
        outer.addLayout(filters)

        expand_defaults = QHBoxLayout()
        expand_defaults.addWidget(QLabel("默认展开"))

        self._expand_reasoning_cb = QCheckBox("展开思考")
        self._expand_reasoning_cb.setChecked(False)
        self._expand_reasoning_cb.stateChanged.connect(self._on_filter_changed)
        expand_defaults.addWidget(self._expand_reasoning_cb)

        self._expand_system_cb = QCheckBox("展开系统")
        self._expand_system_cb.setChecked(False)
        self._expand_system_cb.stateChanged.connect(self._on_filter_changed)
        expand_defaults.addWidget(self._expand_system_cb)

        self._expand_tool_call_cb = QCheckBox("展开工具调用")
        self._expand_tool_call_cb.setChecked(False)
        self._expand_tool_call_cb.stateChanged.connect(self._on_filter_changed)
        expand_defaults.addWidget(self._expand_tool_call_cb)

        self._expand_tool_result_cb = QCheckBox("展开工具返回/结果")
        self._expand_tool_result_cb.setChecked(False)
        self._expand_tool_result_cb.stateChanged.connect(self._on_filter_changed)
        expand_defaults.addWidget(self._expand_tool_result_cb)
        expand_defaults.addStretch(1)
        outer.addLayout(expand_defaults)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_item_changed)
        splitter.addWidget(self._list)

        self._detail = QTextBrowser()
        self._detail.setPlaceholderText("点选左侧某个会话查看")
        self._detail.setOpenLinks(False)
        self._detail.anchorClicked.connect(self._on_detail_anchor_clicked)
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
                records = await _load_chat_page_records(rt)
            except Exception as e:
                logger.warning(f"加载 history 失败: {e}")
                return
            self._records = records
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
        if not self._conversations:
            self._list_signature = []
            self._list.clear()
            self._load_more_btn.setEnabled(False)
            self._show_all_btn.setEnabled(False)
            self._show_empty(True)
            return

        new_signature = _conversation_list_signature(self._conversations)
        if new_signature == self._list_signature:
            self._refresh_current_detail()
            return
        self._list_signature = new_signature

        self._list.clear()
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
        self._update_load_more_state()
        if self._current_key == selected_key:
            QTimer.singleShot(
                0,
                lambda: self._restore_detail_scroll(
                    detail_scroll,
                    stick_to_bottom=detail_was_at_bottom,
                ),
            )

    def _refresh_current_detail(self) -> None:
        if not self._current_key:
            return
        item = self._list.currentItem()
        if item is None:
            return
        if item.data(Qt.ItemDataRole.UserRole) != self._current_key:
            return
        self._on_item_changed(item, item)

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
        self._update_load_more_state(conv)
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

    def _load_more_current(self) -> None:
        if not self._current_key:
            return
        conv = next((c for c in self._conversations if c["key"] == self._current_key), None)
        if conv is None:
            return
        current = self._visible_record_limits.get(
            self._current_key,
            DEFAULT_VISIBLE_RECORD_LIMIT,
        )
        total = self._filtered_display_count(conv)
        self._visible_record_limits[self._current_key] = min(
            total,
            current + VISIBLE_RECORD_STEP,
        )
        self._current_detail_html = ""
        self._refresh_current_detail()

    def _show_all_current(self) -> None:
        if not self._current_key:
            return
        conv = next((c for c in self._conversations if c["key"] == self._current_key), None)
        if conv is None:
            return
        total = self._filtered_display_count(conv)
        self._visible_record_limits[self._current_key] = total
        self._current_detail_html = ""
        self._refresh_current_detail()

    def _on_filter_changed(self, *_args: Any) -> None:
        self._search_text = self._search_input.text().strip()
        self._current_detail_html = ""
        self._refresh_current_detail()

    def _on_detail_anchor_clicked(self, url: QUrl) -> None:
        item_id = _toggle_item_id_from_url(url)
        if not item_id:
            return
        if item_id in self._collapsed_item_ids:
            self._collapsed_item_ids.remove(item_id)
            self._expanded_item_ids.add(item_id)
        elif item_id in self._expanded_item_ids:
            self._expanded_item_ids.remove(item_id)
        elif item_id in self._default_expanded_item_ids:
            self._collapsed_item_ids.add(item_id)
        else:
            self._expanded_item_ids.add(item_id)
        self._current_detail_html = ""
        self._refresh_current_detail()

    def _update_load_more_state(self, conv: dict | None = None) -> None:
        if conv is None and self._current_key:
            conv = next((c for c in self._conversations if c["key"] == self._current_key), None)
        if conv is None:
            self._load_more_btn.setEnabled(False)
            self._load_more_btn.setText("加载更早")
            self._show_all_btn.setEnabled(False)
            return
        total = self._filtered_display_count(conv)
        limit = self._visible_record_limits.get(
            str(conv.get("key") or ""),
            DEFAULT_VISIBLE_RECORD_LIMIT,
        )
        hidden = max(0, total - limit)
        self._load_more_btn.setEnabled(hidden > 0)
        self._show_all_btn.setEnabled(hidden > 0)
        self._load_more_btn.setText(
            f"加载更早（{hidden}）" if hidden > 0 else "已显示全部"
        )

    def _restore_detail_scroll(self, value: int, *, stick_to_bottom: bool) -> None:
        bar = self._detail.verticalScrollBar()
        if stick_to_bottom:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(min(value, bar.maximum()))

    def _filtered_display_items_for_conversation(self, conv: dict) -> list[DisplayItem]:
        return _build_display_items(
            list(conv.get("records") or []),
            persona_name=_runtime_persona_name(self._runtime),
            search_text=self._search_text,
            show_chat=self._show_chat_cb.isChecked(),
            show_system=self._show_system_cb.isChecked(),
            show_tools=self._show_tools_cb.isChecked(),
            media_only=self._media_only_cb.isChecked(),
        )

    def _filtered_display_count(self, conv: dict) -> int:
        return len(self._filtered_display_items_for_conversation(conv))

    def _default_expanded_ids_for_items(self, items: list[DisplayItem]) -> set[str]:
        expanded: set[str] = set()
        for item in items:
            if self._item_default_expanded(item):
                expanded.add(_display_item_expand_id(item))
            for result in item.tool_results:
                if self._item_default_expanded(result):
                    expanded.add(_display_item_expand_id(result))
        return expanded

    def _item_default_expanded(self, item: DisplayItem) -> bool:
        if item.kind == "reasoning":
            return self._expand_reasoning_cb.isChecked()
        if item.kind in {"system_event", "runtime_receipt", "assistant_note"}:
            return self._expand_system_cb.isChecked()
        if item.kind == "tool_call":
            return self._expand_tool_call_cb.isChecked()
        if item.kind == "tool_result":
            return self._expand_tool_result_cb.isChecked()
        return False

    def _render_conversation(self, conv: dict) -> str:
        records = list(conv.get("records") or [])
        all_items = normalize_history_records(
            records,
            persona_name=_runtime_persona_name(self._runtime),
        )
        filtered_items = _filter_display_items(
            all_items,
            search_text=self._search_text,
            show_chat=self._show_chat_cb.isChecked(),
            show_system=self._show_system_cb.isChecked(),
            show_tools=self._show_tools_cb.isChecked(),
            media_only=self._media_only_cb.isChecked(),
        )
        limit = self._visible_record_limits.get(
            str(conv.get("key") or ""),
            DEFAULT_VISIBLE_RECORD_LIMIT,
        )
        visible = filtered_items[-limit:] if limit > 0 else filtered_items
        default_expanded = self._default_expanded_ids_for_items(visible)
        self._default_expanded_item_ids = default_expanded
        effective_expanded = (self._expanded_item_ids | default_expanded) - self._collapsed_item_ids
        hidden = max(0, len(filtered_items) - len(visible))
        parts = [
            _chat_html_style(_runtime_theme_name(self._runtime)),
            f"<h3 style='margin:0 0 4px 0'>{_escape(conv['label'])}</h3>"
            f"<div class='chat-meta'>已显示 {len(visible)} / 共 {len(all_items)} 条"
            f"；当前过滤后 {len(filtered_items)} 条</div>"
        ]
        if hidden:
            parts.append(
                "<div class='chat-record chat-event chat-event-system'>"
                f"还有 {hidden} 条更早记录未显示。点击上方“加载更早”逐步展开，或“显示全部”查看完整记录。"
                "</div>"
            )
        if not visible:
            parts.append(
                "<div class='chat-record chat-event chat-event-system'>"
                "当前搜索或过滤条件下没有可显示记录。"
                "</div>"
            )
        for item in visible:
            parts.append(_render_display_item(item, expanded_item_ids=effective_expanded))
        return "".join(parts)

    def _render_record(self, rec: dict) -> str:
        return _render_record_html(
            rec,
            persona_name=_runtime_persona_name(self._runtime),
        )


def _runtime_theme_name(runtime: Any) -> str:
    cfg = getattr(runtime, "config", None)
    app = getattr(cfg, "app", None)
    theme = getattr(app, "theme", None)
    if theme in {"light", "dark", "auto"}:
        return resolve_theme_name(theme)
    return resolve_theme_name("auto")


def _chat_html_style(theme_name: str) -> str:
    palette = palette_for_theme(theme_name)
    if palette.name == "dark":
        bot_bg = "#302B26"
        peer_bg = "#213631"
    else:
        bot_bg = "#F1E8D6"
        peer_bg = "#E8F1ED"
    return (
        "<style>"
        ".chat-record{margin:10px 0;}"
        ".chat-message-table{width:100%;border-collapse:collapse;margin:10px 0;}"
        ".chat-message-cell{vertical-align:top;}"
        ".chat-spacer-cell{font-size:1px;line-height:1px;}"
        ".chat-bubble-frame{border-collapse:separate;border-spacing:0;}"
        ".chat-name-line{font-weight:600;margin:0 0 4px 0;}"
        ".chat-bubble{text-align:left;max-width:100%;"
        "padding:10px 12px;border-radius:12px;"
        f"border:1px solid {palette.border};color:{palette.text_primary};"
        "overflow-wrap:anywhere;word-break:break-word;}"
        f".chat-bot .chat-bubble{{background:{bot_bg};}}"
        f".chat-peer .chat-bubble{{background:{peer_bg};}}"
        ".chat-side-left .chat-name-line{text-align:left;}"
        ".chat-side-right .chat-name-line{text-align:right;}"
        ".chat-event{text-align:center;font-size:13px;font-weight:400;line-height:1.45;"
        f"color:{palette.text_secondary};"
        "overflow-wrap:anywhere;word-break:break-word;}"
        ".chat-event-tool,.chat-event-system{font-weight:400;}"
        ".chat-event-title{font-weight:400;}"
        ".chat-event-detail{margin:6px auto 0 auto;padding:0;text-align:center;"
        "white-space:pre-wrap;max-width:78%;font-size:13px;font-weight:400;"
        f"color:{palette.text_secondary};"
        "overflow-wrap:anywhere;word-break:break-word;}"
        ".chat-head{font-weight:400;margin-bottom:6px;}"
        f".chat-meta{{color:{palette.text_secondary};font-size:12px;}}"
        ".chat-pre{white-space:pre-wrap;margin:6px 0 0 0;"
        "overflow-wrap:anywhere;word-break:break-word;}"
        f".chat-summary{{color:{palette.text_secondary};}}"
        f".chat-toggle{{color:{palette.accent_blue};text-decoration:none;}}"
        ".chat-tool-list{margin:6px 0 0 18px;}"
        ".chat-tool-result{margin:8px 0 0 0;padding:0;text-align:center;"
        f"color:{palette.text_secondary};"
        "overflow-wrap:anywhere;word-break:break-word;}"
        "</style>"
    )


def _toggle_item_id_from_url(url: QUrl) -> str | None:
    raw = url.toString()
    prefix = "diana-chat-toggle:"
    if not raw.startswith(prefix):
        return None
    item_id = unquote(raw.removeprefix(prefix))
    return item_id or None


def _toggle_href(item_id: str) -> str:
    return f"diana-chat-toggle:{quote(item_id, safe='')}"


def _display_item_expand_id(item: DisplayItem) -> str:
    return f"{item.conversation_id}\n{item.item_id}"


_LEGACY_HEADER_RE = re.compile(
    r"^【(?P<timestamp>.*?) (?P<location>群聊 (?P<group_id>\S+)|私聊) "
    r"(?P<nickname>.*?)\((?P<user_id>.*?)\) msg_id=(?P<message_id>.*?)】",
    re.S,
)
_LEGACY_HEADER_LINE_RE = re.compile(
    r"(?m)^【(?P<timestamp>[^\n】]*?) (?P<location>群聊 (?P<group_id>\S+)|私聊) "
    r"(?P<nickname>.*?)\((?P<user_id>.*?)\) msg_id=(?P<message_id>.*?)】"
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\s<>'\"|?*]+")
_WORKSPACE_PATH_RE = re.compile(r"\b(?:workspace=)?(?:incoming|workspace|outgoing)[/\\][^\s\]]+")
_CQ_MEDIA_RE = re.compile(r"\[CQ:(?:image|file|record|video)\b", re.I)
_MEDIA_OR_FILE_EXT_RE = re.compile(
    r"(?i)(?:^|[\s\"'=:/\\])[\w.\-()[\]/\\]+"
    r"\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|mov|mkv|webm|mp3|wav|ogg|flac|"
    r"pdf|docx?|xlsx?|pptx?|txt|md|zip|7z|rar|tar|gz|json|csv)\b"
)
_MEDIA_TOOL_NAMES = {
    "describe_image",
    "send_group_image",
    "send_private_image",
    "send_emoji",
    "upload_file",
    "read_file",
    "write_file",
    "edit_file",
    "get_forward_msg",
}


def _group_records_by_conversation(records: list[dict]) -> list[dict]:
    """把线性 history 分组成会话。

    新记录优先读 metadata；旧记录从 MessagePipeline 写入的正文头部解析。
    优先使用 record.conversation_id；无会话 ID 的 system/tool 归入系统记录。
    旧记录没有 conversation_id 时，assistant 仅在紧跟用户消息时归入最近会话，
    避免后台/撤回等系统轮次漂移到普通会话。
    """
    conversations: dict[str, dict] = {}
    order: list[str] = []
    current_key: str | None = None
    send_id_targets: dict[str, list[dict[str, str]]] = {}

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

    def _append(info: dict[str, str], rec: dict) -> None:
        conv = _ensure(info["key"], info["label"])
        conv["records"].append(rec)
        content = (rec.get("content") or "").strip()
        if content:
            conv["preview"] = content

    for rec in records:
        role = _record_role(rec)
        if role == "tool":
            _remember_send_id_targets(rec, send_id_targets)

        explicit_cid = rec.get("conversation_id")
        explicit_key = explicit_cid if isinstance(explicit_cid, str) else ""
        for info, fallback_record in _send_receipt_sent_fallback_records(
            rec,
            explicit_key=explicit_key,
        ):
            _append(info, fallback_record)

        send_status = _send_status_info(str(rec.get("content") or ""))
        if send_status and send_status.get("completed"):
            send_id = send_status.get("send_id") or ""
            targets = send_id_targets.get(send_id, [])
            if targets:
                accepted = _accepted_messages_for_send_id(records, send_id)
                for info in targets:
                    clone = {
                        **rec,
                        "_display_conversation_id": info["key"],
                        "_accepted_messages_for_send_status": accepted,
                    }
                    _append(info, clone)

        if isinstance(explicit_cid, str) and explicit_cid:
            info = _conversation_info_from_id(explicit_cid)
            if (
                send_status
                and send_status.get("completed")
                and info["key"] in {target["key"] for target in send_id_targets.get(send_id, [])}
            ):
                continue
            current_key = info["key"] if role == "user" else current_key
            _append(info, rec)
        elif role == "user":
            info = _conversation_info(rec)
            current_key = info["key"]
            _append(info, rec)
        elif role in {"system", "tool"}:
            _append({"key": "system:global", "label": "系统记录"}, rec)
        else:
            if current_key is None:
                _append({"key": "unknown:history", "label": "未标记来源"}, rec)
            else:
                _append(
                    {
                        "key": current_key,
                        "label": "系统记录" if current_key.startswith("system:") else current_key,
                    },
                    rec,
                )

    # 最近活跃的会话排前面。
    return [conversations[k] for k in reversed(order)]


def _targeted_tool_calls_for_record(
    rec: dict,
) -> tuple[list[tuple[dict[str, str], list[dict]]], dict[str, list[dict[str, str]]]]:
    by_conversation: dict[str, tuple[dict[str, str], list[dict]]] = {}
    call_targets: dict[str, list[dict[str, str]]] = {}
    for tool_call in rec.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        targets = _tool_call_target_infos(tool_call)
        if not targets:
            continue
        tool_call_id = str(tool_call.get("id") or "").strip()
        if tool_call_id:
            call_targets[tool_call_id] = targets
        for info in targets:
            entry = by_conversation.setdefault(info["key"], (info, []))
            entry[1].append(tool_call)
    return list(by_conversation.values()), call_targets


def _tool_call_target_infos(tool_call: dict) -> list[dict[str, str]]:
    func = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(func, dict):
        return []
    name = str(func.get("name") or "").strip()
    args = _parse_tool_arguments(func.get("arguments"))
    if name == "send_private_messages":
        targets = args.get("targets")
        if not isinstance(targets, list):
            targets = [args]
        return _unique_conversation_infos(
            _private_target_info(target)
            for target in targets
            if isinstance(target, dict)
        )
    if name == "send_group_message":
        return _unique_conversation_infos([_group_target_info(args)])
    return _conversation_infos_from_payload(args)


def _tool_result_target_infos(
    rec: dict,
    tool_call_targets: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    payload = _parse_json_object(str(rec.get("content") or ""))
    if payload:
        targets.extend(_conversation_infos_from_payload(payload))
        for key in ("sent", "accepted_messages", "accepted", "queued"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        targets.extend(_conversation_infos_from_payload(item))
    tool_call_id = str(rec.get("tool_call_id") or "").strip()
    if tool_call_id:
        targets.extend(tool_call_targets.get(tool_call_id, []))
    return _unique_conversation_infos(targets)


def _remember_send_id_targets(
    rec: dict,
    send_id_targets: dict[str, list[dict[str, str]]],
) -> None:
    payload = _parse_json_object(str(rec.get("content") or ""))
    if not payload:
        return
    send_id = str(payload.get("send_id") or "").strip()
    if not send_id:
        return
    targets: list[dict[str, str]] = []
    for key in ("accepted_messages", "sent"):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                targets.extend(_conversation_infos_from_payload(item))
    if targets:
        send_id_targets[send_id] = _unique_conversation_infos(
            [*send_id_targets.get(send_id, []), *targets]
        )


def _send_receipt_sent_fallback_records(
    rec: dict[str, Any],
    *,
    explicit_key: str,
) -> list[tuple[dict[str, str], dict[str, Any]]]:
    result: list[tuple[dict[str, str], dict[str, Any]]] = []
    for sent in _send_receipt_sent_items(str(rec.get("content") or "")):
        text = str(sent.get("content") or sent.get("label") or "").strip()
        if not text:
            continue
        for info in _conversation_infos_from_payload(sent):
            if info["key"] == explicit_key:
                continue
            fallback = {
                **sent,
                "role": "assistant",
                "direction": "outbound",
                "content": text,
                "conversation_id": info["key"],
                "_display_conversation_id": info["key"],
                "qq_visible": sent.get("qq_visible", True),
                "_synthetic_source": "send_receipt",
                "_record_order": rec.get("_record_order"),
            }
            timestamp = sent.get("time") or rec.get("timestamp") or rec.get("created_at")
            if timestamp and not fallback.get("timestamp"):
                fallback["timestamp"] = timestamp
            result.append((info, fallback))
    return result


def _accepted_messages_for_send_id(records: list[dict], send_id: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for rec in records:
        payload = _parse_json_object(str(rec.get("content") or ""))
        if not payload or str(payload.get("send_id") or "").strip() != send_id:
            continue
        accepted = payload.get("accepted_messages")
        if isinstance(accepted, list):
            messages.extend(dict(item) for item in accepted if isinstance(item, dict))
    return messages


def _conversation_infos_from_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    infos: list[dict[str, str]] = []
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if conversation_id:
        infos.append(_conversation_info_from_id(conversation_id))
    scope = str(payload.get("scope") or payload.get("target_type") or "").strip()
    if scope == "group":
        infos.append(_group_target_info(payload))
    elif scope in {"private", "user"}:
        infos.append(_private_target_info(payload))
    elif payload.get("group_id"):
        infos.append(_group_target_info(payload))
    elif payload.get("target_qq") or payload.get("user_id") or payload.get("target_id"):
        infos.append(_private_target_info(payload))
    return _unique_conversation_infos(infos)


def _private_target_info(payload: dict[str, Any]) -> dict[str, str] | None:
    target_id = str(
        payload.get("target_qq")
        or payload.get("user_id")
        or payload.get("target_id")
        or payload.get("sender_id")
        or ""
    ).strip()
    if not target_id:
        return None
    return _conversation_info_from_id(f"private:{target_id}")


def _group_target_info(payload: dict[str, Any]) -> dict[str, str] | None:
    group_id = str(payload.get("group_id") or payload.get("target_id") or "").strip()
    if not group_id:
        return None
    return _conversation_info_from_id(f"group:{group_id}")


def _unique_conversation_infos(infos: Iterable[dict[str, str] | None]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for info in infos:
        if not info:
            continue
        key = str(info.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({"key": key, "label": str(info.get("label") or key)})
    return result


def _parse_json_object(content: str) -> dict[str, Any] | None:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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


def _conversation_info_from_id(conversation_id: str) -> dict[str, str]:
    if ":" not in conversation_id:
        return {"key": conversation_id, "label": conversation_id}
    scope, target_id = conversation_id.split(":", 1)
    if scope == "group":
        return {"key": conversation_id, "label": f"群聊 {target_id}"}
    if scope == "private":
        return {"key": conversation_id, "label": f"私聊 {target_id}"}
    if scope == "system":
        return {"key": conversation_id, "label": _system_conversation_label(target_id)}
    return {"key": conversation_id, "label": conversation_id}


def _system_conversation_label(target_id: str) -> str:
    labels = {
        "global": "系统记录 · 全局",
        "proactive": "系统记录 · 主动思考",
        "wakeup": "系统记录 · 定时唤醒",
        "agent_task": "系统记录 · 后台任务",
        "request": "系统记录 · 请求处理",
    }
    return labels.get(target_id, f"系统记录 · {target_id}")


def _conversation_list_signature(conversations: list[dict]) -> list[tuple[str, int, str]]:
    signature: list[tuple[str, int, str]] = []
    for conv in conversations:
        signature.append(
            (
                str(conv.get("key") or ""),
                len(conv.get("records") or []),
                str(conv.get("preview") or ""),
            )
        )
    return signature


def _filter_visible_records(
    records: list[dict],
    *,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    media_only: bool = False,
) -> list[dict]:
    query = search_text.strip().casefold()
    result: list[dict] = []
    for record in records:
        if not _record_matches_display_filters(
            record,
            query=query,
            show_chat=show_chat,
            show_system=show_system,
            show_tools=show_tools,
            media_only=media_only,
        ):
            continue
        result.append(record)
    return result


def _build_render_items(
    records: list[dict],
    *,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    media_only: bool = False,
) -> list[tuple[dict, dict[str, list[dict]]]]:
    """Build display records and attach tool results to their tool calls.

    History stores assistant tool calls and tool results as separate raw records.
    The chat page should show the result under the corresponding call when the
    `tool_call_id` matches, while orphan results stay visible as standalone
    tool events.
    """
    tool_results, attached_tool_result_indexes = _collect_attached_tool_results(records)
    query = search_text.strip().casefold()
    items: list[tuple[dict, dict[str, list[dict]]]] = []

    for index, record in enumerate(records):
        if index in attached_tool_result_indexes:
            continue
        attached = _attached_results_for_record(record, tool_results) if show_tools else {}
        record_matches = _record_matches_display_filters(
            record,
            query=query,
            show_chat=show_chat,
            show_system=show_system,
            show_tools=show_tools,
            media_only=media_only,
        )
        attached_matches = bool(attached) and (
            not media_only
            or any(_record_has_media_or_file(result) for results in attached.values() for result in results)
        ) and (
            not query
            or any(
                query in _record_search_text(result).casefold()
                for results in attached.values()
                for result in results
            )
        )
        if record_matches or attached_matches:
            items.append((record, attached))
    return items


def _collect_attached_tool_results(records: list[dict]) -> tuple[dict[str, list[dict]], set[int]]:
    call_ids: set[str] = set()
    for record in records:
        call_ids.update(_record_tool_call_ids(record))

    results: dict[str, list[dict]] = {}
    attached_indexes: set[int] = set()
    if not call_ids:
        return results, attached_indexes

    for index, record in enumerate(records):
        if record.get("role") != "tool":
            continue
        tool_call_id = str(record.get("tool_call_id") or "").strip()
        if not tool_call_id or tool_call_id not in call_ids:
            continue
        results.setdefault(tool_call_id, []).append(record)
        attached_indexes.add(index)
    return results, attached_indexes


def _record_tool_call_ids(record: dict) -> list[str]:
    ids: list[str] = []
    for tool_call in record.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = str(tool_call.get("id") or "").strip()
        if tool_call_id:
            ids.append(tool_call_id)
    return ids


def _attached_results_for_record(
    record: dict,
    tool_results: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    attached: dict[str, list[dict]] = {}
    for tool_call_id in _record_tool_call_ids(record):
        results = tool_results.get(tool_call_id)
        if results:
            attached[tool_call_id] = results
    return attached


def _record_matches_display_filters(
    record: dict,
    *,
    query: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    media_only: bool = False,
) -> bool:
    categories = _record_display_categories(record)
    if not show_chat and "chat" in categories:
        categories.discard("chat")
    if not show_system and "system" in categories:
        categories.discard("system")
    if not show_tools and "tool" in categories:
        categories.discard("tool")
    if not categories:
        return False
    if media_only and not _record_has_media_or_file(record):
        return False
    return not query or query in _record_search_text(record).casefold()


def _record_has_media_or_file(record: dict) -> bool:
    content = str(record.get("content") or "")
    if _text_has_media_or_file(content):
        return True

    meta = record.get("metadata")
    if isinstance(meta, dict) and _text_has_media_or_file(json.dumps(meta, ensure_ascii=False)):
        return True

    for tool_call in record.get("tool_calls") or []:
        func = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(func, dict):
            continue
        name = str(func.get("name") or "")
        if name in _MEDIA_TOOL_NAMES:
            return True
        if _text_has_media_or_file(json.dumps(_parse_tool_arguments(func.get("arguments")), ensure_ascii=False)):
            return True
    return False


def _text_has_media_or_file(text: str) -> bool:
    if not text:
        return False
    if _CQ_MEDIA_RE.search(text):
        return True
    if "[图片" in text or "[文件" in text or "[视频" in text or "[语音" in text:
        return True
    if _MEDIA_OR_FILE_EXT_RE.search(text):
        return True
    lowered = text.casefold()
    return any(
        marker in lowered
        for marker in (
            "workspace=incoming/",
            "workspace=incoming\\",
            "image_ref",
            "image_path",
            "image_url",
            "file_path",
            "file_name",
            "file_id",
            "forward_id",
        )
    )


def _record_display_categories(record: dict) -> set[str]:
    role = _record_role(record)
    content = str(record.get("content") or "")
    categories: set[str] = set()
    if role == "tool":
        categories.add("tool")
    elif _runtime_event_summary(content) is not None or role == "system":
        categories.add("system")
    elif role in {"user", "assistant"}:
        if content.strip():
            categories.add("chat")
        if record.get("tool_calls"):
            categories.add("tool")
    else:
        categories.add("system")
    return categories


def _record_search_text(record: dict) -> str:
    parts = [str(record.get("role") or ""), str(record.get("content") or "")]
    tool_call_id = record.get("tool_call_id")
    if tool_call_id:
        parts.append(str(tool_call_id))
    tool_calls = record.get("tool_calls") or []
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False))
    meta = record.get("metadata")
    if isinstance(meta, dict):
        parts.append(json.dumps(meta, ensure_ascii=False))
    return "\n".join(parts)


def _runtime_persona_name(runtime: Any) -> str:
    persona = getattr(runtime, "persona", None)
    name = getattr(persona, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "助手"


def normalize_history_records(
    records: list[dict],
    *,
    persona_name: str,
    bot_user_id: str | None = None,
) -> list[DisplayItem]:
    """把 raw history 记录归一化成 UI 可显示项，并挂靠工具结果。"""
    items: list[DisplayItem] = []
    tool_calls: dict[str, DisplayItem] = {}
    seen_ids: set[str] = set()
    send_context = _build_send_display_context(records)
    for record_index, record in enumerate(records):
        record_items = normalize_history_record(
            record,
            persona_name=persona_name,
            bot_user_id=bot_user_id,
        )
        record_items.extend(
            _send_status_outbound_items(
                record,
                context=send_context,
                persona_name=persona_name,
                bot_user_id=bot_user_id,
            )
        )
        for item in record_items:
            if _should_skip_generated_outbound(item, send_context):
                continue
            item.item_id = _unique_item_id(item.item_id, record_index, seen_ids)
            if item.kind == "tool_call" and item.related_tool_call_id:
                tool_calls[item.related_tool_call_id] = item
                items.append(item)
                continue
            if item.kind == "tool_result" and item.related_tool_call_id:
                parent = tool_calls.get(item.related_tool_call_id)
                if parent is not None:
                    parent.tool_results.append(item)
                    continue
            items.append(item)
            _remember_generated_outbound(item, send_context)
    return _sort_display_items(items)


def _build_send_display_context(records: list[dict]) -> SendDisplayContext:
    context = SendDisplayContext()
    for rec in records:
        content = str(rec.get("content") or "")
        runtime = _runtime_event_summary(content)
        if runtime is None and not rec.get("_synthetic_source") and _record_is_qq_visible_outbound(rec):
            msg_id = _record_message_id(rec)
            if msg_id:
                context.real_msg_ids.add(msg_id)
            send_id = _record_send_id(rec)
            order = _record_send_order(rec)
            if send_id and order is not None:
                context.real_send_orders.add((send_id, order))
        cloned_accepted = rec.get("_accepted_messages_for_send_status")
        cloned_send_status = _send_status_info(content)
        if (
            isinstance(cloned_accepted, list)
            and cloned_send_status
            and cloned_send_status.get("send_id")
        ):
            context.accepted_by_send_id.setdefault(
                str(cloned_send_status["send_id"]),
                [dict(item) for item in cloned_accepted if isinstance(item, dict)],
            )
        payload = _parse_json_object(content)
        if not payload:
            continue
        send_id = str(payload.get("send_id") or "").strip()
        accepted = payload.get("accepted_messages")
        if send_id and isinstance(accepted, list):
            messages = [dict(item) for item in accepted if isinstance(item, dict)]
            if messages:
                context.accepted_by_send_id.setdefault(send_id, messages)
    return context


def _send_status_outbound_items(
    rec: dict[str, Any],
    *,
    context: SendDisplayContext,
    persona_name: str,
    bot_user_id: str | None,
) -> list[DisplayItem]:
    status = _send_status_info(str(rec.get("content") or ""))
    if not status:
        return []
    send_id = status.get("send_id") or ""
    accepted = context.accepted_by_send_id.get(send_id)
    msg_ids = status.get("msg_ids") or []
    if not send_id or not accepted or not msg_ids or not status.get("completed"):
        return []
    target_conversation_id = _record_conversation_id_for_display(rec)
    timestamp = _record_timestamp(rec)
    base_id = _record_display_base_id(rec, conversation_id=target_conversation_id, role=_record_role(rec))
    items: list[DisplayItem] = []
    for order, message in enumerate(accepted):
        conversation_id = _accepted_message_conversation_id(message, fallback=target_conversation_id)
        if conversation_id != target_conversation_id:
            continue
        content = str(message.get("content") or message.get("label") or "").strip()
        if not content:
            continue
        msg_id = msg_ids[order] if order < len(msg_ids) else ""
        raw = {
            **message,
            "send_id": send_id,
            "send_order": order,
            "msg_id": msg_id,
            "qq_visible": True,
            "_synthetic_source": "send_status",
        }
        items.append(
            DisplayItem(
                item_id=f"{base_id}:send_status_sent:{order}",
                conversation_id=conversation_id,
                timestamp=str(message.get("time") or timestamp or "") or None,
                kind="outbound_message",
                speaker_label=persona_name,
                speaker_id=bot_user_id,
                role_label="角色",
                text=content,
                summary=_sent_item_status(raw),
                raw=raw,
                related_message_id=msg_id or None,
                collapsed_by_default=False,
            )
        )
    return items


def _accepted_message_conversation_id(message: dict[str, Any], *, fallback: str) -> str:
    conversation_id = str(message.get("conversation_id") or "").strip()
    if conversation_id:
        return conversation_id
    infos = _conversation_infos_from_payload(message)
    if infos:
        return infos[0]["key"]
    return fallback


def _should_skip_generated_outbound(item: DisplayItem, context: SendDisplayContext) -> bool:
    if item.kind != "outbound_message" or not _is_generated_outbound(item):
        return False
    if item.related_message_id and (
        item.related_message_id in context.real_msg_ids
        or item.related_message_id in context.generated_msg_ids
    ):
        return True
    send_id = str(item.raw.get("send_id") or "").strip()
    order = _raw_send_order(item.raw)
    return bool(
        send_id
        and order is not None
        and ((send_id, order) in context.real_send_orders or (send_id, order) in context.generated_send_orders)
    )


def _remember_generated_outbound(item: DisplayItem, context: SendDisplayContext) -> None:
    if item.kind != "outbound_message" or not _is_generated_outbound(item):
        return
    if item.related_message_id:
        context.generated_msg_ids.add(item.related_message_id)
    send_id = str(item.raw.get("send_id") or "").strip()
    order = _raw_send_order(item.raw)
    if send_id and order is not None:
        context.generated_send_orders.add((send_id, order))


def _is_generated_outbound(item: DisplayItem) -> bool:
    return bool(item.raw.get("_synthetic_source"))


def _record_send_id(rec: dict[str, Any]) -> str | None:
    value = rec.get("send_id")
    if value:
        return str(value)
    meta = rec.get("metadata")
    if isinstance(meta, dict) and meta.get("send_id"):
        return str(meta.get("send_id"))
    return None


def _record_send_order(rec: dict[str, Any]) -> int | None:
    for source in (rec, rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}):
        order = _raw_send_order(source)
        if order is not None:
            return order
    return None


def _raw_send_order(raw: dict[str, Any]) -> int | None:
    for key in ("send_order", "order", "message_index", "index"):
        value = raw.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def normalize_history_record(
    rec: dict[str, Any],
    *,
    persona_name: str,
    bot_user_id: str | None = None,
) -> list[DisplayItem]:
    """把一条 history/archive 记录拆成一个或多个 DisplayItem。"""
    role = _record_role(rec)
    content = str(rec.get("content") or "")
    conversation_id = _record_conversation_id_for_display(rec)
    timestamp = _record_timestamp(rec)
    base_id = _record_display_base_id(rec, conversation_id=conversation_id, role=role)
    runtime = _runtime_event_summary(content)

    if runtime is not None:
        title, summary = runtime
        items = [
            DisplayItem(
                item_id=f"{base_id}:runtime",
                conversation_id=conversation_id,
                timestamp=timestamp,
                kind="runtime_receipt" if "<send_receipt" in content else "system_event",
                speaker_label="系统",
                speaker_id=None,
                role_label="系统",
                text=content,
                summary=f"{title} · {summary}" if summary else title,
                raw=rec,
                collapsed_by_default=True,
                severity=_runtime_severity(content),
            )
        ]
        for index, sent in enumerate(_send_receipt_sent_items(content)):
            sent_conversation_id = _accepted_message_conversation_id(sent, fallback=conversation_id)
            if sent_conversation_id != conversation_id:
                continue
            msg_id = str(sent.get("msg_id") or "").strip() or None
            items.append(
                DisplayItem(
                    item_id=f"{base_id}:sent:{index}",
                    conversation_id=sent_conversation_id,
                    timestamp=str(sent.get("time") or timestamp or "") or None,
                    kind="outbound_message",
                    speaker_label=persona_name,
                    speaker_id=bot_user_id,
                    role_label="角色",
                    text=str(sent.get("content") or sent.get("label") or "").strip(),
                    summary=_sent_item_status(sent),
                    raw=dict(sent),
                    related_message_id=msg_id,
                    collapsed_by_default=False,
                )
            )
        return items

    if role == "tool":
        tool_call_id = str(rec.get("tool_call_id") or "").strip() or None
        return [
            DisplayItem(
                item_id=f"{base_id}:tool_result",
                conversation_id=conversation_id,
                timestamp=timestamp,
                kind="tool_result",
                speaker_label="工具结果",
                speaker_id=None,
                role_label="工具结果",
                text=content,
                summary=_format_tool_result_summary(content),
                raw=rec,
                related_tool_call_id=tool_call_id,
                collapsed_by_default=True,
                severity=_tool_result_severity(content),
            )
        ]

    if role == "user":
        legacy_messages = _split_legacy_header_messages(content)
        if legacy_messages:
            return [
                DisplayItem(
                    item_id=f"{base_id}:legacy:{index}:{message['message_id'] or index}",
                    conversation_id=message["conversation_id"],
                    timestamp=message["timestamp"] or timestamp,
                    kind="inbound_message",
                    speaker_label=message["speaker_label"],
                    speaker_id=message["speaker_id"],
                    role_label="成员",
                    text=message["text"],
                    summary=_compact_inline_tokens(_first_nonempty_line(message["text"])),
                    raw={**rec, "content": message["text"], "legacy_header": message["raw_header"]},
                    related_message_id=message["message_id"],
                )
                for index, message in enumerate(legacy_messages)
            ]
        stripped = _strip_legacy_header(content)
        return [
            DisplayItem(
                item_id=f"{base_id}:inbound",
                conversation_id=conversation_id,
                timestamp=timestamp,
                kind="inbound_message",
                speaker_label=_user_record_label(rec),
                speaker_id=_user_record_sender_id(rec),
                role_label="成员",
                text=stripped,
                summary=_compact_inline_tokens(_first_nonempty_line(stripped)),
                raw=rec,
                related_message_id=_record_message_id(rec),
            )
        ]

    if role == "assistant":
        items: list[DisplayItem] = []
        reasoning = str(rec.get("reasoning_content") or "")
        if reasoning:
            items.append(
                DisplayItem(
                    item_id=f"{base_id}:reasoning",
                    conversation_id=conversation_id,
                    timestamp=timestamp,
                    kind="reasoning",
                    speaker_label=persona_name,
                    speaker_id=bot_user_id,
                    role_label="思考",
                    text=reasoning,
                    summary="思考过程",
                    raw=rec,
                    collapsed_by_default=True,
                )
            )
        if content.strip():
            outbound = _record_is_qq_visible_outbound(rec)
            items.append(
                DisplayItem(
                    item_id=f"{base_id}:assistant_text",
                    conversation_id=conversation_id,
                    timestamp=timestamp,
                    kind="outbound_message" if outbound else "assistant_note",
                    speaker_label=persona_name,
                    speaker_id=bot_user_id,
                    role_label="角色" if outbound else "助手内部",
                    text=content,
                    summary=_sent_item_status(rec) if outbound else _assistant_note_summary(content),
                    raw=rec,
                    related_message_id=_record_message_id(rec),
                    collapsed_by_default=not outbound,
                )
            )
        for index, tool_call in enumerate(rec.get("tool_calls") or []):
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = str(tool_call.get("id") or f"{base_id}:tool:{index}").strip()
            tool_display = format_tool_call(tool_call)
            items.append(
                DisplayItem(
                    item_id=f"{base_id}:tool_call:{index}",
                    conversation_id=conversation_id,
                    timestamp=timestamp,
                    kind="tool_call",
                    speaker_label=persona_name,
                    speaker_id=bot_user_id,
                    role_label="工具动作",
                    text=tool_display.detail,
                    summary=tool_display.summary,
                    raw={"record": rec, "tool_call": tool_call},
                    related_tool_call_id=tool_call_id,
                    collapsed_by_default=True,
                    severity=_tool_call_severity(tool_call),
                )
            )
        return items

    return [
        DisplayItem(
            item_id=f"{base_id}:system",
            conversation_id=conversation_id,
            timestamp=timestamp,
            kind="system_event",
            speaker_label="系统",
            speaker_id=None,
            role_label="系统",
            text=content,
            summary=_compact_inline_tokens(_first_nonempty_line(content)) or "系统事件",
            raw=rec,
            collapsed_by_default=True,
        )
    ]


def _unique_item_id(item_id: str, record_index: int, seen_ids: set[str]) -> str:
    if item_id not in seen_ids:
        seen_ids.add(item_id)
        return item_id
    unique = f"{item_id}:{record_index}"
    suffix = 1
    while unique in seen_ids:
        suffix += 1
        unique = f"{item_id}:{record_index}:{suffix}"
    seen_ids.add(unique)
    return unique


def _sort_display_items(items: list[DisplayItem]) -> list[DisplayItem]:
    return [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda pair: _display_item_sort_key(pair[1], pair[0]),
        )
    ]


def _display_item_sort_key(item: DisplayItem, fallback_order: int) -> tuple[int, float, int, int]:
    sort_ts = _display_item_sort_ts(item)
    record_order = _display_item_record_order(item, fallback_order=fallback_order)
    if sort_ts is None:
        return 1, 0.0, record_order, fallback_order
    return 0, sort_ts, record_order, fallback_order


def _display_item_sort_ts(item: DisplayItem) -> float | None:
    raw = item.raw
    record = raw.get("record") if isinstance(raw.get("record"), dict) else raw
    for value in (
        record.get("_sort_ts"),
        record.get("timestamp"),
        record.get("time"),
        record.get("created_at"),
        item.timestamp,
    ):
        parsed = _parse_timestamp_value(value)
        if parsed is not None:
            return parsed
    return None


def _display_item_record_order(item: DisplayItem, *, fallback_order: int) -> int:
    raw = item.raw
    record = raw.get("record") if isinstance(raw.get("record"), dict) else raw
    value = record.get("_record_order")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return fallback_order


def _parse_timestamp_value(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _build_display_items(
    records: list[dict],
    *,
    persona_name: str,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    media_only: bool = False,
) -> list[DisplayItem]:
    return _filter_display_items(
        normalize_history_records(records, persona_name=persona_name),
        search_text=search_text,
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
        media_only=media_only,
    )


def _filter_display_items(
    items: list[DisplayItem],
    *,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    media_only: bool = False,
) -> list[DisplayItem]:
    query = search_text.strip().casefold()
    return [
        item
        for item in items
        if _display_item_matches_filters(
            item,
            query=query,
            show_chat=show_chat,
            show_system=show_system,
            show_tools=show_tools,
            media_only=media_only,
        )
    ]


def _display_item_matches_filters(
    item: DisplayItem,
    *,
    query: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    media_only: bool,
) -> bool:
    categories = _display_item_categories(item)
    if not show_chat and "chat" in categories:
        categories.discard("chat")
    if not show_system and "system" in categories:
        categories.discard("system")
    if not show_tools and "tool" in categories:
        categories.discard("tool")
    if not categories:
        return False
    if media_only and not _display_item_has_media_or_file(item):
        return False
    if not query:
        return True
    if query in _display_item_search_text(item).casefold():
        return True
    return item.kind == "tool_call" and any(
        query in _display_item_search_text(result).casefold()
        for result in item.tool_results
    )


def _display_item_categories(item: DisplayItem) -> set[str]:
    if item.kind in {"inbound_message", "outbound_message"}:
        return {"chat"}
    if item.kind in {"tool_call", "tool_result"}:
        return {"tool"}
    return {"system"}


def _display_item_has_media_or_file(item: DisplayItem) -> bool:
    if _text_has_media_or_file(item.text) or _text_has_media_or_file(item.summary):
        return True
    if _text_has_media_or_file(json.dumps(item.raw, ensure_ascii=False, default=str)):
        return True
    return any(_display_item_has_media_or_file(result) for result in item.tool_results)


def _display_item_search_text(item: DisplayItem) -> str:
    parts = [
        item.kind,
        item.speaker_label or "",
        item.speaker_id or "",
        item.role_label,
        item.text,
        item.summary,
        item.related_tool_call_id or "",
        item.related_message_id or "",
        json.dumps(item.raw, ensure_ascii=False, default=str),
    ]
    for result in item.tool_results:
        parts.append(_display_item_search_text(result))
    return "\n".join(parts)


def _render_display_item(
    item: DisplayItem,
    *,
    expanded_item_ids: set[str] | None = None,
) -> str:
    expanded_item_ids = expanded_item_ids or set()
    expand_id = _display_item_expand_id(item)
    if item.kind == "inbound_message":
        return _render_chat_message_item(
            item,
            css="chat-peer",
            side="right",
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
    if item.kind == "outbound_message":
        return _render_chat_message_item(
            item,
            css="chat-bot",
            side="left",
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
    if item.kind == "tool_call":
        return _render_tool_call_item(item, expanded_item_ids=expanded_item_ids)
    if item.kind == "tool_result":
        return _render_tool_result_item(
            item,
            expanded_item_ids=expanded_item_ids,
        )
    if item.kind == "runtime_receipt":
        return _render_expandable_event_record(
            "系统消息",
            _system_event_detail(item),
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            event_class="chat-event-system",
        )
    if item.kind == "assistant_note":
        return _render_expandable_event_record(
            f"{item.speaker_label or '助手'} · 内部文本",
            _system_event_detail(item),
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            event_class="chat-event-system",
        )
    if item.kind == "reasoning":
        return _render_chat_message_item(
            item,
            css="chat-bot",
            side="left",
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            label_override=f"{item.speaker_label or '助手'} · 思考过程",
            body_html=_render_expandable_detail(
                item.text,
                toggle_id=expand_id,
                expanded=expand_id in expanded_item_ids,
                collapsed_label="点击展开",
            ),
        )
    if item.kind == "system_event" and item.speaker_label == "系统" and " · " in item.summary:
        return _render_expandable_event_record(
            "系统消息",
            _system_event_detail(item),
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            event_class="chat-event-system",
        )
    return _render_expandable_event_record(
        "系统消息",
        _system_event_detail(item),
        toggle_id=expand_id,
        expanded=expand_id in expanded_item_ids,
        event_class="chat-event-system",
    )


def _render_chat_message_item(
    item: DisplayItem,
    *,
    css: str,
    side: Literal["left", "right"],
    toggle_id: str,
    expanded: bool,
    label_override: str | None = None,
    body_html: str | None = None,
) -> str:
    label = label_override or item.speaker_label or item.role_label
    meta = _chat_item_meta(item)
    side_class = f"chat-side-{side}"
    align = side
    spacer_cell = "<td class='chat-spacer-cell' width='22%'>&nbsp;</td>"
    message_cell_parts = [
        f"<td class='chat-message-cell' width='78%' align='{align}' valign='top'>",
        f"<div class='chat-name-line' align='{align}'>{_escape(label)}",
    ]
    if meta:
        message_cell_parts.append(f" <span class='chat-meta'>{_escape(meta)}</span>")
    message_cell_parts.extend(
        [
            "</div>",
            f"<table class='chat-bubble-frame' align='{align}' cellspacing='0' cellpadding='0' border='0'>",
            "<tr><td class='chat-bubble' data-rounded='qt-inline-radius' "
            "style='border-radius:12px;padding:10px 12px;'>",
            body_html
            if body_html is not None
            else _render_text_block(item.text or "(空消息)", toggle_id=toggle_id, expanded=expanded),
            "</td></tr></table>",
            "</td>",
        ]
    )
    message_cell = "".join(message_cell_parts)
    cells = f"{message_cell}{spacer_cell}" if side == "left" else f"{spacer_cell}{message_cell}"
    return (
        f"<table class='chat-record chat-message-table {side_class} {css}' "
        "width='100%' cellspacing='0' cellpadding='0' border='0'>"
        f"<tr>{cells}</tr></table>"
    )


def _render_expandable_detail(
    detail: str,
    *,
    toggle_id: str,
    expanded: bool,
    collapsed_label: str = "点击展开",
) -> str:
    label = "收起" if expanded else collapsed_label
    parts = [
        "<div class='chat-summary'>"
        f"<a class='chat-toggle' href='{_escape_attr(_toggle_href(toggle_id))}'>{_escape(label)}</a>"
        "</div>"
    ]
    if expanded:
        parts.append(f"<pre class='chat-pre'>{_escape(detail)}</pre>")
    return "".join(parts)


def _render_expandable_event_record(
    title: str,
    detail: str,
    *,
    toggle_id: str,
    expanded: bool,
    event_class: str,
) -> str:
    label = "收起" if expanded else "点击展开"
    parts = [
        f"<div class='chat-record chat-event {event_class}' align='center'>",
        f"<span class='chat-event-title'>{_escape(title)}</span>",
        " · ",
        f"<a class='chat-toggle' href='{_escape_attr(_toggle_href(toggle_id))}'>{_escape(label)}</a>",
    ]
    if expanded and detail:
        parts.append(f"<div class='chat-event-detail'>{_escape(detail)}</div>")
    parts.append("</div>")
    return "".join(parts)


def _render_tool_result_item(
    item: DisplayItem,
    *,
    expanded_item_ids: set[str] | None = None,
    tool_name: str | None = None,
) -> str:
    expanded_item_ids = expanded_item_ids or set()
    expand_id = _display_item_expand_id(item)
    display = format_tool_result(
        item.text,
        tool_name=tool_name,
        tool_call_id=item.related_tool_call_id,
    )
    return _render_expandable_event_record(
        display.title,
        display.detail,
        toggle_id=expand_id,
        expanded=expand_id in expanded_item_ids,
        event_class="chat-event-tool",
    )


def _render_tool_call_item(
    item: DisplayItem,
    *,
    expanded_item_ids: set[str] | None = None,
) -> str:
    expanded_item_ids = expanded_item_ids or set()
    expand_id = _display_item_expand_id(item)
    tool_call = item.raw.get("tool_call") if isinstance(item.raw, dict) else None
    display = format_tool_call(tool_call if isinstance(tool_call, dict) else {})
    parts = [
        _render_expandable_event_record(
            f"{item.speaker_label or '助手'} · {display.title}",
            display.detail,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            event_class="chat-event-tool",
        )
    ]
    if item.tool_results:
        for result in item.tool_results:
            parts.append(
                _render_tool_result_item(
                    result,
                    expanded_item_ids=expanded_item_ids,
                    tool_name=display.tool_name,
                )
            )
    return "".join(parts)


def _system_event_detail(item: DisplayItem) -> str:
    content = item.text.strip()
    if not content:
        return "系统消息：空内容。"
    if "<task_context" in content:
        return _format_task_context_detail(content)
    if "<send_status" in content:
        return f"系统消息：发送状态。{_format_send_status_summary(content)}。"
    if "<send_receipt" in content:
        return f"系统消息：发送回执。{_format_send_receipt_summary(content)}。"
    if "<send_receipt_task" in content:
        return "系统消息：发送回执任务。运行时正在跟踪消息投递状态。"
    compact = _compact_inline_tokens(content)
    if compact != content:
        return f"系统消息：{compact}。原始数据已隐藏。"
    return content


def _format_task_context_detail(content: str) -> str:
    lines = ["系统消息：运行时上下文。"]
    first_line = _first_nonempty_line(_extract_tag_text(content, "task_context") or content)
    if first_line and "现在是" in first_line:
        lines.append(first_line.rstrip("。") + "。")
    conv_match = re.search(r"当前会话：([^。\n]+)", content)
    if conv_match:
        lines.append(f"当前会话：{conv_match.group(1).strip()}。")
    group_count = len(re.findall(r"<recent_group_messages\b", content))
    private_count = len(re.findall(r"<recent_private_messages\b", content))
    recent_parts = []
    if group_count:
        recent_parts.append(f"{group_count} 组群聊窗口")
    if private_count:
        recent_parts.append(f"{private_count} 组私聊窗口")
    if recent_parts:
        lines.append("包含最近消息：" + "、".join(recent_parts) + "。")
    if "可用表情" in content:
        lines.append("包含可用表情提示。")
    if len(lines) == 1:
        lines.append("原始系统上下文已隐藏。")
    return "\n".join(lines)


def _render_event_record(head: str, body: str, *, event_class: str) -> str:
    parts = [
        f"<div class='chat-record chat-event {event_class}' align='center'>",
        f"<span class='chat-event-title'>{_escape(head)}</span>",
    ]
    if body:
        parts.append(f"<div class='chat-event-detail'>{body}</div>")
    parts.append("</div>")
    return "".join(parts)


def _split_display_summary(summary: str) -> tuple[str, str]:
    if " · " not in summary:
        return summary or "系统事件", summary or "系统事件"
    title, detail = summary.split(" · ", 1)
    return title or "系统事件", detail or title or "系统事件"


def _chat_item_meta(item: DisplayItem) -> str:
    parts = []
    if item.timestamp:
        parts.append(item.timestamp)
    if item.speaker_id and item.speaker_id not in str(item.speaker_label or ""):
        parts.append(item.speaker_id)
    if item.summary and item.kind == "outbound_message":
        parts.append(item.summary)
    return " · ".join(parts)


def _record_role(rec: dict[str, Any]) -> str:
    role = str(rec.get("role") or "").strip()
    direction = str(rec.get("direction") or "").strip()
    if role in {"system", "tool"}:
        return role
    if direction == "outbound":
        return "assistant"
    if direction == "inbound":
        return "user"
    if role:
        return role
    if str(rec.get("conversation_id") or "").startswith("system:"):
        return "system"
    return "user"


def _record_conversation_id_for_display(rec: dict[str, Any]) -> str:
    display = rec.get("_display_conversation_id")
    if isinstance(display, str) and display:
        return display
    direct = rec.get("conversation_id")
    if isinstance(direct, str) and direct:
        return direct
    if _record_role(rec) == "user":
        return _conversation_info(rec)["key"]
    return "system:global" if _record_role(rec) in {"system", "tool"} else "unknown:history"


def _record_timestamp(rec: dict[str, Any]) -> str | None:
    for key in ("timestamp", "time", "created_at"):
        value = rec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        date = meta.get("date")
        if isinstance(date, str) and date.strip():
            return date.strip()
    content = str(rec.get("content") or "")
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        return match.group("timestamp")
    return None


def _record_display_base_id(rec: dict[str, Any], *, conversation_id: str, role: str) -> str:
    for key in ("archive_id", "id", "message_id", "msg_id", "tool_call_id"):
        value = rec.get(key)
        if value:
            return str(value)
    content = str(rec.get("content") or "")
    return f"{conversation_id}:{role}:{_record_timestamp(rec) or ''}:{len(content)}"


def _record_message_id(rec: dict[str, Any]) -> str | None:
    for key in ("message_id", "msg_id", "original_msg_id"):
        value = rec.get(key)
        if value:
            return str(value)
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        for key in ("original_msg_id", "message_id", "msg_id"):
            value = meta.get(key)
            if value:
                return str(value)
    content = str(rec.get("content") or "")
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        return match.group("message_id")
    return None


def _record_is_qq_visible_outbound(rec: dict[str, Any]) -> bool:
    if str(rec.get("direction") or "") == "outbound":
        return rec.get("qq_visible") is not False
    if rec.get("qq_visible") is True:
        return True
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        if str(meta.get("direction") or "") == "outbound":
            return meta.get("qq_visible") is not False
        if meta.get("qq_visible") is True:
            return True
    return False


def _user_record_sender_id(rec: dict[str, Any]) -> str | None:
    meta_messages = (rec.get("metadata") or {}).get("messages") or []
    first = meta_messages[0] if meta_messages else None
    if isinstance(first, dict):
        value = first.get("user_id") or first.get("sender_id") or first.get("target_id")
        return str(value) if value else None
    for key in ("sender_id", "user_id"):
        value = rec.get(key)
        if value:
            return str(value)
    content = str(rec.get("content") or "")
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        return match.group("user_id")
    return None


def _sent_item_status(item: dict[str, Any]) -> str:
    parts = []
    status = item.get("status")
    if status:
        parts.append(_status_label(str(status)))
    elif item.get("qq_visible") is True or item.get("direction") == "outbound":
        parts.append("已发送")
    if item.get("delivery"):
        parts.append(f"投递 {item.get('delivery')}")
    if item.get("qq_visible") == "pending":
        parts.append("QQ 可见性待确认")
    elif item.get("qq_visible") is False:
        parts.append("未 QQ 可见")
    if item.get("msg_id"):
        parts.append(f"msg_id={item.get('msg_id')}")
    return " · ".join(parts) or "已发送"


def _status_label(status: str) -> str:
    labels = {
        "sent": "已发送",
        "accepted": "已入队",
        "queued": "排队中",
        "pending": "等待确认",
        "stale": "已过期",
        "failed": "失败",
        "needs_review": "等待复核",
        "needs_review_again": "等待再次复核",
    }
    return labels.get(status, status)


def _assistant_note_summary(content: str) -> str:
    first = _compact_inline_tokens(_first_nonempty_line(content))
    return first or "未发送文本"


def _runtime_severity(content: str) -> DisplaySeverity:
    summary = _format_send_receipt_summary(content) if "<send_receipt" in content else content
    lowered = summary.casefold()
    if any(token in lowered for token in ("failed", "失败", "stale", "过期")):
        return "warning"
    return "info"


def _tool_result_severity(content: str) -> DisplaySeverity:
    lowered = content.casefold()
    if any(token in lowered for token in ("failed", '"ok": false', "error", "失败")):
        return "warning"
    return "info"


def _tool_call_severity(tool_call: dict[str, Any]) -> DisplaySeverity:
    name = _tool_call_name(tool_call)
    if name in {"set_group_kick", "set_group_ban", "set_group_whole_ban", "set_group_leave"}:
        return "warning"
    return "info"


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    func = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(func, dict):
        return ""
    return str(func.get("name") or "")


async def _load_chat_page_records(runtime: Any) -> list[dict]:
    history = getattr(runtime, "history", None)
    history_records = list(await history.records() or []) if history is not None else []
    timeline_records = _load_chat_timeline_records(runtime)
    archive = getattr(runtime, "archive", None)
    if archive is None:
        return _tag_record_order(
            _merge_chat_page_records(
                archive_records=[],
                timeline_records=timeline_records,
                history_records=history_records,
            )
        )

    try:
        archive_records = await _load_archive_records_paged(archive)
    except Exception as e:
        logger.warning(f"加载 archive 失败，仅显示实时聊天和运行时事件: {e}")
        archive_records = []
    return _tag_record_order(
        _merge_chat_page_records(
            archive_records=list(archive_records or []),
            timeline_records=timeline_records,
            history_records=history_records,
        )
    )


def _load_chat_timeline_records(runtime: Any) -> list[dict[str, Any]]:
    pipeline = getattr(runtime, "pipeline", None)
    timeline = getattr(pipeline, "chat_timeline", None)
    snapshot = getattr(timeline, "snapshot", None)
    if not callable(snapshot):
        return []
    try:
        conversations = snapshot()
    except Exception as e:
        logger.warning(f"加载实时聊天时间线失败: {e}")
        return []
    records: list[dict[str, Any]] = []
    if not isinstance(conversations, dict):
        return records
    for conversation_id, messages in conversations.items():
        if not isinstance(messages, list):
            continue
        for message in messages:
            records.append(_chat_timeline_message_to_record(str(conversation_id), message))
    return records


def _chat_timeline_message_to_record(conversation_id: str, message: Any) -> dict[str, Any]:
    direction = str(getattr(message, "direction", "") or "")
    text = str(getattr(message, "text", "") or "")
    raw_message = str(getattr(message, "raw_message", "") or "")
    content = text or raw_message
    timestamp = getattr(message, "timestamp", None)
    try:
        sort_ts = float(timestamp)
    except (TypeError, ValueError):
        sort_ts = None
    return {
        "role": "assistant" if direction == "outbound" else "user",
        "direction": direction,
        "content": content,
        "conversation_id": conversation_id,
        "timestamp": str(getattr(message, "time_text", "") or "") or None,
        "_sort_ts": sort_ts,
        "_source": "chat_timeline",
        "qq_visible": True,
        "sender_name": getattr(message, "sender_name", None),
        "sender_id": getattr(message, "sender_id", None),
        "target_id": getattr(message, "target_id", None),
        "group_id": getattr(message, "group_id", None),
        "msg_id": getattr(message, "msg_id", None),
        "raw_message": raw_message,
        "reply_to": getattr(message, "reply_to", None),
        "attachments": list(getattr(message, "attachments", []) or []),
        "cq_segments": list(getattr(message, "cq_segments", []) or []),
    }


def _merge_chat_page_records(
    *,
    archive_records: list[dict],
    timeline_records: list[dict],
    history_records: list[dict],
) -> list[dict]:
    timeline_real_ids = {
        identity
        for record in timeline_records
        if (identity := _real_record_identity(record)) is not None
    }
    archive_unique = [
        record
        for record in archive_records
        if _real_record_identity(record) not in timeline_real_ids
    ]
    return [
        *archive_unique,
        *timeline_records,
        *_history_runtime_event_records(history_records),
    ]


def _history_runtime_event_records(records: list[dict]) -> list[dict]:
    result: list[dict] = []
    for record in records:
        role = _record_role(record)
        content = str(record.get("content") or "")
        if role in {"system", "tool"} or _runtime_event_summary(content) is not None:
            result.append(record)
            continue
        if role == "assistant" and not _record_is_qq_visible_outbound(record):
            if content.strip() or record.get("tool_calls") or record.get("reasoning_content"):
                result.append(record)
    return result


def _tag_record_order(records: list[dict]) -> list[dict]:
    return [{**record, "_record_order": index} for index, record in enumerate(records)]


def _real_record_identity(record: dict[str, Any]) -> tuple[str, str, str] | None:
    msg_id = _record_message_id(record)
    if not msg_id:
        return None
    conversation_id = _record_conversation_id_for_display(record)
    if not conversation_id:
        return None
    direction = str(record.get("direction") or "")
    if not direction:
        direction = "outbound" if _record_role(record) == "assistant" else "inbound"
    return conversation_id, direction, msg_id


async def _load_archive_records_paged(archive: Any) -> list[dict]:
    filter_records = getattr(archive, "filter_records", None)
    if not callable(filter_records):
        return list(await archive.records() or [])

    records: list[dict] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        page = await filter_records(
            {
                "limit": ARCHIVE_FETCH_PAGE_SIZE,
                "offset": offset,
                "order": "asc",
            }
        )
        if not isinstance(page, dict):
            break
        raw_results = page.get("results") or []
        results = [item for item in raw_results if isinstance(item, dict)]
        if not results:
            break
        records.extend(await _full_archive_records_for_page(archive, results))
        offset += len(results)
        total_value = page.get("total")
        total = int(total_value) if isinstance(total_value, int | str) and str(total_value).isdigit() else offset
    return records


async def _full_archive_records_for_page(archive: Any, results: list[dict]) -> list[dict]:
    ids = [str(item.get("id") or "").strip() for item in results if item.get("id")]
    get_by_ids = getattr(archive, "get_by_ids", None)
    if ids and callable(get_by_ids):
        try:
            full_records = await get_by_ids(ids)
        except Exception as e:
            logger.warning(f"按归档 ID 还原完整记录失败，使用轻量记录: {e}")
        else:
            by_id = {
                str(item.get("archive_id") or item.get("id") or ""): item
                for item in full_records or []
                if isinstance(item, dict)
            }
            ordered = [by_id[archive_id] for archive_id in ids if archive_id in by_id]
            if len(ordered) == len(ids):
                return ordered
    return [_light_archive_result_to_record(item) for item in results]


def _light_archive_result_to_record(item: dict[str, Any]) -> dict[str, Any]:
    direction = str(item.get("direction") or "")
    role = "assistant" if direction == "outbound" else "user"
    conversation_id = item.get("conversation_id")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "role": role,
        "content": str(item.get("content") or ""),
        "conversation_id": str(conversation_id) if conversation_id else None,
        "archive_id": item.get("id"),
        "timestamp": item.get("time"),
        "direction": direction,
        "sender_id": item.get("sender_id"),
        "sender_name": item.get("sender_name"),
        "metadata": {
            **metadata,
            "direction": direction,
            "sender_id": item.get("sender_id"),
            "sender_name": item.get("sender_name"),
        },
    }


def _render_record_html(
    rec: dict,
    *,
    persona_name: str,
    show_chat: bool = True,
    show_system: bool = True,
    show_tools: bool = True,
) -> str:
    bubbles = _render_record_bubbles(
        rec,
        persona_name=persona_name,
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
    )
    return "".join(bubbles)


def _render_record_bubbles(
    rec: dict,
    *,
    persona_name: str,
    show_chat: bool = True,
    show_system: bool = True,
    show_tools: bool = True,
    attached_tool_results: dict[str, list[dict]] | None = None,
) -> list[str]:
    items = normalize_history_record(rec, persona_name=persona_name)
    if attached_tool_results:
        for item in items:
            if item.kind != "tool_call" or not item.related_tool_call_id:
                continue
            result_records = attached_tool_results.get(item.related_tool_call_id, [])
            for result_record in result_records:
                result_items = normalize_history_record(result_record, persona_name=persona_name)
                item.tool_results.extend(
                    result for result in result_items if result.kind == "tool_result"
                )
    visible = _filter_display_items(
        items,
        search_text="",
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
    )
    return [_render_display_item(item) for item in visible]

def _render_tool_call_bubble(
    tool_calls: list,
    *,
    persona_name: str,
    attached_tool_results: dict[str, list[dict]] | None = None,
) -> str:
    parts = []
    for index, tc in enumerate(tool_calls):
        display = format_tool_call(tc if isinstance(tc, dict) else {})
        toggle_id = f"legacy-tool-call:{index}"
        parts.append(
            _render_expandable_event_record(
                f"{persona_name} · {display.title}",
                display.detail,
                toggle_id=toggle_id,
                expanded=False,
                event_class="chat-event-tool",
            )
        )
        tool_call_id = str(tc.get("id") or "").strip() if isinstance(tc, dict) else ""
        for result in (attached_tool_results or {}).get(tool_call_id, []):
            result_item = DisplayItem(
                item_id=f"legacy-tool-result:{tool_call_id}",
                conversation_id="legacy",
                timestamp=None,
                kind="tool_result",
                speaker_label="工具返回",
                speaker_id=None,
                role_label="工具返回",
                text=str(result.get("content") or ""),
                summary="工具返回",
                raw=result,
                related_tool_call_id=tool_call_id,
            )
            parts.append(_render_tool_result_item(result_item, tool_name=display.tool_name))
    return "".join(parts)


def _render_outbound_bubble(item: dict[str, Any], *, persona_name: str) -> str:
    content = str(item.get("content") or item.get("label") or "").strip()
    if not content:
        content = "(空消息)"
    msg_id = str(item.get("msg_id") or "").strip()
    status = "已发送"
    if msg_id:
        status = f"{status} · msg_id={msg_id}"
    parts = [
        "<div class='chat-record chat-bubble chat-assistant'>",
        f"<div class='chat-head'>{_escape(persona_name)}"
        f" <span class='chat-meta'>{_escape(status)}</span></div>",
        _render_text_block(content),
        "</div>",
    ]
    return "".join(parts)


def _user_record_label(rec: dict) -> str:
    meta_messages = (rec.get("metadata") or {}).get("messages") or []
    first = meta_messages[0] if meta_messages else None
    if isinstance(first, dict):
        nickname = str(first.get("nickname") or "").strip()
        user_id = str(first.get("user_id") or first.get("target_id") or "").strip()
        if nickname and user_id:
            return f"{nickname}({user_id})"
        if nickname:
            return nickname
        if user_id:
            return f"用户 {user_id}"
    sender_name = str(rec.get("sender_name") or "").strip()
    sender_id = str(rec.get("sender_id") or "").strip()
    if sender_name and sender_id:
        return f"{sender_name}({sender_id})"
    if sender_name:
        return sender_name
    if sender_id:
        return f"用户 {sender_id}"
    meta = rec.get("metadata")
    if isinstance(meta, dict):
        sender_name = str(meta.get("sender_name") or "").strip()
        sender_id = str(meta.get("sender_id") or "").strip()
        if sender_name and sender_id:
            return f"{sender_name}({sender_id})"
        if sender_name:
            return sender_name
        if sender_id:
            return f"用户 {sender_id}"

    content = str(rec.get("content") or "")
    match = _LEGACY_HEADER_RE.match(content)
    if match:
        return f"{match.group('nickname')}({match.group('user_id')})"
    return "用户"


def _strip_legacy_header(content: str) -> str:
    match = _LEGACY_HEADER_RE.match(content)
    if not match:
        return content
    return content[match.end():].lstrip()


def _split_legacy_header_messages(content: str) -> list[dict[str, str]]:
    matches = list(_LEGACY_HEADER_LINE_RE.finditer(content))
    if not matches or matches[0].start() != 0:
        return []
    messages: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        text = content[match.end():next_start].strip()
        group_id = match.group("group_id") or ""
        user_id = match.group("user_id") or ""
        conversation_id = f"group:{group_id}" if group_id else f"private:{user_id}"
        messages.append(
            {
                "timestamp": match.group("timestamp") or "",
                "conversation_id": conversation_id,
                "speaker_label": f"{match.group('nickname')}({user_id})",
                "speaker_id": user_id,
                "message_id": match.group("message_id") or "",
                "text": text,
                "raw_header": match.group(0),
            }
        )
    return messages


def _runtime_event_summary(content: str) -> tuple[str, str] | None:
    if not content:
        return None
    if "<send_receipt_task" in content:
        return "发送回执任务", _compact_inline_tokens(_first_nonempty_line(content))
    if "<send_status" in content:
        return "发送状态", _format_send_status_summary(content)
    if "<send_receipt" in content:
        return "发送回执", _format_send_receipt_summary(content)
    if "<task_context" in content:
        return "运行时上下文", _format_task_context_summary(content)
    return None


def _format_send_status_summary(content: str) -> str:
    body = _extract_tag_text(content, "send_status") or content
    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and "不是用户新发言" not in line
    ]
    summary = lines[-1] if lines else _first_nonempty_line(body)
    return _compact_inline_tokens(summary) or "发送状态"


def _send_status_info(content: str) -> dict[str, Any] | None:
    if "<send_status" not in content and "send_id" not in content:
        return None
    body = _extract_tag_text(content, "send_status") or content
    payload = _parse_json_object(body)
    if payload:
        send_id = str(payload.get("send_id") or "").strip()
        msg_ids = payload.get("msg_ids") or payload.get("message_ids")
        if not isinstance(msg_ids, list):
            msg_ids = []
        parsed_msg_ids = [str(item).strip() for item in msg_ids if str(item).strip()]
        return {
            "send_id": send_id,
            "msg_ids": parsed_msg_ids,
            "completed": _send_status_payload_completed(payload, parsed_msg_ids),
        } if send_id else None

    send_id_match = re.search(r"\bsend_id=([^\s,;，。]+)", body)
    if not send_id_match:
        return None
    msg_ids: list[str] = []
    list_match = re.search(r"\bmsg_ids=\[([^\]]*)\]", body)
    if list_match:
        msg_ids = [
            part.strip().strip("'\"")
            for part in list_match.group(1).split(",")
            if part.strip().strip("'\"")
        ]
    else:
        single_match = re.search(r"\bmsg_id=([^\s,;，。]+)", body)
        if single_match:
            msg_ids = [single_match.group(1).strip().strip("'\"")]
    return {
        "send_id": send_id_match.group(1).strip(),
        "msg_ids": msg_ids,
        "completed": _send_status_text_completed(body, msg_ids),
    }


def _send_status_payload_completed(payload: dict[str, Any], msg_ids: list[str]) -> bool:
    status = str(payload.get("status") or payload.get("delivery") or "").strip().casefold()
    if status in {"sent", "done", "completed", "complete", "success", "succeeded"}:
        return True
    if status in {"pending", "queued", "accepted", "needs_review", "failed", "stale"}:
        return False
    if payload.get("ok") is True and msg_ids:
        return True
    return bool(msg_ids and status not in {"failed", "stale"})


def _send_status_text_completed(body: str, msg_ids: list[str]) -> bool:
    if not msg_ids:
        return False
    lowered = body.casefold()
    if any(token in lowered for token in ("失败", "过期", "stale", "failed", "pending", "等待")):
        return False
    return any(token in body for token in ("发送完成", "发送成功", "已发送", "投递完成")) or "sent" in lowered


def _format_send_receipt_summary(content: str) -> str:
    payload = _extract_tag_json(content, "send_receipt")
    if not payload:
        return _format_send_receipt_text_summary(content)
    status = payload.get("status")
    ok = payload.get("ok")
    sent = payload.get("sent") or []
    unsent = payload.get("unsent") or []
    attempted = payload.get("attempted_messages") or []
    new_messages = payload.get("new_messages") or payload.get("new_visible_messages") or []
    recalled = payload.get("recalled_messages") or []
    parts = []
    if status:
        parts.append(f"状态 {status}")
    elif ok is not None:
        parts.append("成功" if ok else "未完成")
    if sent:
        parts.append(f"已发送 {len(sent)} 条")
    if unsent:
        parts.append(f"未发送 {len(unsent)} 条")
    if attempted:
        parts.append(f"待发送/尝试 {len(attempted)} 条")
    accepted = payload.get("accepted") or payload.get("queued") or []
    if accepted:
        parts.append(f"排队/待确认 {len(accepted)} 条")
    if new_messages:
        parts.append(f"新消息 {len(new_messages)} 条")
    if recalled:
        parts.append(f"撤回 {len(recalled)} 条")
    delivery = payload.get("delivery")
    if delivery:
        parts.append(f"投递 {delivery}")
    if payload.get("qq_visible") == "pending":
        parts.append("QQ 可见性待确认")
    note = str(payload.get("note") or payload.get("next") or "").strip()
    if note:
        parts.append(_compact_inline_tokens(note))
    return "；".join(parts) if parts else "发送回执"


def _format_send_receipt_text_summary(content: str) -> str:
    body = _extract_tag_text(content, "send_receipt") or content
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    parts: list[str] = []
    for line in lines:
        match = re.match(r"^状态[:：]\s*(.+?)\s*$", line)
        if match:
            status = match.group(1).strip().rstrip("。.")
            if status:
                parts.append(f"状态 {status}")
            break
    for source, label in (
        ("已发送", "已发送"),
        ("未发送", "未发送"),
        ("新消息", "新消息"),
        ("撤回消息", "撤回"),
        ("错误", "错误"),
    ):
        count = _send_receipt_text_section_count(lines, source)
        if count > 0:
            parts.append(f"{label} {count} 条")
    if parts:
        return "；".join(parts)
    first = _first_nonempty_line(body)
    return _compact_inline_tokens(first) if first else "发送回执"


def _send_receipt_sent_items(content: str) -> list[dict[str, Any]]:
    payload = _extract_tag_json(content, "send_receipt")
    if not payload:
        return _send_receipt_text_sent_items(content)
    sent = payload.get("sent")
    if not isinstance(sent, list):
        return []
    send_id = str(payload.get("send_id") or "").strip()
    result: list[dict[str, Any]] = []
    for order, item in enumerate(sent):
        if not isinstance(item, dict) or item.get("qq_visible") is False:
            continue
        enriched = {
            **item,
            "send_order": item.get("send_order", order),
            "_synthetic_source": "send_receipt",
        }
        if send_id and not enriched.get("send_id"):
            enriched["send_id"] = send_id
        result.append(enriched)
    return result


def _send_receipt_text_sent_items(content: str) -> list[dict[str, Any]]:
    body = _extract_tag_text(content, "send_receipt")
    if not body:
        return []
    try:
        return _parse_send_receipt_text_sent_items(body)
    except (AttributeError, TypeError, ValueError):
        return []


def _parse_send_receipt_text_sent_items(body: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    send_id = _send_receipt_text_send_id(lines)
    start = -1
    declared_count = 0
    for index, line in enumerate(lines):
        count = _send_receipt_text_section_count([line], "已发送")
        if count >= 0:
            start = index + 1
            declared_count = count
            break
    if start < 0 or declared_count <= 0:
        return []

    result: list[dict[str, Any]] = []
    for order, line in enumerate(_send_receipt_text_section_lines(lines, start)):
        match = re.match(r"^\s*\d+[\.\)、]\s*(.+?)\s*$", line)
        if not match:
            continue
        item = _parse_send_receipt_text_sent_line(
            match.group(1),
            fallback_send_id=send_id,
            fallback_order=order,
        )
        if item:
            result.append(item)
    return result


def _send_receipt_text_send_id(lines: list[str]) -> str:
    for line in lines:
        match = re.match(r"^发送回执[:：]\s*(\S+)\s*$", line)
        if match:
            value = match.group(1).strip()
            return "" if value == "无" else value
    return ""


def _send_receipt_text_section_count(lines: list[str], title: str) -> int:
    pattern = rf"^{re.escape(title)}\s*(\d+)\s*条[:：]?\s*$"
    for line in lines:
        match = re.match(pattern, line)
        if match:
            return int(match.group(1))
    return -1


def _send_receipt_text_section_lines(lines: list[str], start: int) -> list[str]:
    result: list[str] = []
    for line in lines[start:]:
        if _send_receipt_text_is_section_heading(line) or line.startswith("处理要求"):
            break
        result.append(line)
    return result


def _send_receipt_text_is_section_heading(line: str) -> bool:
    return bool(
        re.match(
            r"^(已发送|未发送|新消息|撤回消息|错误|attempted|accepted|待发送/尝试|排队/待确认|已接受消息)\s*\d+\s*条[:：]?\s*$",
            line,
        )
    )


def _parse_send_receipt_text_sent_line(
    text: str,
    *,
    fallback_send_id: str,
    fallback_order: int,
) -> dict[str, Any] | None:
    parts = [part.strip() for part in re.split(r"[；;](?=[A-Za-z_][\w-]*=)", text) if part.strip()]
    if not parts:
        return None
    item: dict[str, Any] = {}
    content_parts: list[str] = []
    for part in parts:
        match = re.match(r"^([A-Za-z_][\w-]*)=(.*)$", part)
        if not match:
            content_parts.append(part)
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if value and value != "无":
            item[key] = _send_receipt_text_field_value(key, value)
    content = str(item.get("content") or "；".join(content_parts)).strip()
    if not content:
        return None
    if item.get("qq_visible") is False:
        return None
    item["content"] = content
    item.setdefault("qq_visible", True)
    item.setdefault("send_order", item.get("order", fallback_order))
    if fallback_send_id:
        item.setdefault("send_id", fallback_send_id)
    item["_synthetic_source"] = "send_receipt"
    return item


def _send_receipt_text_field_value(key: str, value: str) -> Any:
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if key in {"order", "send_order", "message_index", "index"} and value.isdigit():
        return int(value)
    return value


def _format_task_context_summary(content: str) -> str:
    parts = ["本轮系统上下文"]
    conv_match = re.search(r"当前会话：([^。\n]+)", content)
    if conv_match:
        parts.append(f"当前会话 {conv_match.group(1).strip()}")
    recent_count = len(re.findall(r"<recent_group_messages\b", content))
    if recent_count:
        parts.append("包含最近群聊窗口")
    if "可用表情" in content:
        parts.append("包含可用表情提示")
    return "；".join(parts)


def _extract_tag_json(content: str, tag: str) -> dict[str, Any] | None:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", content, re.S)
    if not match:
        return None
    raw = match.group(1).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        json_start = raw.find("{")
        json_end = raw.rfind("}")
        if json_start < 0 or json_end <= json_start:
            return None
        try:
            value = json.loads(raw[json_start:json_end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _extract_tag_text(content: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}\b[^>]*>\s*(.*?)\s*</{tag}>", content, re.S)
    if not match:
        return None
    return match.group(1).strip()


def _render_tool_result_content(
    content: str,
    *,
    toggle_id: str | None = None,
    expanded: bool = False,
    tool_name: str | None = None,
) -> str:
    display = format_tool_result(content, tool_name=tool_name)
    if toggle_id:
        return _render_expandable_detail(
            display.detail,
            toggle_id=toggle_id,
            expanded=expanded,
            collapsed_label="点击展开",
        )
    return f"<pre class='chat-pre'>{_escape(display.detail)}</pre>"


def _format_tool_result_summary(content: str) -> str:
    return format_tool_result(content).summary


def _format_message_list_summary(value: Any, *, head: str, noun: str) -> str:
    if not isinstance(value, list) or not value:
        return ""
    details: list[str] = []
    times = [
        str(item.get("time") or item.get("timestamp") or item.get("created_at") or "").strip()
        for item in value
        if isinstance(item, dict)
    ]
    times = [item for item in times if item]
    if times:
        details.append(times[0] if times[0] == times[-1] else f"{times[0]}-{times[-1]}")
    sources = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(
            item.get("conversation_id")
            or item.get("source")
            or item.get("group_id")
            or item.get("target_id")
            or ""
        ).strip()
        if source and source not in sources:
            sources.append(source)
    if sources:
        shown = "、".join(sources[:2])
        if len(sources) > 2:
            shown = f"{shown} 等 {len(sources)} 个来源"
        details.append(f"来源 {shown}")
    sample = _message_list_sample(value)
    if sample:
        details.append(f"样例：{sample}")
    suffix = f"（{'；'.join(details)}）" if details else ""
    return f"{head} {len(value)} 条{noun}{suffix}"


def _message_list_sample(value: list[Any]) -> str:
    first = next((item for item in value if isinstance(item, dict)), None)
    if first is None:
        return ""
    sender = str(
        first.get("sender_name")
        or first.get("nickname")
        or first.get("user_id")
        or first.get("sender_id")
        or ""
    ).strip()
    text = str(
        first.get("text")
        or first.get("content")
        or first.get("message")
        or first.get("raw_message")
        or ""
    ).strip()
    text = _short_text(_compact_inline_tokens(text), limit=48)
    if sender and text:
        return f"{sender}: {text}"
    return sender or text


def _render_collapsed_content(
    summary: str,
    content: str,
    *,
    toggle_id: str | None = None,
    expanded: bool = False,
    expand_label: str = "展开原文",
) -> str:
    parts = [f"<div class='chat-summary'>{_escape(summary)}</div>"]
    parts.append(
        _render_collapsible_raw(
            content,
            toggle_id=toggle_id,
            expanded=expanded,
            expand_label=expand_label,
        )
    )
    return "".join(parts)


def _render_collapsible_raw(
    content: str,
    *,
    toggle_id: str | None,
    expanded: bool,
    expand_label: str,
) -> str:
    if not content:
        return ""
    if toggle_id:
        label = "收起" if expanded else expand_label
        parts = [
            "<div class='chat-summary'>"
            f"<a class='chat-toggle' href='{_escape_attr(_toggle_href(toggle_id))}'>{_escape(label)}</a>"
            "</div>"
        ]
        if expanded:
            parts.append(f"<pre class='chat-pre'>{_escape(content)}</pre>")
        return "".join(parts)
    return (
        f"<details><summary class='chat-summary'>{_escape(expand_label)}</summary>"
        f"<pre class='chat-pre'>{_escape(content)}</pre></details>"
    )


def _render_text_block(
    content: str,
    *,
    toggle_id: str | None = None,
    expanded: bool = False,
) -> str:
    if not content:
        return ""
    compact = _compact_inline_tokens(content)
    should_collapse = compact != content or len(content) > COMPACT_TEXT_LIMIT
    preview = compact
    if len(preview) > COMPACT_TEXT_LIMIT:
        preview = f"{preview[:COMPACT_TEXT_LIMIT]}..."
    if should_collapse and expanded:
        return (
            _render_collapsible_raw(
                content,
                toggle_id=toggle_id,
                expanded=True,
                expand_label="展开原文",
            )
        )
    parts = [f"<pre class='chat-pre'>{_escape(preview)}</pre>"]
    if should_collapse:
        parts.append(
            _render_collapsible_raw(
                content,
                toggle_id=toggle_id,
                expanded=False,
                expand_label="展开原文",
            )
        )
    return "".join(parts)


def _compact_inline_tokens(content: str) -> str:
    json_compact = _compact_json_blob(content)
    if json_compact != content:
        return json_compact
    compact = _URL_RE.sub(lambda m: _compact_url(m.group(0)), content)
    compact = _WINDOWS_PATH_RE.sub(lambda m: _compact_path(m.group(0)), compact)
    compact = _WORKSPACE_PATH_RE.sub(lambda m: _compact_workspace_path(m.group(0)), compact)
    return compact


def _compact_json_blob(content: str) -> str:
    stripped = content.strip()
    if len(stripped) <= INLINE_PREVIEW_LIMIT:
        return content
    if not (
        (stripped.startswith("{") and stripped.endswith("}"))
        or (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return content
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return content
    kind = "JSON对象" if isinstance(value, dict) else "JSON数组"
    if isinstance(value, dict):
        preview_keys = "、".join(str(key) for key in list(value)[:4])
        suffix = f" · {preview_keys}" if preview_keys else ""
    elif isinstance(value, list):
        suffix = f" · {len(value)} 项"
    else:
        suffix = ""
    return f"[{kind}{suffix} · {len(stripped)}字，已折叠]"


def _compact_url(value: str) -> str:
    if len(value) <= INLINE_PREVIEW_LIMIT:
        return value
    parsed = urlsplit(value)
    host = parsed.netloc or "url"
    tail = parsed.path.rsplit("/", 1)[-1] or parsed.path.strip("/") or "link"
    tail = tail[:24] + "..." if len(tail) > 27 else tail
    return f"[URL {host}/{tail} · {len(value)}字]"


def _compact_path(value: str) -> str:
    if len(value) <= INLINE_PREVIEW_LIMIT:
        return value
    tail = value.replace("/", "\\").rsplit("\\", 1)[-1] or "path"
    return f"[路径 {tail} · {len(value)}字]"


def _compact_workspace_path(value: str) -> str:
    if len(value) <= INLINE_PREVIEW_LIMIT:
        return value
    cleaned = value.removeprefix("workspace=")
    tail = cleaned.replace("\\", "/").rsplit("/", 1)[-1] or "workspace"
    return f"[workspace {tail} · {len(value)}字]"


def _first_nonempty_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _format_tool_call_for_display(tool_call: dict) -> str:
    return format_tool_call(tool_call).summary


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _format_private_send_args(args: dict[str, Any]) -> str:
    targets = args.get("targets")
    if not isinstance(targets, list):
        targets = []
    lines = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        qq = target.get("target_qq") or target.get("target_id") or "默认私聊"
        lines.append(f"向 {qq} 发送消息：{_message_with_delay(target)}")
    return "；".join(lines) or "发送私聊消息"


def _format_group_send_args(args: dict[str, Any]) -> str:
    group_id = args.get("group_id") or args.get("target_id") or "默认群"
    targets = args.get("targets")
    if not isinstance(targets, list):
        targets = []
    messages = [
        _message_with_delay(target)
        for target in targets
        if isinstance(target, dict)
    ]
    joined = "；".join(messages) if messages else "(空消息)"
    return f"在群 {group_id} 发送消息：{joined}"


def _message_with_delay(target: dict[str, Any]) -> str:
    content = str(target.get("content") or "")
    delay = target.get("delay")
    if delay in (None, 0, 0.0, ""):
        return content
    return f"{content}（{delay}s）"


def _format_commit_send_attempt_args(args: dict[str, Any]) -> str:
    parts = []
    attempt_id = str(args.get("send_attempt_id") or "").strip()
    if attempt_id:
        parts.append(f"send_attempt_id={attempt_id}")
    if args.get("reviewed_until_seq") is not None:
        parts.append(f"已复核到 seq {args.get('reviewed_until_seq')}")
    policy = str(args.get("delivery_interrupt_policy") or "").strip()
    if policy:
        parts.append(f"中断策略 {policy}")
    reply_to = str(args.get("reply_to_message_id") or "").strip()
    if reply_to:
        parts.append(f"引用 msg_id={reply_to}")
    if args.get("ignore_review_interrupts") is True:
        parts.append("忽略复核打断")
    reason = str(args.get("reason") or "").strip()
    if reason:
        parts.append(f"原因：{_short_text(_compact_inline_tokens(reason), limit=60)}")
    detail = "；".join(parts) if parts else "无参数"
    return f"提交发送尝试：{detail}"


def _format_generic_tool_args(name: str, args: dict[str, Any]) -> str:
    if not args:
        return f"{name}：无参数"
    parts = []
    for key, value in args.items():
        parts.append(f"{key}={_format_tool_arg_value(value)}")
        if len(parts) >= 5:
            break
    if len(args) > len(parts):
        parts.append(f"另有 {len(args) - len(parts)} 项")
    return f"{name}：{'；'.join(parts)}"


def _format_tool_arg_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        sample = _message_list_sample(value)
        suffix = f"，样例：{sample}" if sample else ""
        return f"{len(value)} 项{suffix}"
    if isinstance(value, dict):
        keys = "、".join(str(key) for key in list(value)[:4])
        return f"对象({keys})" if keys else "对象"
    return _short_text(_compact_inline_tokens(str(value)), limit=80)


def _format_upload_args(args: dict[str, Any]) -> str:
    target_type = args.get("target_type") or "-"
    target_id = args.get("target_id") or "-"
    path = args.get("file_path") or args.get("path") or "-"
    return f"上传文件到 {target_type}:{target_id}：{path}"


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attr(s: str) -> str:
    return _escape(s).replace('"', "&quot;").replace("'", "&#x27;")


def _short_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def _scrollbar_near_bottom(bar: Any, threshold: int = 24) -> bool:
    return bar.maximum() - bar.value() <= threshold
