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
import time
import weakref
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote, unquote

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
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
from .chats.display_items import (
    _CQ_MEDIA_RE as _CQ_MEDIA_RE,
)
from .chats.display_items import (
    _MEDIA_OR_FILE_EXT_RE as _MEDIA_OR_FILE_EXT_RE,
)
from .chats.display_items import (
    _MEDIA_TOOL_NAMES as _MEDIA_TOOL_NAMES,
)
from .chats.display_items import (
    CHAT_SORT_LAYER_RANK as CHAT_SORT_LAYER_RANK,
)
from .chats.display_items import (
    _accepted_message_conversation_id as _accepted_message_conversation_id,
)
from .chats.display_items import (
    _display_item_categories as _display_item_categories,
)
from .chats.display_items import (
    _display_item_has_media_or_file as _display_item_has_media_or_file,
)
from .chats.display_items import (
    _display_item_matches_filters as _display_item_matches_filters,
)
from .chats.display_items import (
    _display_item_record as _display_item_record,
)
from .chats.display_items import (
    _display_item_record_order as _display_item_record_order,
)
from .chats.display_items import (
    _display_item_search_text as _display_item_search_text,
)
from .chats.display_items import (
    _display_item_sort_key as _display_item_sort_key,
)
from .chats.display_items import (
    _display_item_sort_ts as _display_item_sort_ts,
)
from .chats.display_items import (
    _filter_display_items as _filter_display_items,
)
from .chats.display_items import (
    _is_generated_outbound as _is_generated_outbound,
)
from .chats.display_items import (
    _parse_float_value as _parse_float_value,
)
from .chats.display_items import (
    _parse_timestamp_value as _parse_timestamp_value,
)
from .chats.display_items import (
    _raw_send_order as _raw_send_order,
)
from .chats.display_items import (
    _record_has_media_or_file as _record_has_media_or_file,
)
from .chats.display_items import (
    _record_send_id as _record_send_id,
)
from .chats.display_items import (
    _record_send_order as _record_send_order,
)
from .chats.display_items import (
    _record_sort_layer as _record_sort_layer,
)
from .chats.display_items import (
    _record_sort_value_for_layer as _record_sort_value_for_layer,
)
from .chats.display_items import (
    _record_timestamp_sort_value as _record_timestamp_sort_value,
)
from .chats.display_items import (
    _remember_generated_outbound as _remember_generated_outbound,
)
from .chats.display_items import (
    _should_attach_tool_result_to_call as _should_attach_tool_result_to_call,
)
from .chats.display_items import (
    _should_skip_generated_outbound as _should_skip_generated_outbound,
)
from .chats.display_items import (
    _sort_display_items as _sort_display_items,
)
from .chats.display_items import (
    _text_has_media_or_file as _text_has_media_or_file,
)
from .chats.display_items import (
    _unique_item_id as _unique_item_id,
)
from .chats.grouping import (
    _LEGACY_HEADER_LINE_RE as _LEGACY_HEADER_LINE_RE,
)
from .chats.grouping import (
    _LEGACY_HEADER_RE as _LEGACY_HEADER_RE,
)
from .chats.grouping import (
    _accepted_messages_for_send_id as _accepted_messages_for_send_id,
)
from .chats.grouping import (
    _conversation_info as _conversation_info,
)
from .chats.grouping import (
    _conversation_info_from_id as _conversation_info_from_id,
)
from .chats.grouping import (
    _conversation_infos_from_payload as _conversation_infos_from_payload,
)
from .chats.grouping import (
    _conversation_list_signature as _conversation_list_signature,
)
from .chats.grouping import (
    _group_records_by_conversation as _group_records_by_conversation,
)
from .chats.grouping import (
    _group_target_info as _group_target_info,
)
from .chats.grouping import (
    _private_target_info as _private_target_info,
)
from .chats.grouping import (
    _record_cache_blob as _record_cache_blob,
)
from .chats.grouping import (
    _record_role as _record_role,
)
from .chats.grouping import (
    _records_cache_signature as _records_cache_signature,
)
from .chats.grouping import (
    _remember_send_id_targets as _remember_send_id_targets,
)
from .chats.grouping import (
    _send_receipt_sent_fallback_records as _send_receipt_sent_fallback_records,
)
from .chats.grouping import (
    _system_conversation_label as _system_conversation_label,
)
from .chats.grouping import (
    _targeted_tool_calls_for_record as _targeted_tool_calls_for_record,
)
from .chats.grouping import (
    _tool_call_target_infos as _tool_call_target_infos,
)
from .chats.grouping import (
    _tool_result_target_infos as _tool_result_target_infos,
)
from .chats.grouping import (
    _unique_conversation_infos as _unique_conversation_infos,
)
from .chats.models import (
    ConversationDisplayCache,
    DisplayItem,
    DisplaySeverity,
    SendDisplayContext,
)
from .chats.models import (
    DisplayKind as DisplayKind,
)
from .chats.records import (
    ARCHIVE_FETCH_PAGE_SIZE as ARCHIVE_FETCH_PAGE_SIZE,
)
from .chats.records import (
    EVENT_STORE_CHAT_PAGE_EVENT_TYPES as EVENT_STORE_CHAT_PAGE_EVENT_TYPES,
)
from .chats.records import (
    EVENT_STORE_QQ_FETCH_LIMIT as EVENT_STORE_QQ_FETCH_LIMIT,
)
from .chats.records import (
    QQ_VISIBLE_EVENT_TYPES as QQ_VISIBLE_EVENT_TYPES,
)
from .chats.records import (
    RUNTIME_EVENT_TYPES as RUNTIME_EVENT_TYPES,
)
from .chats.records import (
    _chat_timeline_message_to_record as _chat_timeline_message_to_record,
)
from .chats.records import (
    _dedupe_event_store_records as _dedupe_event_store_records,
)
from .chats.records import (
    _dedupe_records_by_real_identity as _dedupe_records_by_real_identity,
)
from .chats.records import (
    _duplicate_identity_text as _duplicate_identity_text,
)
from .chats.records import (
    _event_id as _event_id,
)
from .chats.records import (
    _event_sort_id as _event_sort_id,
)
from .chats.records import (
    _event_store_event_to_record as _event_store_event_to_record,
)
from .chats.records import (
    _event_store_record_duplicate_identity as _event_store_record_duplicate_identity,
)
from .chats.records import (
    _event_store_runtime_duplicate_identities as _event_store_runtime_duplicate_identities,
)
from .chats.records import (
    _event_type_is_runtime as _event_type_is_runtime,
)
from .chats.records import (
    _event_type_visible_on_chat_page as _event_type_visible_on_chat_page,
)
from .chats.records import (
    _first_payload_text as _first_payload_text,
)
from .chats.records import (
    _full_archive_records_for_page as _full_archive_records_for_page,
)
from .chats.records import (
    _history_runtime_event_records as _history_runtime_event_records,
)
from .chats.records import (
    _light_archive_result_to_record as _light_archive_result_to_record,
)
from .chats.records import (
    _load_archive_records_paged as _load_archive_records_paged,
)
from .chats.records import (
    _load_chat_page_records as _load_chat_page_records,
)
from .chats.records import (
    _load_chat_timeline_records as _load_chat_timeline_records,
)
from .chats.records import (
    _load_event_store_records as _load_event_store_records,
)
from .chats.records import (
    _merge_chat_page_records as _merge_chat_page_records,
)
from .chats.records import (
    _optional_record_text as _optional_record_text,
)
from .chats.records import (
    _payload_list as _payload_list,
)
from .chats.records import (
    _qq_event_conversation_id as _qq_event_conversation_id,
)
from .chats.records import (
    _qq_event_timestamp as _qq_event_timestamp,
)
from .chats.records import (
    _qq_visible_event_to_record as _qq_visible_event_to_record,
)
from .chats.records import (
    _real_record_identity as _real_record_identity,
)
from .chats.records import (
    _recent_chat_page_events as _recent_chat_page_events,
)
from .chats.records import (
    _record_conversation_id_for_display as _record_conversation_id_for_display,
)
from .chats.records import (
    _record_display_base_id as _record_display_base_id,
)
from .chats.records import (
    _record_event_id as _record_event_id,
)
from .chats.records import (
    _record_event_payload as _record_event_payload,
)
from .chats.records import (
    _record_is_qq_visible_outbound as _record_is_qq_visible_outbound,
)
from .chats.records import (
    _record_message_id as _record_message_id,
)
from .chats.records import (
    _record_timestamp as _record_timestamp,
)
from .chats.records import (
    _record_with_sort_layer as _record_with_sort_layer,
)
from .chats.records import (
    _runtime_event_conversation_id as _runtime_event_conversation_id,
)
from .chats.records import (
    _runtime_event_detail as _runtime_event_detail,
)
from .chats.records import (
    _runtime_event_summary as _runtime_event_summary,
)
from .chats.records import (
    _runtime_event_summary_parts as _runtime_event_summary_parts,
)
from .chats.records import (
    _runtime_event_summary_text as _runtime_event_summary_text,
)
from .chats.records import (
    _runtime_event_title as _runtime_event_title,
)
from .chats.records import (
    _runtime_event_to_record as _runtime_event_to_record,
)
from .chats.records import (
    _runtime_event_tool_call_id as _runtime_event_tool_call_id,
)
from .chats.records import (
    _runtime_event_tool_name as _runtime_event_tool_name,
)
from .chats.records import (
    _runtime_payload_subset as _runtime_payload_subset,
)
from .chats.records import (
    _runtime_record_duplicate_identities as _runtime_record_duplicate_identities,
)
from .chats.records import (
    _runtime_record_severity as _runtime_record_severity,
)
from .chats.records import (
    _runtime_record_tool_call_identity as _runtime_record_tool_call_identity,
)
from .chats.records import (
    _runtime_tool_call_arguments as _runtime_tool_call_arguments,
)
from .chats.records import (
    _runtime_tool_call_for_record as _runtime_tool_call_for_record,
)
from .chats.records import (
    _runtime_tool_result_payload as _runtime_tool_result_payload,
)
from .chats.records import (
    _send_runtime_duplicate_ids as _send_runtime_duplicate_ids,
)
from .chats.records import (
    _send_runtime_primary_duplicate_id as _send_runtime_primary_duplicate_id,
)
from .chats.records import (
    _sort_unique_chat_page_events as _sort_unique_chat_page_events,
)
from .chats.records import (
    _system_note_duplicate_identity as _system_note_duplicate_identity,
)
from .chats.records import (
    _tag_record_order as _tag_record_order,
)
from .chats.records import (
    _tool_call_name as _tool_call_name,
)
from .chats.records import (
    _unique_text_parts as _unique_text_parts,
)
from .chats.text_format import (
    INLINE_PREVIEW_LIMIT as INLINE_PREVIEW_LIMIT,
)
from .chats.text_format import (
    _compact_inline_tokens,
    _escape,
    _escape_attr,
    _extract_tag_text,
    _first_nonempty_line,
    _format_send_receipt_summary,
    _format_send_status_summary,
    _format_tool_call_for_display,
    _format_tool_result_summary,
    _parse_json_object,
    _send_receipt_sent_items,
    _send_status_info,
)
from .chats.text_format import (
    _compact_json_blob as _compact_json_blob,
)
from .chats.text_format import (
    _compact_path as _compact_path,
)
from .chats.text_format import (
    _compact_url as _compact_url,
)
from .chats.text_format import (
    _compact_workspace_path as _compact_workspace_path,
)
from .chats.text_format import (
    _extract_tag_json as _extract_tag_json,
)
from .chats.text_format import (
    _format_commit_send_attempt_args as _format_commit_send_attempt_args,
)
from .chats.text_format import (
    _format_generic_tool_args as _format_generic_tool_args,
)
from .chats.text_format import (
    _format_group_send_args as _format_group_send_args,
)
from .chats.text_format import (
    _format_message_list_summary as _format_message_list_summary,
)
from .chats.text_format import (
    _format_private_send_args as _format_private_send_args,
)
from .chats.text_format import (
    _format_send_receipt_text_summary as _format_send_receipt_text_summary,
)
from .chats.text_format import (
    _format_tool_arg_value as _format_tool_arg_value,
)
from .chats.text_format import (
    _format_upload_args as _format_upload_args,
)
from .chats.text_format import (
    _message_list_sample as _message_list_sample,
)
from .chats.text_format import (
    _message_with_delay as _message_with_delay,
)
from .chats.text_format import (
    _parse_send_receipt_text_sent_items as _parse_send_receipt_text_sent_items,
)
from .chats.text_format import (
    _parse_send_receipt_text_sent_line as _parse_send_receipt_text_sent_line,
)
from .chats.text_format import (
    _parse_tool_arguments as _parse_tool_arguments,
)
from .chats.text_format import (
    _send_receipt_text_field_value as _send_receipt_text_field_value,
)
from .chats.text_format import (
    _send_receipt_text_is_section_heading as _send_receipt_text_is_section_heading,
)
from .chats.text_format import (
    _send_receipt_text_section_count as _send_receipt_text_section_count,
)
from .chats.text_format import (
    _send_receipt_text_section_lines as _send_receipt_text_section_lines,
)
from .chats.text_format import (
    _send_receipt_text_send_id as _send_receipt_text_send_id,
)
from .chats.text_format import (
    _send_receipt_text_sent_items as _send_receipt_text_sent_items,
)
from .chats.text_format import (
    _send_status_payload_completed as _send_status_payload_completed,
)
from .chats.text_format import (
    _send_status_text_completed as _send_status_text_completed,
)
from .chats.text_format import (
    _short_text as _short_text,
)
from .copy import DASHBOARD_COPY
from .tool_display import format_tool_call, format_tool_result

logger = logging.getLogger(__name__)

DEFAULT_VISIBLE_RECORD_LIMIT = 300
VISIBLE_RECORD_STEP = 300
COMPACT_TEXT_LIMIT = 1800
CHAT_REFRESH_DEBOUNCE_MS = 100
CHAT_SEARCH_DEBOUNCE_MS = 150


class ChatsPage(QWidget):
    """列表 + 详情。"""

    _chat_timeline_changed = Signal()

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
        self._records_signature: tuple[int, str] | None = None
        self._conversation_display_cache: ConversationDisplayCache | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_pending = False
        self._refresh_generation = 0
        self._chat_timeline_source: Any | None = None
        self._chat_timeline_unsubscribe: Callable[[], None] | None = None
        self._chat_timeline_changed.connect(
            self._on_chat_timeline_changed,
            Qt.ConnectionType.QueuedConnection,
        )
        self.destroyed.connect(self._unsubscribe_chat_timeline)

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
        self._search_input.textChanged.connect(self._on_search_text_changed)
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

        self._show_reasoning_cb = QCheckBox("思考")
        self._show_reasoning_cb.setChecked(True)
        self._show_reasoning_cb.stateChanged.connect(self._on_filter_changed)
        filters.addWidget(self._show_reasoning_cb)

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

        self._refresh_debounce_timer = QTimer(self)
        self._refresh_debounce_timer.setSingleShot(True)
        self._refresh_debounce_timer.setInterval(CHAT_REFRESH_DEBOUNCE_MS)
        self._refresh_debounce_timer.timeout.connect(self._start_refresh_load)

        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(CHAT_SEARCH_DEBOUNCE_MS)
        self._search_debounce_timer.timeout.connect(self._apply_search_filter)

        self._sync_chat_timeline_subscription()
        self.refresh()

    def on_shown(self) -> None:
        self._sync_chat_timeline_subscription()
        if not self._timer.isActive():
            self._timer.start()
        self.refresh()

    def on_hidden(self) -> None:
        self._timer.stop()
        self._refresh_debounce_timer.stop()
        self._search_debounce_timer.stop()

    def refresh(self) -> None:
        self._sync_chat_timeline_subscription()
        rt = self._runtime
        if rt is None or rt.history is None:
            self._refresh_pending = False
            self._refresh_debounce_timer.stop()
            self._show_empty(True)
            return
        self._refresh_generation += 1
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_pending = True
            return
        self._refresh_debounce_timer.start()

    def _start_refresh_load(self) -> None:
        rt = self._runtime
        if rt is None or rt.history is None:
            self._refresh_pending = False
            self._show_empty(True)
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_pending = True
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        generation = self._refresh_generation
        self._refresh_task = loop.create_task(self._load_refresh(generation, rt))

    def closeEvent(self, event: Any) -> None:
        self._unsubscribe_chat_timeline()
        super().closeEvent(event)

    def _sync_chat_timeline_subscription(self) -> None:
        pipeline = getattr(self._runtime, "pipeline", None)
        timeline = getattr(pipeline, "chat_timeline", None)
        subscribe = getattr(timeline, "subscribe", None)
        if not callable(subscribe):
            timeline = None
        if timeline is self._chat_timeline_source:
            return
        self._unsubscribe_chat_timeline()
        if timeline is None:
            return

        page_ref = weakref.ref(self)

        def on_timeline_message(_message: Any) -> None:
            page = page_ref()
            if page is None:
                return
            try:
                page._chat_timeline_changed.emit()
            except RuntimeError:
                return

        try:
            unsubscribe = subscribe(on_timeline_message)
        except Exception as e:
            logger.warning("订阅实时聊天时间线失败: %s", e)
            return
        if not callable(unsubscribe):
            logger.warning("实时聊天时间线订阅未返回取消函数，已跳过订阅")
            return
        self._chat_timeline_source = timeline
        self._chat_timeline_unsubscribe = unsubscribe

    def _unsubscribe_chat_timeline(self, *_args: Any) -> None:
        unsubscribe = self._chat_timeline_unsubscribe
        self._chat_timeline_unsubscribe = None
        self._chat_timeline_source = None
        if unsubscribe is None:
            return
        try:
            unsubscribe()
        except Exception as e:
            logger.warning("取消实时聊天时间线订阅失败: %s", e)

    def _on_chat_timeline_changed(self) -> None:
        self.refresh()

    async def _load_refresh(self, generation: int, rt: Any) -> None:
        try:
            records = await _load_chat_page_records(rt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("加载 history 失败: %s", e)
            return
        else:
            if generation != self._refresh_generation:
                return
            self._set_records(records)
            self._render_list()
        finally:
            if self._refresh_task is asyncio.current_task():
                self._refresh_task = None
                if self._refresh_pending:
                    self._refresh_pending = False
                    self._start_refresh_load()

    def _show_empty(self, on: bool) -> None:
        self._splitter.setVisible(not on)
        self._empty.setVisible(on)

    def _set_records(self, records: list[dict]) -> None:
        signature = _records_cache_signature(records)
        if signature != self._records_signature:
            self._clear_conversation_display_cache()
        self._records_signature = signature
        self._records = records

    def _clear_conversation_display_cache(self) -> None:
        self._conversation_display_cache = None

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
        self._search_debounce_timer.stop()
        self._search_text = self._search_input.text().strip()
        self._current_detail_html = ""
        self._refresh_current_detail()

    def _on_search_text_changed(self, text: str) -> None:
        search_text = text.strip()
        if search_text == self._search_text:
            return
        self._search_text = search_text
        self._search_debounce_timer.start()

    def _apply_search_filter(self) -> None:
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

    def _display_filter_signature(self) -> tuple[str, bool, bool, bool, bool, bool]:
        return (
            self._search_text,
            self._show_chat_cb.isChecked(),
            self._show_system_cb.isChecked(),
            self._show_tools_cb.isChecked(),
            self._show_reasoning_cb.isChecked(),
            self._media_only_cb.isChecked(),
        )

    def _display_items_for_conversation(
        self,
        conv: dict,
        *,
        records: list[dict] | None = None,
    ) -> tuple[list[DisplayItem], list[DisplayItem]]:
        if records is None:
            records = list(conv.get("records") or [])
        conversation_key = str(conv.get("key") or "")
        persona_name = _runtime_persona_name(self._runtime)
        records_signature = _records_cache_signature(records)
        cache = self._conversation_display_cache
        if (
            cache is None
            or cache.conversation_key != conversation_key
            or cache.records_signature != records_signature
            or cache.persona_name != persona_name
        ):
            cache = ConversationDisplayCache(
                conversation_key=conversation_key,
                records_signature=records_signature,
                persona_name=persona_name,
                normalized_items=normalize_history_records(
                    records,
                    persona_name=persona_name,
                ),
            )
            self._conversation_display_cache = cache

        filter_signature = self._display_filter_signature()
        if cache.filter_signature != filter_signature:
            cache.filtered_items = _filter_display_items(
                cache.normalized_items,
                search_text=self._search_text,
                show_chat=self._show_chat_cb.isChecked(),
                show_system=self._show_system_cb.isChecked(),
                show_tools=self._show_tools_cb.isChecked(),
                show_reasoning=self._show_reasoning_cb.isChecked(),
                media_only=self._media_only_cb.isChecked(),
            )
            cache.filter_signature = filter_signature
        return cache.normalized_items, cache.filtered_items

    def _filtered_display_items_for_conversation(self, conv: dict) -> list[DisplayItem]:
        return self._display_items_for_conversation(conv)[1]

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
        started_at = time.perf_counter()
        records = list(conv.get("records") or [])
        all_items, filtered_items = self._display_items_for_conversation(
            conv,
            records=records,
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
        html = "".join(parts)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "对话详情渲染指标 conversation_key=%s raw_records=%d display_items=%d "
                "filtered_items=%d shown_items=%d html_length=%d elapsed_ms=%.3f",
                conv.get("key"),
                len(records),
                len(all_items),
                len(filtered_items),
                len(visible),
                len(html),
                (time.perf_counter() - started_at) * 1000,
            )
        return html

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
    """把 raw history 记录归一化成 UI 可显示项，并保留必要的工具结果位置。"""
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
            if _should_attach_tool_result_to_call(item):
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
    runtime_event_type = str(rec.get("_runtime_event_type") or "")

    if runtime_event_type == "tool_call_started":
        tool_call_id = str(rec.get("tool_call_id") or "").strip() or None
        tool_call = _runtime_tool_call_for_record(rec, base_id=base_id)
        return [
            DisplayItem(
                item_id=f"{base_id}:runtime_tool_call",
                conversation_id=conversation_id,
                timestamp=timestamp,
                kind="tool_call",
                speaker_label=persona_name,
                speaker_id=bot_user_id,
                role_label="工具动作",
                text=str(rec.get("_runtime_detail") or content),
                summary=str(rec.get("_runtime_summary") or _format_tool_call_for_display(tool_call)),
                raw={"record": rec, "tool_call": tool_call, "runtime_event": True},
                related_tool_call_id=tool_call_id,
                collapsed_by_default=True,
                severity=_runtime_record_severity(rec),
            )
        ]

    if runtime_event_type == "tool_result_received":
        tool_call_id = str(rec.get("tool_call_id") or "").strip() or None
        return [
            DisplayItem(
                item_id=f"{base_id}:runtime_tool_result",
                conversation_id=conversation_id,
                timestamp=timestamp,
                kind="tool_result",
                speaker_label="工具返回",
                speaker_id=None,
                role_label="工具返回",
                text=content,
                summary=str(rec.get("_runtime_summary") or _format_tool_result_summary(content)),
                raw=rec,
                related_tool_call_id=tool_call_id,
                collapsed_by_default=True,
                severity=_runtime_record_severity(rec),
            )
        ]

    if runtime_event_type:
        title = _runtime_event_title(runtime_event_type)
        summary = str(rec.get("_runtime_summary") or title)
        return [
            DisplayItem(
                item_id=f"{base_id}:runtime_event",
                conversation_id=conversation_id,
                timestamp=timestamp,
                kind="system_event",
                speaker_label="系统",
                speaker_id=None,
                role_label="系统",
                text=str(rec.get("_runtime_detail") or content),
                summary=f"{title} · {summary}" if summary and summary != title else title,
                raw=rec,
                collapsed_by_default=True,
                severity=_runtime_record_severity(rec),
            )
        ]

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


def _build_display_items(
    records: list[dict],
    *,
    persona_name: str,
    search_text: str,
    show_chat: bool,
    show_system: bool,
    show_tools: bool,
    show_reasoning: bool = True,
    media_only: bool = False,
) -> list[DisplayItem]:
    return _filter_display_items(
        normalize_history_records(records, persona_name=persona_name),
        search_text=search_text,
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
        show_reasoning=show_reasoning,
        media_only=media_only,
    )


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
    if isinstance(item.raw, dict) and item.raw.get("_runtime_event_type") == "tool_result_received":
        return _render_expandable_event_record(
            str(item.raw.get("_runtime_title") or "工具返回"),
            str(item.raw.get("_runtime_detail") or item.text),
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            event_class="chat-event-tool",
        )
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
    if isinstance(item.raw, dict) and item.raw.get("runtime_event"):
        record = item.raw.get("record") if isinstance(item.raw.get("record"), dict) else {}
        title = str(record.get("_runtime_title") or "工具调用")
        return _render_expandable_event_record(
            f"{item.speaker_label or '助手'} · {title}",
            item.text,
            toggle_id=expand_id,
            expanded=expand_id in expanded_item_ids,
            event_class="chat-event-tool",
        )
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


def _render_record_html(
    rec: dict,
    *,
    persona_name: str,
    show_chat: bool = True,
    show_system: bool = True,
    show_tools: bool = True,
    show_reasoning: bool = True,
) -> str:
    bubbles = _render_record_bubbles(
        rec,
        persona_name=persona_name,
        show_chat=show_chat,
        show_system=show_system,
        show_tools=show_tools,
        show_reasoning=show_reasoning,
    )
    return "".join(bubbles)


def _render_record_bubbles(
    rec: dict,
    *,
    persona_name: str,
    show_chat: bool = True,
    show_system: bool = True,
    show_tools: bool = True,
    show_reasoning: bool = True,
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
        show_reasoning=show_reasoning,
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


def _scrollbar_near_bottom(bar: Any, threshold: int = 24) -> bool:
    return bar.maximum() - bar.value() <= threshold
