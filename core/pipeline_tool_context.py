"""ToolContext construction helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change ToolContext extras, send callback metadata, agent-task
callback wiring, or outbound timeline recording while moving methods.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from adapters.types import Target
from tools import ToolContext

logger = logging.getLogger(__name__)


class PipelineToolContextMixin:
    def _build_tool_context(
        self,
        default_target: Target | None = None,
        latest_user_text: str = "",
        conversation_id: str | None = None,
        trigger_message_id: str | None = None,
        trigger_inbound_seq: int = 0,
        trigger_user_id: str | None = None,
        seen_inbound_seq: int = 0,
        focus_user_ids: list[str] | None = None,
        trigger_message_ids: list[str] | None = None,
        task_phase: str = "normal",
        tool_policy: dict[str, Any] | None = None,
    ) -> ToolContext:
        """每次 Agent 调用前构造新的 ToolContext。

        collected 是 per-call 的（绝不复用），所以每次新建实例。
        """
        extras: dict[str, Any] = {
            "tool_registry": self.tool_registry,
            "tool_search_approved_tools": set(),
            "self_id_by_conversation": dict(self._self_id_by_conversation),
            "pending_requests": self.pending_requests,
            "rate_limiter": self.rate_limiter,
        }
        if conversation_id:
            self_id = self._self_id_by_conversation.get(conversation_id)
            if self_id:
                extras["self_id"] = self_id
        if default_target is not None:
            raw_target_id = str(default_target.target_id)
            extras["default_reply_target"] = {
                "target_type": default_target.scope,
                "target_id": int(raw_target_id) if raw_target_id.isdigit() else raw_target_id,
            }
        if latest_user_text:
            extras["latest_user_message"] = latest_user_text
        extras["chat_timeline"] = self.chat_timeline
        extras["task_phase"] = task_phase
        extras["seen_inbound_seq"] = int(seen_inbound_seq or trigger_inbound_seq or 0)
        extras["focus_user_ids"] = list(focus_user_ids or [])
        extras["trigger_message_ids"] = list(trigger_message_ids or [])
        if tool_policy:
            extras["tool_policy"] = tool_policy

        async def _send_actions(
            actions: list[dict[str, Any]],
            source_tool: str,
            *,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return await self._send_manager.submit(
                actions,
                source_tool,
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
                default_reviewed_until_seq=extras["seen_inbound_seq"],
                default_focus_user_ids=extras["focus_user_ids"],
                default_trigger_message_ids=extras["trigger_message_ids"],
                metadata=metadata,
            )

        async def _agent_task(payload: dict[str, Any]) -> dict[str, Any]:
            return await self._start_agent_task(
                payload,
                conversation_id=conversation_id,
                default_target=default_target,
            )

        return ToolContext(
            adapter=self.adapter,
            important=self.important,
            conversation_id=conversation_id,
            history=self.history,
            archive=self.archive,
            wakeup_cb=self.wakeup_scheduler.schedule,
            vision=self.vision,
            web_search=self.web_search,
            weather=self.weather,
            tts=self.tts,
            persona_agent=getattr(self, "persona_agent", None),
            subconscious_agent=getattr(self, "subconscious_agent", None),
            persona_db=getattr(self, "persona_db", None),
            workspace_dir=self.workspace_dir,
            emoji_dir=self.emoji_dir,
            tool_result_default_budget_tokens=self.behavior_cfg.context.tool_result_default_budget_tokens,
            tool_result_default_hard_cap_tokens=self.behavior_cfg.context.tool_result_default_hard_cap_tokens,
            tool_result_budgets=dict(self.behavior_cfg.context.tool_result_budgets),
            tool_result_soft_limit_tokens=self.behavior_cfg.context.tool_result_soft_limit_tokens,
            tool_result_hard_cap_tokens=self.behavior_cfg.context.tool_result_hard_cap_tokens,
            tool_result_soft_overrides=dict(self.behavior_cfg.context.tool_result_soft_overrides),
            activity_cb=self.mark_activity,
            send_actions_cb=_send_actions,
            agent_task_cb=_agent_task,
            default_history_fetch_count=self.behavior_cfg.default_history_fetch_count,
            collected=[],
            extras=extras,
        )

    async def _record_successful_outbound(
        self,
        action: dict[str, Any],
        *,
        conversation_id: str,
        msg_id: str,
    ) -> None:
        """记录 QQ 上真实发送成功的出站消息。"""
        self_id = self._self_id_by_conversation.get(conversation_id) or None
        timestamp_unix = time.time()
        normalized = {
            "target_scope": action.get("target_scope") or action.get("action"),
            "target_id": action.get("target_id") or action.get("target"),
            "content": action.get("content") or "",
            "label": action.get("label") or action.get("content") or "",
            "kind": action.get("kind", "text"),
        }
        self.chat_timeline.append_outbound_action(
            normalized,
            conversation_id=conversation_id,
            msg_id=msg_id,
            self_id=self_id,
            timestamp=timestamp_unix,
        )
        event_store = getattr(self, "event_store", None) or getattr(self, "_event_store", None)
        if event_store is None:
            return
        await self._append_qq_message_sent_event(
            event_store,
            normalized,
            conversation_id=conversation_id,
            msg_id=msg_id,
            self_id=self_id,
            timestamp_unix=timestamp_unix,
        )

    async def _append_qq_message_sent_event(
        self,
        event_store: Any,
        action: dict[str, Any],
        *,
        conversation_id: str,
        msg_id: str,
        self_id: str | None,
        timestamp_unix: float,
    ) -> None:
        source = _optional_text(getattr(self.adapter, "name", None))
        external_id = _optional_text(msg_id)
        payload = {
            "direction": "outbound",
            "conversation_id": conversation_id,
            "source": source,
            "msg_id": external_id,
            "content": action.get("content") or "",
            "label": action.get("label") or action.get("content") or "",
            "kind": action.get("kind", "text"),
            "target_scope": action.get("target_scope"),
            "target_id": action.get("target_id"),
            "self_id": self_id,
            "timestamp_unix": timestamp_unix,
        }
        debug_enabled = logger.isEnabledFor(logging.DEBUG)
        write_started_at = time.perf_counter() if debug_enabled else 0.0
        await event_store.append_event(
            event_type="qq_message_sent",
            conversation_id=conversation_id,
            source=source,
            external_id=external_id,
            idempotency_key=(
                f"qq_message_sent:{conversation_id}:{external_id}:sent"
                if external_id
                else None
            ),
            timestamp_unix=timestamp_unix,
            payload=payload,
        )
        if debug_enabled:
            logger.debug(
                "QQ 出站消息 EventStore 写入指标 conversation_id=%s msg_id=%s "
                "elapsed_ms=%.3f",
                conversation_id,
                external_id,
                (time.perf_counter() - write_started_at) * 1000,
            )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
