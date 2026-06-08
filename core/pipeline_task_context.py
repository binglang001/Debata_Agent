"""Task-context and batch-focus helpers for MessagePipeline.

This module owns only lightweight runtime task hints. Full recent chat windows
stay available through tools so they do not inflate every prompt.
"""

from __future__ import annotations

from .send_manager import _text_mentions_self_or_role
from .state import PendingMessageItem


class PipelineTaskContextMixin:
    def _build_task_context(self, now: str, conversation_id: str | None = None) -> str:
        """组装本轮 task_context 文本。

        组成：当前时间 + 当前会话 + 表情包提示 + 上下文查询提示 + 待处理请求
        """
        from tools import build_emoji_hint

        parts: list[str] = []
        emoji_hint = build_emoji_hint(self.emoji_dir)
        if emoji_hint:
            parts.append(f"现在是{now}。{emoji_hint}。")
        else:
            parts.append(f"现在是{now}。")
        if conversation_id:
            parts.append(f"当前会话：{conversation_id}。")
            lookup_hint = self._context_lookup_hint(conversation_id)
            if lookup_hint:
                parts.append(lookup_hint)

        pending_info = self.pending_requests.to_prompt_text()
        if pending_info:
            parts.append(pending_info)

        return "\n".join(parts)

    def _context_lookup_hint(self, conversation_id: str) -> str:
        """给群聊轮次追加轻量查询提示，不内联最近聊天正文。"""
        if not conversation_id.startswith("group:"):
            return ""
        return (
            '<conversation_context_hint source="runtime">\n'
            "task_context 不自动注入当前群最近完整聊天窗口。"
            "需要确认最近 QQ 可见消息、引用、插话或断层时，调用 get_recent_chat_messages；"
            "需要更早或跨会话历史时，先通过 tool_search 查询 recall_history 后再按需调用。"
            "\n</conversation_context_hint>"
        )

    def _batch_trigger_message_ids(
        self,
        items: list[PendingMessageItem],
    ) -> list[str]:
        explicit: list[str] = []
        for item in items:
            if self._is_explicit_batch_trigger(item):
                message_id = str(item.message_id or "").strip()
                if message_id and message_id not in explicit:
                    explicit.append(message_id)
        if explicit:
            return explicit
        if not items:
            return []
        last_id = str(items[-1].message_id or "").strip()
        return [last_id] if last_id else []

    def _batch_focus_user_ids(
        self,
        items: list[PendingMessageItem],
        trigger_message_ids: list[str],
    ) -> list[str]:
        focus: list[str] = []
        trigger_set = set(trigger_message_ids)
        for item in items:
            if str(item.message_id or "") not in trigger_set:
                continue
            user_id = str(item.user_id or "").strip()
            if user_id and user_id not in focus:
                focus.append(user_id)
        if focus:
            return focus
        if not items:
            return []
        user_id = str(items[-1].user_id or "").strip()
        return [user_id] if user_id else []

    def _is_explicit_batch_trigger(self, item: PendingMessageItem) -> bool:
        if item.raw_event.is_private():
            return True
        if _text_mentions_self_or_role(
            item.text,
            item.raw_event.self_id,
            self.persona.name,
        ):
            return True
        if item.raw_event.reply_to and self._reply_targets_recent_outbound(
            item.conversation_id,
            item.raw_event.reply_to,
        ):
            return True
        return False

    def _reply_targets_recent_outbound(
        self,
        conversation_id: str,
        reply_to: str,
    ) -> bool:
        target = str(reply_to or "").strip()
        if not target:
            return False
        for message in self.chat_timeline.recent(conversation_id, 20):
            if message.direction == "outbound" and str(message.msg_id or "") == target:
                return True
        return False
