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
from dataclasses import dataclass, field
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
        if item_id in self._expanded_item_ids:
            self._expanded_item_ids.remove(item_id)
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
            parts.append(_render_display_item(item, expanded_item_ids=self._expanded_item_ids))
            parts.append("<hr/>")
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
        event_bg = "#292522"
        event_tool_bg = "#202832"
        result_bg = "#1D252C"
    else:
        bot_bg = "#F1E8D6"
        peer_bg = "#E8F1ED"
        event_bg = "#F7F3EA"
        event_tool_bg = "#F2F6FA"
        result_bg = "#F8FBFD"
    return (
        "<style>"
        ".chat-record{margin:10px 0;}"
        ".chat-row{width:100%;}"
        ".chat-bubble{display:inline-block;max-width:78%;text-align:left;"
        "padding:10px 12px;border-radius:8px;"
        f"border:1px solid {palette.border};color:{palette.text_primary};"
        "overflow-wrap:anywhere;word-break:break-word;}"
        f".chat-bot .chat-bubble{{background:{bot_bg};}}"
        f".chat-peer .chat-bubble{{background:{peer_bg};}}"
        ".chat-left{margin-right:16%;}"
        ".chat-right{margin-left:16%;}"
        ".chat-left{text-align:left;}"
        ".chat-right{text-align:right;}"
        ".chat-right .chat-head{text-align:right;}"
        ".chat-event{padding:8px 10px;border-left:3px solid;"
        f"color:{palette.text_primary};background:{event_bg};"
        f"border-color:{palette.border};"
        "overflow-wrap:anywhere;word-break:break-word;}"
        f".chat-event-tool{{border-color:{palette.accent_blue};background:{event_tool_bg};}}"
        f".chat-event-system{{border-color:{palette.border};background:{event_bg};}}"
        f".chat-event-reasoning{{border-color:{palette.accent_primary};background:{event_bg};}}"
        ".chat-head{font-weight:600;margin-bottom:6px;}"
        f".chat-meta{{color:{palette.text_secondary};font-size:12px;}}"
        ".chat-pre{white-space:pre-wrap;margin:6px 0 0 0;"
        "overflow-wrap:anywhere;word-break:break-word;}"
        f".chat-summary{{color:{palette.text_secondary};}}"
        f".chat-toggle{{color:{palette.accent_blue};text-decoration:none;}}"
        ".chat-tool-list{margin:6px 0 0 18px;}"
        ".chat-tool-result{margin:8px 0 0 0;padding:8px 10px;border-left:3px solid;"
        f"border-color:{palette.accent_blue};background:{result_bg};"
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
        explicit_cid = rec.get("conversation_id")
        if isinstance(explicit_cid, str) and explicit_cid:
            info = _conversation_info_from_id(explicit_cid)
            current_key = info["key"] if rec.get("role") == "user" else current_key
            conv = _ensure(info["key"], info["label"])
        elif rec.get("role") == "user":
            info = _conversation_info(rec)
            current_key = info["key"]
            conv = _ensure(info["key"], info["label"])
        elif rec.get("role") in {"system", "tool"}:
            conv = _ensure("system:global", "系统记录")
        else:
            if current_key is None:
                conv = _ensure("unknown:history", "未标记来源")
            else:
                conv = _ensure(
                    current_key,
                    "系统记录" if current_key.startswith("system:") else current_key,
                )
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
    role = str(record.get("role") or "")
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
    for record_index, record in enumerate(records):
        for item in normalize_history_record(
            record,
            persona_name=persona_name,
            bot_user_id=bot_user_id,
        ):
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
    return items


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
            msg_id = str(sent.get("msg_id") or "").strip() or None
            items.append(
                DisplayItem(
                    item_id=f"{base_id}:sent:{index}",
                    conversation_id=str(sent.get("conversation_id") or conversation_id),
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
        return [
            DisplayItem(
                item_id=f"{base_id}:inbound",
                conversation_id=conversation_id,
                timestamp=timestamp,
                kind="inbound_message",
                speaker_label=_user_record_label(rec),
                speaker_id=_user_record_sender_id(rec),
                role_label="成员",
                text=_strip_legacy_header(content),
                summary=_compact_inline_tokens(_first_nonempty_line(_strip_legacy_header(content))),
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
            items.append(
                DisplayItem(
                    item_id=f"{base_id}:tool_call:{index}",
                    conversation_id=conversation_id,
                    timestamp=timestamp,
                    kind="tool_call",
                    speaker_label=persona_name,
                    speaker_id=bot_user_id,
                    role_label="工具动作",
                    text=json.dumps(tool_call, ensure_ascii=False, indent=2),
                    summary=_format_tool_call_for_display(tool_call),
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
            css="chat-peer chat-right",
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
    if item.kind == "outbound_message":
        return _render_chat_message_item(
            item,
            css="chat-bot chat-left",
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
    if item.kind == "tool_call":
        return _render_tool_call_item(item, expanded_item_ids=expanded_item_ids)
    if item.kind == "tool_result":
        head = f"工具结果 · {item.related_tool_call_id}" if item.related_tool_call_id else "工具结果"
        return _render_event_record(
            head,
            _render_tool_result_content(
                item.text,
                toggle_id=expand_id,
                expanded=expand_id in expanded_item_ids,
            ),
            event_class="chat-event-tool",
        )
    if item.kind == "runtime_receipt":
        title, summary = _split_display_summary(item.summary)
        head = f"系统 · {title}"
        body = _render_collapsed_content(
            summary,
            item.text,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
        return _render_event_record(head, body, event_class="chat-event-system")
    if item.kind == "assistant_note":
        head = f"{item.speaker_label or '助手'} · 内部文本"
        body = _render_collapsed_content(
            item.summary,
            item.text,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
        return _render_event_record(head, body, event_class="chat-event-system")
    if item.kind == "reasoning":
        head = f"{item.speaker_label or '助手'} · 思考过程"
        body = _render_collapsed_content(
            item.summary,
            item.text,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
        return _render_event_record(head, body, event_class="chat-event-reasoning")
    if item.kind == "system_event" and item.speaker_label == "系统" and " · " in item.summary:
        title, summary = _split_display_summary(item.summary)
        head = f"系统 · {title}"
        body = _render_collapsed_content(
            summary,
            item.text,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
        )
        return _render_event_record(head, body, event_class="chat-event-system")
    head = "系统"
    body = _render_collapsed_content(
        item.summary,
        item.text,
        toggle_id=expand_id,
        expanded=expand_id in expanded_item_ids,
    )
    return _render_event_record(head, body, event_class="chat-event-system")


def _render_chat_message_item(
    item: DisplayItem,
    *,
    css: str,
    toggle_id: str,
    expanded: bool,
) -> str:
    label = item.speaker_label or item.role_label
    meta = _chat_item_meta(item)
    parts = [
        f"<div class='chat-record chat-row {css}'>",
        "<div class='chat-bubble'>",
        f"<div class='chat-head'>{_escape(label)}",
    ]
    if meta:
        parts.append(f" <span class='chat-meta'>{_escape(meta)}</span>")
    parts.extend(
        [
            "</div>",
            _render_text_block(item.text or "(空消息)", toggle_id=toggle_id, expanded=expanded),
            "</div></div>",
        ]
    )
    return "".join(parts)


def _render_tool_call_item(
    item: DisplayItem,
    *,
    expanded_item_ids: set[str] | None = None,
) -> str:
    expanded_item_ids = expanded_item_ids or set()
    expand_id = _display_item_expand_id(item)
    tool_call = item.raw.get("tool_call") if isinstance(item.raw, dict) else None
    tool_name = _tool_call_name(tool_call if isinstance(tool_call, dict) else {})
    if tool_name == "no_action":
        head = f"{item.speaker_label or '助手'} · 工具动作"
        body = _render_collapsed_content(
            "选择不发送消息",
            item.text,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            expand_label="展开原始参数",
        )
        return _render_event_record(head, body, event_class="chat-event-tool")

    parts = [
        "<div class='chat-record chat-event chat-event-tool'>",
        f"<div class='chat-head'>{_escape(item.speaker_label or '助手')} · 工具动作</div>",
        f"<div class='chat-summary'>调用工具：{_escape(tool_name or '未知工具')}</div>",
        f"<div>{_escape(item.summary)}</div>",
    ]
    if item.tool_results:
        for result in item.tool_results:
            result_id = result.related_tool_call_id or item.related_tool_call_id or ""
            result_expand_id = _display_item_expand_id(result)
            result_body = _render_tool_result_content(
                result.text,
                toggle_id=result_expand_id,
                expanded=result_expand_id in expanded_item_ids,
            )
            parts.append(
                "<div class='chat-tool-result'>"
                f"<div class='chat-head'>工具结果 · {_escape(result_id)}</div>"
                f"{result_body}"
                "</div>"
            )
    parts.append(
        _render_collapsible_raw(
            item.text,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            expand_label="展开原始参数",
        )
    )
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
    elif item.related_message_id:
        parts.append(f"msg_id={item.related_message_id}")
    return " · ".join(parts)


def _record_role(rec: dict[str, Any]) -> str:
    role = str(rec.get("role") or "").strip()
    if role:
        return role
    direction = str(rec.get("direction") or "").strip()
    if direction == "outbound":
        return "assistant"
    if direction == "inbound":
        return "user"
    if str(rec.get("conversation_id") or "").startswith("system:"):
        return "system"
    return "user"


def _record_conversation_id_for_display(rec: dict[str, Any]) -> str:
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
    if history is None:
        return []

    history_records = await history.records()
    archive = getattr(runtime, "archive", None)
    if archive is None:
        return list(history_records or [])

    try:
        archive_records = await _load_archive_records_paged(archive)
    except Exception as e:
        logger.warning(f"加载 archive 失败，仅显示活跃 history: {e}")
        return list(history_records or [])
    return [*list(archive_records or []), *list(history_records or [])]


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


def _render_event_record(head: str, body: str, *, event_class: str) -> str:
    parts = [
        f"<div class='chat-record chat-event {event_class}'>",
        f"<div class='chat-head'>{_escape(head)}</div>",
    ]
    if body:
        parts.append(body)
    parts.append("</div>")
    return "".join(parts)


def _render_tool_call_bubble(
    tool_calls: list,
    *,
    persona_name: str,
    attached_tool_results: dict[str, list[dict]] | None = None,
) -> str:
    parts = [
        "<div class='chat-record chat-bubble chat-tool'>",
        f"<div class='chat-head'>{_escape(persona_name)} · 工具调用</div>",
        "<ul class='chat-tool-list'>",
    ]
    for tc in tool_calls:
        label = _format_tool_call_for_display(tc)
        tool_call_id = str(tc.get("id") or "").strip() if isinstance(tc, dict) else ""
        parts.append(f"<li>{_escape(label)}")
        for result in (attached_tool_results or {}).get(tool_call_id, []):
            result_id = str(result.get("tool_call_id") or tool_call_id)
            parts.append(
                "<div class='chat-tool-result'>"
                f"<div class='chat-head'>工具结果 · {_escape(result_id)}</div>"
                f"{_render_tool_result_content(str(result.get('content') or ''))}"
                "</div>"
            )
        parts.append("</li>")
    parts.append("</ul>")
    parts.append(
        "<details><summary class='chat-summary'>展开原始工具参数</summary>"
        f"<pre class='chat-pre'>{_escape(json.dumps(tool_calls, ensure_ascii=False, indent=2))}</pre>"
        "</details>"
    )
    parts.append("</div>")
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


def _format_send_receipt_summary(content: str) -> str:
    payload = _extract_tag_json(content, "send_receipt")
    if not payload:
        return _compact_inline_tokens(_first_nonempty_line(content))
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


def _send_receipt_sent_items(content: str) -> list[dict[str, Any]]:
    payload = _extract_tag_json(content, "send_receipt")
    if not payload:
        return []
    sent = payload.get("sent")
    if not isinstance(sent, list):
        return []
    return [item for item in sent if isinstance(item, dict) and item.get("qq_visible") is not False]


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
) -> str:
    summary = _format_tool_result_summary(content)
    return _render_collapsed_content(
        summary,
        content,
        toggle_id=toggle_id,
        expanded=expanded,
    )


def _format_tool_result_summary(content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return _compact_inline_tokens(_first_nonempty_line(content))
    if not isinstance(payload, dict):
        return _compact_inline_tokens(str(payload))

    parts = []
    status = payload.get("status")
    if status:
        parts.append(f"状态 {status}")
    elif "ok" in payload:
        parts.append("成功" if payload.get("ok") else "失败")
    if payload.get("send_id"):
        parts.append(f"send_id={payload.get('send_id')}")
    if payload.get("send_attempt_id"):
        parts.append(f"send_attempt_id={payload.get('send_attempt_id')}")
    sent = payload.get("sent")
    if isinstance(sent, list):
        parts.append(f"已发送 {len(sent)} 条")
    attempted = payload.get("attempted_messages")
    if isinstance(attempted, list):
        parts.append(f"尝试 {len(attempted)} 条")
    accepted = payload.get("accepted") or payload.get("queued")
    if isinstance(accepted, list):
        parts.append(f"排队/待确认 {len(accepted)} 条")
    new_messages = payload.get("new_messages") or payload.get("new_visible_messages")
    if isinstance(new_messages, list):
        parts.append(f"新消息 {len(new_messages)} 条")
    delivery = payload.get("delivery")
    if delivery:
        parts.append(f"投递 {delivery}")
    if payload.get("qq_visible") == "pending":
        parts.append("QQ 可见性待确认")
    elif payload.get("qq_visible") is False:
        parts.append("未 QQ 可见")
    if payload.get("ignored_review_interrupts") is True:
        parts.append("已忽略复核打断")
    forced = _format_message_list_summary(
        payload.get("forced_unseen_messages"),
        head="已忽略",
        noun="新打断",
    )
    if forced:
        parts.append(forced)
    unseen = _format_message_list_summary(
        payload.get("unseen_messages"),
        head="发现",
        noun="新打断",
    )
    if unseen:
        parts.append(unseen)
    priority = _format_message_list_summary(
        payload.get("priority_interrupts"),
        head="发现",
        noun="高优先级打断",
    )
    if priority:
        parts.append(priority)
    if payload.get("latest_seq") is not None:
        parts.append(f"latest_seq={payload.get('latest_seq')}")
    if payload.get("attempt_revision") is not None:
        parts.append(f"revision={payload.get('attempt_revision')}")
    next_hint = str(payload.get("next") or payload.get("note") or "").strip()
    if next_hint:
        parts.append(_short_text(_compact_inline_tokens(next_hint), limit=96))
    return "；".join(parts) if parts else _compact_inline_tokens(content)


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
    func = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(func, dict):
        return "未知工具调用"
    name = str(func.get("name") or "?")
    args = _parse_tool_arguments(func.get("arguments"))
    if name == "send_private_messages":
        return _format_private_send_args(args)
    if name == "send_group_message":
        return _format_group_send_args(args)
    if name == "commit_send_attempt":
        return _format_commit_send_attempt_args(args)
    if name == "no_action":
        return "不发送消息"
    if name == "upload_file":
        return _format_upload_args(args)
    if name == "get_forward_msg":
        return f"读取合并转发：{args.get('forward_id', '-')}"
    if name == "get_recent_chat_messages":
        target = args.get("conversation_id") or "当前会话"
        return f"读取最近聊天记录：{target}，{args.get('limit', 50)} 条"
    if name == "recall_history":
        target = args.get("conversation_id") or "全部会话"
        return f"检索历史记录：{target}，关键词={args.get('keyword') or '-'}"
    if name == "read_file":
        return f"读取文件：{args.get('path', '-')}"
    if name == "write_file":
        return f"写入文件：{args.get('path', '-')}"
    if name == "edit_file":
        return f"编辑文件：{args.get('path', '-')}"
    if name == "list_files":
        return f"列出文件：{args.get('path', '.')} / {args.get('pattern', '*')}"
    if name == "run_python":
        code = str(args.get("code") or "").strip().replace("\n", " ")
        return f"运行 Python：{code[:80]}{'...' if len(code) > 80 else ''}"
    return _format_generic_tool_args(name, args)


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
