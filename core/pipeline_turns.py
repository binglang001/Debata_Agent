"""Out-of-band model turn helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change wakeup prompts, tool denylist handling, history
conversation routing, or run-one-turn locking while moving methods.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from adapters.types import Target
from agents import build_messages
from utils import get_time

from .pipeline_context import _earliest_record_ts, _filter_tool_schemas

logger = logging.getLogger(__name__)

_OUT_OF_BAND_DENIED_TOOLS = frozenset(
    {
        "start_agent_task",
        "summarize_conversation",
        "summarize_chat_history",
    }
)


def _message_pipeline_global(name: str, fallback):
    module = sys.modules.get("core.message_pipeline")
    return getattr(module, name, fallback) if module is not None else fallback


class PipelineTurnsMixin:
    async def run_wakeup_turn(
        self,
        reminder: str,
        target: dict[str, Any] | None = None,
        mode: str = "wakeup",
        message_text: str | None = None,
    ) -> None:
        """schedule_wakeup 到时调用，按模式执行定时任务。

        mode=send_message 直接发固定消息；mode=wakeup 跑一轮 Agent。
        """
        now = get_time()
        self.mark_activity()
        logger.info(f"定时任务执行 mode={mode}: {reminder!r}")

        conversation_id: str | None = None
        if target:
            target_type = target.get("target_type")
            target_id = target.get("target_id")
            if target_type and target_id is not None:
                conversation_id = f"{target_type}:{target_id}"
        await self.history.add_system_note(
            f"{now} 定时唤醒：{reminder}",
            conversation_id=conversation_id or "system:wakeup",
        )

        if mode == "send_message":
            if target and message_text:
                await self._send_scheduled_message(target, message_text)
            else:
                logger.warning(
                    "mode=send_message 缺少 target 或 message_text，跳过执行"
                )
            return

        target_hint = ""
        if target:
            target_type = target.get("target_type")
            target_id = target.get("target_id")
            if target_type and target_id is not None:
                target_hint = (
                    f"\n本次唤醒来自一个明确的提醒目标：{target_type}:{target_id}。"
                    "如果 reminder 要求通知这个目标，请调用发送消息工具；如果任务无需通知，可以 no_action。"
                )
        task_context = (
            f"现在是{now}。这是定时唤醒轮的环境信息，不是新用户消息。\n"
            "固定消息发送应在设置阶段使用 schedule_wakeup 的 mode=send_message；本模式只处理需要查询、整理、判断或调用工具的复杂任务。\n"
            f"{target_hint}"
        )
        user_event = (
            "[系统事件 · 非用户消息] 定时唤醒已到。\n"
            f"提醒任务原文：{reminder}\n"
            "现在就执行这条提醒；通常需要给提醒目标发送消息或完成提醒中指定的查询/判断。\n"
            "只处理这一条提醒，做完即止；不要延续或重复最近对话里已完成、无关或仅作背景的话题。\n"
            "只有这条提醒确实纯内部、无需通知时，才调用 no_action。"
        )
        denied_tools = _message_pipeline_global(
            "_OUT_OF_BAND_DENIED_TOOLS",
            _OUT_OF_BAND_DENIED_TOOLS,
        )
        await self.run_one_turn(
            task_context,
            user_event=user_event,
            default_target=(
                self._target_from_conversation_id(conversation_id)
                if conversation_id
                else None
            ),
            conversation_id=conversation_id,
            history_conversation_id=conversation_id or "system:wakeup",
            task_contract=f"定时唤醒任务：{reminder}",
            task_phase="wakeup",
            tool_denylist=denied_tools,
        )

    async def run_one_turn(
        self,
        task_context: str,
        *,
        user_event: str | None = None,
        as_system_note: str | None = None,
        lock_already_held: bool = False,
        default_target: Target | None = None,
        conversation_id: str | None = None,
        history_conversation_id: str | None = None,
        task_contract: str | None = None,
        task_phase: str = "normal",
        tool_policy: dict[str, Any] | None = None,
        tool_denylist: set[str] | frozenset[str] | None = None,
    ) -> None:
        """通用单轮 Agent 入口：注入 task_context，跑一轮，处理 collected。

        Args:
            task_context: 本轮 ephemeral context（时间、事件描述、提醒等）
            as_system_note: 若给，会在调 Agent 前写入 history 作为事件记录
                （如撤回通知、请求通知）
            history_conversation_id: 仅用于历史展示归类。默认沿用
                conversation_id；后台主动思考这类全局系统事件可传
                system:proactive，避免被归入最近一个聊天会话。
            task_contract: 本轮任务锚点，传给 AgentRunner 防止工具循环漂移。
            task_phase: 本轮运行阶段，保留给日志归类和未来策略扩展。
            tool_policy: 本轮工具策略所需的结构化上下文。
            tool_denylist: 本轮不允许调用的工具名集合。schema 仍保持暴露，调用时返回 denied。
        """
        self.mark_activity()
        record_conversation_id = history_conversation_id or conversation_id
        if as_system_note:
            await self.history.add_system_note(
                as_system_note,
                conversation_id=record_conversation_id,
            )

        ctx = self._build_tool_context(
            default_target=default_target,
            conversation_id=conversation_id,
            task_phase=task_phase,
            tool_policy=tool_policy,
        )
        executor = self.tool_registry.get_executor(ctx)
        denied_tools = set(tool_denylist or ())
        tools_schema = _filter_tool_schemas(
            self.tool_registry.get_schemas(),
            denied_tools,
        )
        if denied_tools:
            base_executor = executor

            async def _guarded_executor(
                tool_name: str,
                args: dict[str, Any],
                *,
                tool_call_id: str | None = None,
            ) -> dict[str, Any]:
                if tool_name in denied_tools:
                    return {
                        "ok": False,
                        "status": "denied",
                        "error": f"本轮系统事件不允许调用工具 {tool_name}",
                        "next": "只处理当前系统事件；如无需行动请调用 no_action。",
                    }
                return await base_executor(tool_name, args, tool_call_id=tool_call_id)

            executor = _guarded_executor

        async def _rebuild_messages() -> list[dict[str, Any]]:
            history_window = await self._select_working_history(conversation_id)
            important_text = await self._important_memory_text(conversation_id)
            rag_context_text = await self._rag_context_text(
                conversation_id,
                query=user_event or task_context,
                before_ts=_earliest_record_ts(history_window),
            )
            return build_messages(
                persona=self.persona,
                history=history_window,
                important_memory_text=important_text,
                rag_context_text=rag_context_text,
                rolling_summary_text=self._rolling_summary_text(),
                current_context=task_context,
                memory_mode=self.features_cfg.long_term_memory.mode,
                user_event=user_event,
            )

        budget_result = await self._prepare_main_prompt_for_model(
            conversation_id=conversation_id,
            phase=f"run_one_turn:{task_phase}",
            tools_schema=tools_schema,
            rebuild_messages=_rebuild_messages,
        )
        if not budget_result.ok:
            logger.error(
                "run_one_turn 预算预检失败，跳过模型调用 conversation_id=%s "
                "estimated=%s budget=%s",
                conversation_id,
                budget_result.estimated_tokens,
                budget_result.budget_tokens,
            )
            return
        messages = budget_result.messages

        async def _run_locked() -> None:
            self._send_manager.begin_model_turn(conversation_id)
            try:
                result = await self.chat_agent.run(
                    messages,
                    tools=tools_schema,
                    tool_executor=executor,
                    task_contract=task_contract,
                    pending_context_provider=(
                        (lambda: self._consume_send_receipts(conversation_id))
                        if conversation_id
                        else None
                    ),
                    runtime_event_callback=self._runtime_event_callback(
                        record_conversation_id or conversation_id or "system:global"
                    ),
                )
            finally:
                self._send_manager.end_model_turn(conversation_id)

            if result.records:
                await self.history.add_records(
                    result.records,
                    conversation_id=record_conversation_id,
                )
            if ctx.collected:
                await self._execute_collected(ctx.collected)
            self.mark_activity()

        if lock_already_held:
            await _run_locked()
        else:
            async with self.reply_lock:
                await _run_locked()
