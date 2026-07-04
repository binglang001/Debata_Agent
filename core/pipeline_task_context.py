"""Task-context and batch-focus helpers for MessagePipeline.

This module owns only lightweight runtime task hints. Full recent chat windows
stay available through tools so they do not inflate every prompt.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any

from .send_manager import _text_mentions_self_or_role
from .state import PendingMessageItem

logger = logging.getLogger(__name__)


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

        persona_context = self._persona_context_for_chat(conversation_id)
        if persona_context:
            parts.append(persona_context)

        return "\n".join(parts)

    def _persona_context_for_chat(self, conversation_id: str | None) -> str:
        if not conversation_id:
            return ""
        persona_agent = getattr(self, "persona_agent", None)
        if persona_agent is None:
            return ""
        get_context = getattr(persona_agent, "get_context_for_chat", None)
        if get_context is None:
            return ""
        try:
            context = get_context(conversation_id)
        except Exception:
            logger.debug("获取人格聊天上下文失败", exc_info=True)
            return ""
        if inspect.isawaitable(context):
            logger.debug("忽略异步人格聊天上下文：当前 task_context 构造为同步路径")
            return ""
        return str(context or "").strip()

    def _build_messages_persona_tool_kwargs(
        self,
        build_messages_fn: Callable[..., list[dict[str, Any]]],
    ) -> dict[str, bool]:
        flags = {
            "eat_tool": bool(getattr(self, "eat_tool", False)),
            "sleep_tool": bool(getattr(self, "sleep_tool", False)),
        }
        try:
            signature = inspect.signature(build_messages_fn)
        except (TypeError, ValueError):
            return {}
        parameters = signature.parameters
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return flags
        return {key: value for key, value in flags.items() if key in parameters}

    def _participants_from_pending_items(
        self,
        items: list[PendingMessageItem],
    ) -> list[dict[str, str]]:
        participants: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            user_id = str(item.user_id or "").strip()
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            participant = {"user_id": user_id}
            nickname = str(item.nickname or "").strip()
            if nickname:
                participant["nickname"] = nickname
            participants.append(participant)
        return participants

    def _batch_chat_summary(
        self,
        latest_user_text: str,
        records: list[dict[str, Any]],
    ) -> str:
        return _compact_chat_summary(
            user_or_event_text=latest_user_text,
            task_context="",
            records=records,
        )

    def _run_one_turn_chat_summary(
        self,
        *,
        user_event: str | None,
        task_context: str,
        records: list[dict[str, Any]],
    ) -> str:
        return _compact_chat_summary(
            user_or_event_text=user_event or "",
            task_context=task_context,
            records=records,
        )

    def _schedule_persona_after_turn(
        self,
        *,
        conversation_id: str,
        participants: list[dict[str, str]],
        chat_summary: str,
    ) -> None:
        persona_agent = getattr(self, "persona_agent", None)
        if persona_agent is None:
            return
        after_turn = getattr(persona_agent, "after_turn", None)
        if after_turn is None:
            return

        async def _run_after_turn() -> None:
            try:
                result = after_turn(conversation_id, participants, chat_summary)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("人格 after_turn hook 执行失败", exc_info=True)

        task = asyncio.create_task(_run_after_turn())
        tasks = getattr(self, "_persona_after_turn_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _stop_subconscious_agent(self) -> None:
        subconscious_agent = getattr(self, "subconscious_agent", None)
        if subconscious_agent is None:
            return
        stop = getattr(subconscious_agent, "stop", None)
        if stop is None:
            return
        try:
            result = stop()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("停止潜意识 Agent 失败", exc_info=True)

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


def _compact_chat_summary(
    *,
    user_or_event_text: str,
    task_context: str,
    records: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    user_text = str(user_or_event_text or "").strip()
    if user_text:
        parts.append(f"外部输入/系统事件：{_truncate_summary_part(user_text)}")
    context_text = str(task_context or "").strip()
    if context_text:
        parts.append(f"系统上下文：{_truncate_summary_part(context_text)}")

    assistant_text = _assistant_final_text(records)
    if assistant_text:
        parts.append(f"当前人格自己的回复：{_truncate_summary_part(assistant_text)}")
    return "\n".join(parts)


def _assistant_final_text(records: list[dict[str, Any]]) -> str:
    for record in reversed(records):
        if record.get("role") != "assistant":
            continue
        content = str(record.get("content") or "").strip()
        if content:
            return content

    tool_call_texts: list[str] = []
    for record in records:
        if record.get("role") != "assistant":
            continue
        tool_call_texts.extend(_assistant_tool_call_texts(record))
    return "；".join(text for text in tool_call_texts if text)


def _assistant_tool_call_texts(record: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for tool_call in record.get("tool_calls") or []:
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if name == "no_action":
            continue
        args = _decode_tool_arguments(function.get("arguments"))
        if not args:
            continue
        texts.extend(_content_values_from_tool_args(args))
    return texts


def _decode_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _content_values_from_tool_args(args: dict[str, Any]) -> list[str]:
    values: list[str] = []
    direct = _summary_text(args.get("content"))
    if direct:
        values.append(direct)
    message = _summary_text(args.get("message"))
    if message:
        values.append(message)
    targets = args.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            target_content = _summary_text(target.get("content"))
            if target_content:
                values.append(target_content)
    return values


def _summary_text(value: Any) -> str:
    return str(value or "").strip()


def _truncate_summary_part(text: str, limit: int = 400) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."
