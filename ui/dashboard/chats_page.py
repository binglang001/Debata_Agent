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
from typing import Any
from urllib.parse import urlsplit

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

DEFAULT_VISIBLE_RECORD_LIMIT = 300
VISIBLE_RECORD_STEP = 300
COMPACT_TEXT_LIMIT = 1800
INLINE_PREVIEW_LIMIT = 80


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
        total = len(conv.get("records") or [])
        self._visible_record_limits[self._current_key] = min(
            total,
            current + VISIBLE_RECORD_STEP,
        )
        self._current_detail_html = ""
        self._refresh_current_detail()

    def _update_load_more_state(self, conv: dict | None = None) -> None:
        if conv is None and self._current_key:
            conv = next((c for c in self._conversations if c["key"] == self._current_key), None)
        if conv is None:
            self._load_more_btn.setEnabled(False)
            self._load_more_btn.setText("加载更早")
            return
        total = len(conv.get("records") or [])
        limit = self._visible_record_limits.get(
            str(conv.get("key") or ""),
            DEFAULT_VISIBLE_RECORD_LIMIT,
        )
        hidden = max(0, total - limit)
        self._load_more_btn.setEnabled(hidden > 0)
        self._load_more_btn.setText(
            f"加载更早（{hidden}）" if hidden > 0 else "已显示全部"
        )

    def _restore_detail_scroll(self, value: int, *, stick_to_bottom: bool) -> None:
        bar = self._detail.verticalScrollBar()
        if stick_to_bottom:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(min(value, bar.maximum()))

    def _render_conversation(self, conv: dict) -> str:
        records = list(conv.get("records") or [])
        limit = self._visible_record_limits.get(
            str(conv.get("key") or ""),
            DEFAULT_VISIBLE_RECORD_LIMIT,
        )
        visible = records[-limit:] if limit > 0 else records
        hidden = max(0, len(records) - len(visible))
        persona_name = _runtime_persona_name(self._runtime)
        parts = [
            "<style>"
            ".chat-record{margin:10px 0;padding:10px 12px;border-radius:8px;}"
            ".chat-user{background:#F6F1E8;}"
            ".chat-assistant{background:#EAF4F0;}"
            ".chat-tool{background:#EEF4FA;}"
            ".chat-system{background:#F3F2F0;color:#5F5952;}"
            ".chat-head{font-weight:600;margin-bottom:6px;}"
            ".chat-meta{color:#7A7168;font-size:12px;}"
            ".chat-pre{white-space:pre-wrap;margin:6px 0 0 0;}"
            ".chat-summary{color:#6B635A;}"
            ".chat-tool-list{margin:6px 0 0 18px;}"
            "</style>"
            f"<h3 style='margin:0 0 4px 0'>{_escape(conv['label'])}</h3>"
            f"<div class='chat-meta'>已显示 {len(visible)} / 共 {len(records)} 条</div>"
        ]
        if hidden:
            parts.append(
                "<div class='chat-record chat-system'>"
                f"还有 {hidden} 条更早记录未显示。点击上方“加载更早”继续展开。"
                "</div>"
            )
        for rec in visible:
            parts.append(_render_record_html(rec, persona_name=persona_name))
            parts.append("<hr/>")
        return "".join(parts)

    def _render_record(self, rec: dict) -> str:
        return _render_record_html(
            rec,
            persona_name=_runtime_persona_name(self._runtime),
        )


_LEGACY_HEADER_RE = re.compile(
    r"^【(?P<timestamp>.*?) (?P<location>群聊 (?P<group_id>\S+)|私聊) "
    r"(?P<nickname>.*?)\((?P<user_id>.*?)\) msg_id=(?P<message_id>.*?)】",
    re.S,
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\s<>'\"|?*]+")
_WORKSPACE_PATH_RE = re.compile(r"\b(?:workspace=)?(?:incoming|workspace|outgoing)[/\\][^\s\]]+")


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


def _runtime_persona_name(runtime: Any) -> str:
    persona = getattr(runtime, "persona", None)
    name = getattr(persona, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "助手"


def _render_record_html(rec: dict, *, persona_name: str) -> str:
    role = rec.get("role", "?")
    content = str(rec.get("content") or "")
    reasoning = str(rec.get("reasoning_content") or "")
    tool_calls = rec.get("tool_calls") or []
    tool_call_id = str(rec.get("tool_call_id") or "")
    runtime = _runtime_event_summary(content)

    if role == "tool":
        head = f"工具结果 · {tool_call_id}" if tool_call_id else "工具结果"
        css = "chat-tool"
        body = _render_tool_result_content(content)
    elif runtime is not None:
        title, summary = runtime
        head = f"系统 · {title}"
        css = "chat-system"
        body = _render_collapsed_content(summary, content)
    elif role == "user":
        head = _user_record_label(rec)
        css = "chat-user"
        body = _render_text_block(_strip_legacy_header(content))
    elif role == "assistant":
        head = persona_name
        css = "chat-assistant"
        body = _render_text_block(content) if content else ""
    elif role == "system":
        head = "系统"
        css = "chat-system"
        body = _render_text_block(content)
    else:
        head = str(role or "记录")
        css = "chat-system"
        body = _render_text_block(content)

    parts = [f"<div class='chat-record {css}'>", f"<div class='chat-head'>{_escape(head)}</div>"]
    if reasoning:
        parts.append(
            "<details><summary class='chat-summary'>思考过程</summary>"
            f"<pre class='chat-pre'>{_escape(reasoning)}</pre></details>"
        )
    if body:
        parts.append(body)
    if tool_calls:
        parts.append(f"<div class='chat-head'>{_escape(persona_name)} · 工具调用</div>")
        parts.append("<ul class='chat-tool-list'>")
        for tc in tool_calls:
            label = _format_tool_call_for_display(tc)
            parts.append(f"<li>{_escape(label)}</li>")
        parts.append("</ul>")
        parts.append(
            "<details><summary class='chat-summary'>展开原始工具参数</summary>"
            f"<pre class='chat-pre'>{_escape(json.dumps(tool_calls, ensure_ascii=False, indent=2))}</pre>"
            "</details>"
        )
    parts.append("</div>")
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
    if "<send_receipt" in content:
        return "发送回执", _format_send_receipt_summary(content)
    if "<task_context" in content:
        return "运行时上下文", _format_task_context_summary(content)
    return None


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
    if new_messages:
        parts.append(f"新消息 {len(new_messages)} 条")
    if recalled:
        parts.append(f"撤回 {len(recalled)} 条")
    note = str(payload.get("note") or payload.get("next") or "").strip()
    if note:
        parts.append(_compact_inline_tokens(note))
    return "；".join(parts) if parts else "发送回执"


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


def _render_tool_result_content(content: str) -> str:
    summary = _format_tool_result_summary(content)
    return _render_collapsed_content(summary, content)


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
    sent = payload.get("sent")
    if isinstance(sent, list):
        parts.append(f"已发送 {len(sent)} 条")
    attempted = payload.get("attempted_messages")
    if isinstance(attempted, list):
        parts.append(f"尝试 {len(attempted)} 条")
    new_messages = payload.get("new_messages") or payload.get("new_visible_messages")
    if isinstance(new_messages, list):
        parts.append(f"新消息 {len(new_messages)} 条")
    next_hint = str(payload.get("next") or payload.get("note") or "").strip()
    if next_hint:
        parts.append(_compact_inline_tokens(next_hint))
    return "；".join(parts) if parts else _compact_inline_tokens(content)


def _render_collapsed_content(summary: str, content: str) -> str:
    return (
        f"<div class='chat-summary'>{_escape(summary)}</div>"
        "<details><summary class='chat-summary'>展开原文</summary>"
        f"<pre class='chat-pre'>{_escape(content)}</pre></details>"
    )


def _render_text_block(content: str) -> str:
    if not content:
        return ""
    compact = _compact_inline_tokens(content)
    should_collapse = compact != content or len(content) > COMPACT_TEXT_LIMIT
    preview = compact
    if len(preview) > COMPACT_TEXT_LIMIT:
        preview = f"{preview[:COMPACT_TEXT_LIMIT]}..."
    parts = [f"<pre class='chat-pre'>{_escape(preview)}</pre>"]
    if should_collapse:
        parts.append(
            "<details><summary class='chat-summary'>展开原文</summary>"
            f"<pre class='chat-pre'>{_escape(content)}</pre></details>"
        )
    return "".join(parts)


def _compact_inline_tokens(content: str) -> str:
    compact = _URL_RE.sub(lambda m: _compact_url(m.group(0)), content)
    compact = _WINDOWS_PATH_RE.sub(lambda m: _compact_path(m.group(0)), compact)
    compact = _WORKSPACE_PATH_RE.sub(lambda m: _compact_workspace_path(m.group(0)), compact)
    return compact


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
    return f"{name}：{json.dumps(args, ensure_ascii=False)}"


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


def _scrollbar_near_bottom(bar: Any, threshold: int = 24) -> bool:
    return bar.maximum() - bar.value() <= threshold
