"""AgentRunner —— 通用的多轮工具循环执行框架。

替代旧版 llm_client.run_agent_loop。差异：
    - 直接面向 IProvider（不再绑死 DeepSeek）
    - 工具执行通过外部注入的 executor，避免循环依赖
    - 显式记录 reasoning 过程，方便 UI 可视化
    - finish_reason 结构化（替代旧版 final 字符串判定）

循环退出条件（保留 V1 语义）：
    1. AI 调用了 no_action → finish_reason='no_action'
    2. 所有非 no_action 工具显式允许成功后结束 → 'finish_after_success'
    3. AI 未调用工具，提示重试后仍不调用 → 'no_tool_after_retry'
    4. 工具循环提醒宽限用完 → 'tool_loop_finalized'，随后无工具收尾
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app_config.schema import AgentConfig
from app_config.schema import ReasoningConfig as CfgReasoning
from providers.base import (
    CompletionResult,
    IProvider,
    ProviderError,
    ProviderTimeoutError,
    ReasoningConfig,
    ToolCall,
    normalize_messages,
)

from .base import AgentRunResult, FinishReason, StatusCallback, ToolExecutor, UsageRecorder

logger = logging.getLogger(__name__)


# 兼容旧导出；runner 不再根据这个集合提前结束。
DEFAULT_NO_FEEDBACK_TOOLS: set[str] = {
    "save_important_memory",
    "update_important_memory",
    "delete_important_memory",
    "no_action",
    "send_poke",
    "set_msg_emoji_like",
    "set_friend_add_request",
    "set_group_add_request",
    "schedule_wakeup",
}

# 发送类工具需要把结果回填给模型；发送成功不等于本轮结束。
SEND_TOOL_NAMES: set[str] = {
    "send_private_messages",
    "send_group_message",
    "send_voice_message",
}

LOOP_REMINDER_MESSAGE = (
    "你已连续多轮调用工具。请重新审视任务内容和执行情况，不要在同一个错误上反复无意义尝试；"
    "必要时更换方向、汇报进展或收尾。"
)


class AgentRunner:
    """多轮工具循环执行器。"""

    def __init__(
        self,
        provider: IProvider,
        agent_cfg: AgentConfig,
        *,
        no_feedback_tools: set[str] | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = agent_cfg
        self.no_feedback_tools = (
            no_feedback_tools if no_feedback_tools is not None else DEFAULT_NO_FEEDBACK_TOOLS
        )

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tool_executor: ToolExecutor,
        task_contract: str | None = None,
        pending_context_provider: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        max_loops: int | None = None,
        usage_recorder: UsageRecorder | None = None,
        status_callback: StatusCallback | None = None,
        status_label: str = "主模型",
    ) -> AgentRunResult:
        """执行多轮工具循环。

        Args:
            task_contract: 本轮的"任务合约"——一句话描述本轮目标。
                每 refocus_interval 轮重注入一次到 messages 末尾，防止焦点漂移。
        """
        msgs = list(messages)
        records: list[dict[str, Any]] = []
        reasoning_logs: list[str] = []
        final_content = ""
        finish_reason: FinishReason = "no_response"
        loop_count = 0
        no_tool_retry_count = 0
        prompt_tokens_total = 0
        refocus_interval = self.cfg.refocus_interval
        reminder_interval = max(1, int(self.cfg.tool_loop_reminder_interval))
        final_warning_count = max(1, int(self.cfg.tool_loop_final_warning_count))
        final_grace_loops = max(0, int(self.cfg.tool_loop_final_grace_loops))
        tool_rounds_since_reminder = 0
        tool_loop_reminder_count = 0
        final_warning_sent = False
        final_grace_remaining: int | None = None
        final_no_tool_mode = False

        reasoning = self._to_provider_reasoning(self.cfg.reasoning)
        tool_names_dbg = [t["function"]["name"] for t in tools] if tools else []
        logger.info(
            f"AgentRunner[{self.provider.name}] 启动 model={self.cfg.model}, "
            f"tools={tool_names_dbg}, refocus={refocus_interval}, "
            f"tool_loop_reminder_interval={reminder_interval}, "
            f"tool_loop_final_warning_count={final_warning_count}, "
            f"tool_loop_final_grace_loops={final_grace_loops}, "
            f"legacy_max_loops={max_loops or self.cfg.max_loops}, "
            f"task_contract={task_contract[:60] if task_contract else None!r}"
        )

        async def append_pending_context() -> bool:
            if pending_context_provider is None:
                return False
            pending = await pending_context_provider()
            if not pending:
                return False
            msgs.extend(pending)
            records.extend(pending)
            logger.info("注入发送回执/新消息上下文 %s 条", len(pending))
            return True

        while True:
            if final_no_tool_mode:
                finish_reason = "tool_loop_finalized"
                break

            loop_count += 1

            if await append_pending_context():
                # 不额外消耗一次模型调用；只是保证下一次调用前上下文已追平。
                pass

            # Task Contract 重注入：每 refocus_interval 轮，在末尾追加焦点提醒
            # 放在 messages 末尾不破坏前缀 KV 缓存
            if (
                task_contract
                and refocus_interval > 0
                and loop_count > 1
                and loop_count % refocus_interval == 0
            ):
                refocus = {
                    "role": "system",
                    "content": (
                        f"[本轮焦点提醒] {task_contract}\n"
                        f"已执行 {loop_count - 1} 轮。检查当前操作是否仍在为这个目标服务，"
                        f"若已完成请用 no_action 收尾。"
                    ),
                }
                msgs.append(refocus)
                records.append(refocus)
                logger.debug(f"Task Contract 重注入（轮次 {loop_count}）")

            if (
                not final_warning_sent
                and tool_loop_reminder_count >= final_warning_count
                and tool_rounds_since_reminder >= max(0, reminder_interval - final_grace_loops)
            ):
                warning = self._build_tool_loop_final_warning(final_grace_loops)
                msgs.append(warning)
                records.append(warning)
                final_warning_sent = True
                final_grace_remaining = final_grace_loops
                if final_grace_remaining <= 0:
                    final_no_tool_mode = True
                    continue
                logger.warning(
                    "工具循环进入最终警告：grace_loops=%s loop=%s",
                    final_grace_remaining,
                    loop_count,
                )

            try:
                self._emit_status(
                    status_callback,
                    state="thinking",
                    text=f"{status_label}思考中",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                result = await self.provider.chat_completion(
                    msgs,
                    model=self.cfg.model,
                    tools=tools,
                    temperature=self.cfg.temperature,
                    top_p=self.cfg.top_p,
                    max_tokens=self.cfg.max_tokens,
                    reasoning=reasoning,
                    stream=True,
                    timeout=self.cfg.first_token_timeout_seconds * 4 + 60.0,
                    first_token_timeout=self.cfg.first_token_timeout_seconds,
                )
            except ProviderTimeoutError as e:
                logger.error(f"AgentRunner 超时（轮次 {loop_count}）: {e}")
                self._emit_status(
                    status_callback,
                    state="error",
                    text=f"{status_label}超时",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                    prompt_tokens=prompt_tokens_total,
                )
            except ProviderError as e:
                logger.error(f"AgentRunner API 错误（轮次 {loop_count}）: {e}")
                self._emit_status(
                    status_callback,
                    state="error",
                    text=f"{status_label}调用失败",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                    prompt_tokens=prompt_tokens_total,
                )
            except Exception as e:
                logger.exception(f"AgentRunner 未知错误（轮次 {loop_count}）: {e}")
                self._emit_status(
                    status_callback,
                    state="error",
                    text=f"{status_label}异常",
                    model=self.cfg.model,
                    loop=loop_count,
                )
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                    prompt_tokens=prompt_tokens_total,
                )

            prompt_tokens_total += result.usage.prompt_tokens
            await self._record_usage(
                usage_recorder,
                result.usage,
                agent=status_label,
                operation="agent_loop",
                loop=loop_count,
                **_kv_prompt_diagnostics(
                    msgs,
                    tools,
                    loop=loop_count,
                    model=self.cfg.model,
                ),
            )
            if result.reasoning_content:
                reasoning_logs.append(result.reasoning_content)

            content_preview = (result.content or "")[:60]
            logger.info(
                f"轮次 {loop_count}: content={content_preview!r}, "
                f"tool_calls={len(result.tool_calls)}"
            )

            # === 分支 1：无工具调用 → 引导重试或终止 ===
            if not result.tool_calls:
                if await append_pending_context():
                    continue
                if no_tool_retry_count <= 0:
                    # 允许一次纠正重试：丢弃纯文本草稿，只插入系统纠正后继续。
                    # 不能把无效 assistant 文本放回上下文，否则下一轮可能把
                    # 内部分析/RAG 解释原样当作可发送消息。
                    no_tool_retry_count += 1
                    err = {
                        "role": "system",
                        "content": (
                            "错误：未调用工具。必须调用 send_* 发消息或 no_action 不操作。"
                            "上一轮纯文本已被系统丢弃，不要复述、转发或引用它。"
                        ),
                    }
                    msgs.append(err)
                    records.append(err)
                    continue
                # 重试后仍未调用工具，丢弃文本
                final_content = ""
                finish_reason = "no_tool_after_retry"
                break

            no_tool_retry_count = 0

            assistant_record = self._build_assistant_record(result)
            msgs.append(assistant_record)
            records.append(assistant_record)

            # === 分支 2：执行所有工具调用 ===
            self._emit_status(
                status_callback,
                state="tool",
                text=f"调用工具：{', '.join(tc.name for tc in result.tool_calls)}",
                model=self.cfg.model,
                loop=loop_count,
                tool_names=[tc.name for tc in result.tool_calls],
            )
            tc_results = await self._execute_tools(result.tool_calls, tool_executor)

            stop_results = [
                r for r in tc_results if r["result"].get("stop_after_tool")
            ]
            no_action_finished = any(
                r["name"] == "no_action"
                and not self._tool_result_blocks_completion(r["result"])
                for r in tc_results
            )
            blocked_by_result = any(
                self._tool_result_blocks_completion(r["result"]) for r in tc_results
            )
            finish_after_success = (
                not blocked_by_result
                and self._all_non_no_action_results_allow_completion(tc_results)
            )
            normal_finish = bool(stop_results or no_action_finished or finish_after_success)

            append_final_warning_after_tool_records = False
            if not normal_finish:
                tool_rounds_since_reminder += 1
                if final_grace_remaining is not None:
                    final_grace_remaining -= 1
                    if final_grace_remaining <= 0:
                        final_no_tool_mode = True
                elif (
                    final_grace_loops <= 0
                    and not final_warning_sent
                    and tool_loop_reminder_count >= final_warning_count
                    and tool_rounds_since_reminder >= reminder_interval
                ):
                    final_warning_sent = True
                    final_grace_remaining = 0
                    final_no_tool_mode = True
                    append_final_warning_after_tool_records = True
                if (
                    not final_warning_sent
                    and tool_rounds_since_reminder >= reminder_interval
                ):
                    tool_loop_reminder_count += 1
                    tool_rounds_since_reminder = 0
                    self._append_loop_reminder(
                        tc_results,
                        reminder_interval=reminder_interval,
                        reminder_count=tool_loop_reminder_count,
                        final_warning_count=final_warning_count,
                    )

            for tcr in tc_results:
                tool_record = {
                    "role": "tool",
                    "tool_call_id": tcr["id"],
                    "content": json.dumps(tcr["result"], ensure_ascii=False),
                }
                msgs.append(tool_record)
                records.append(tool_record)

            if append_final_warning_after_tool_records:
                warning = self._build_tool_loop_final_warning(final_grace_loops)
                msgs.append(warning)
                records.append(warning)
                logger.warning(
                    "工具循环进入最终警告：grace_loops=%s loop=%s",
                    final_grace_remaining,
                    loop_count,
                )

            if stop_results:
                final_content = result.content or ""
                finish_reason = "tool_stop"
                break

            if await append_pending_context():
                continue

            # === 终止条件检查 ===
            if no_action_finished:
                final_content = "NO_ACTIONS"
                finish_reason = "no_action"
                break

            if blocked_by_result:
                continue

            if finish_after_success:
                final_content = result.content or ""
                finish_reason = "finish_after_success"
                break

            if final_no_tool_mode:
                finish_reason = "tool_loop_finalized"
                break

        if finish_reason == "tool_loop_finalized":
            logger.warning("工具循环最终宽限已用完，进入无工具收尾")
            stop_record = self._build_tool_loop_stop()
            msgs.append(stop_record)
            records.append(stop_record)
            final_result = await self._finalize_tool_loop(
                msgs,
                usage_recorder=usage_recorder,
                status_callback=status_callback,
                status_label=status_label,
            )
            if final_result is not None:
                prompt_tokens_total += final_result.usage.prompt_tokens
                if final_result.reasoning_content:
                    reasoning_logs.append(final_result.reasoning_content)
                final_record = self._build_assistant_record(final_result)
                records.append(final_record)
                final_content = (final_result.content or "").strip()
            if not final_content:
                final_content = self._last_assistant_text(records)

        self._emit_status(
            status_callback,
            state="idle",
            text="空闲",
            model=self.cfg.model,
            loop=loop_count,
            finish_reason=finish_reason,
        )
        return AgentRunResult(
            final_content=final_content,
            records=records,
            loop_count=loop_count,
            finish_reason=finish_reason,
            reasoning_logs=reasoning_logs,
            prompt_tokens=prompt_tokens_total,
        )

    # ============================================================
    # 内部辅助
    # ============================================================

    @staticmethod
    def _to_provider_reasoning(
        cfg: CfgReasoning | None,
    ) -> ReasoningConfig | None:
        if cfg is None:
            return None
        return ReasoningConfig(
            enabled=cfg.enabled,
            budget=cfg.budget,
            max_tokens=cfg.max_tokens,
        )

    @staticmethod
    def _build_assistant_record(result: CompletionResult) -> dict[str, Any]:
        record: dict[str, Any] = {
            "role": "assistant",
            "content": result.content or "",
        }
        if result.reasoning_content or result.reasoning_blocks:
            record["reasoning_content"] = result.reasoning_content
        if result.reasoning_blocks:
            record["reasoning_blocks"] = result.reasoning_blocks
        if result.tool_calls:
            record["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in result.tool_calls
            ]
        return record

    @staticmethod
    def _last_assistant_text(records: list[dict[str, Any]]) -> str:
        for record in reversed(records):
            if record.get("role") == "assistant":
                content = str(record.get("content") or "").strip()
                if content:
                    return content
        return ""

    async def _record_usage(
        self,
        recorder: UsageRecorder | None,
        usage,
        **metadata: Any,
    ) -> None:
        if recorder is None:
            return
        try:
            await recorder(
                usage,
                {
                    "provider": self.provider.name,
                    "model": self.cfg.model,
                    **metadata,
                },
            )
        except Exception:
            logger.debug("记录模型用量失败", exc_info=True)

    @staticmethod
    def _append_loop_reminder(
        tc_results: list[dict[str, Any]],
        *,
        reminder_interval: int,
        reminder_count: int,
        final_warning_count: int,
    ) -> None:
        if not tc_results:
            return
        result = dict(tc_results[-1]["result"])
        result["loop_reminder"] = {
            "level": "reminder",
            "message": LOOP_REMINDER_MESSAGE,
            "tool_loop_reminder_interval": reminder_interval,
            "reminder_count": reminder_count,
            "final_warning_count": final_warning_count,
        }
        tc_results[-1]["result"] = result

    @staticmethod
    def _build_tool_loop_final_warning(grace_loops: int) -> dict[str, Any]:
        return {
            "role": "user",
            "content": (
                '<tool_loop_final_warning priority="high">\n'
                "系统提示：工具调用轮数过多，即将结束循环。\n"
                f"你还有 {grace_loops} 轮工具调用机会。\n"
                "请停止反复尝试同一错误，利用剩余机会完成必要操作、汇报当前结果并收尾。\n"
                "</tool_loop_final_warning>"
            ),
        }

    @staticmethod
    def _build_tool_loop_stop() -> dict[str, Any]:
        return {
            "role": "user",
            "content": (
                '<tool_loop_stop priority="high">\n'
                "系统提示：工具调用机会已用完。\n"
                "请不要再调用工具。基于已有结果完成最终汇报、说明未完成原因，"
                "或在无需回复时结束。\n"
                "</tool_loop_stop>"
            ),
        }

    async def _finalize_tool_loop(
        self,
        messages: list[dict[str, Any]],
        *,
        usage_recorder: UsageRecorder | None,
        status_callback: StatusCallback | None,
        status_label: str,
    ) -> CompletionResult | None:
        """工具循环最终宽限用尽后做一次无工具收尾。

        这一步不再传 tools，避免模型继续循环；结果只作为记录和上层 fallback 使用。
        """
        try:
            self._emit_status(
                status_callback,
                state="thinking",
                text=f"{status_label}整理部分结果",
                model=self.cfg.model,
            )
            result = await self.provider.chat_completion(
                messages,
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=min(int(self.cfg.max_tokens or 2048), 4096),
                reasoning=self._to_provider_reasoning(self.cfg.reasoning),
                stream=True,
                timeout=self.cfg.first_token_timeout_seconds * 4 + 60.0,
                first_token_timeout=self.cfg.first_token_timeout_seconds,
            )
            await self._record_usage(
                usage_recorder,
                result.usage,
                agent=status_label,
                operation="agent_loop_tool_loop_final",
                **_kv_prompt_diagnostics(
                    messages,
                    None,
                    loop=0,
                    model=self.cfg.model,
                ),
            )
            return result
        except Exception as e:
            logger.warning("AgentRunner 工具循环无工具收尾失败: %s", e)
            self._emit_status(
                status_callback,
                state="error",
                text=f"{status_label}收尾失败",
                model=self.cfg.model,
            )
            return None

    @staticmethod
    def _emit_status(callback: StatusCallback | None, **payload: Any) -> None:
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            logger.debug("更新模型状态失败", exc_info=True)

    async def _execute_tools(
        self,
        tool_calls: list[ToolCall],
        executor: ToolExecutor,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for tc in tool_calls:
            name = tc.name
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
                if not isinstance(args, dict):
                    raise ValueError(f"工具参数必须是对象: {tc.arguments!r}")
            except (json.JSONDecodeError, ValueError) as e:
                result = {"ok": False, "error": f"参数解析失败: {e}"}
                results.append({"id": tc.id, "name": name, "args": {}, "result": result})
                continue

            try:
                if _executor_accepts_tool_call_id(executor):
                    result = await executor(name, args, tool_call_id=tc.id)
                else:
                    result = await executor(name, args)
                if not isinstance(result, dict):
                    result = {"ok": True, "value": result}
            except Exception as e:
                logger.exception(f"工具 {name} 执行失败: {e}")
                result = {"ok": False, "error": str(e)}

            result = self._maybe_mark_turn_completion(name, args, result)
            results.append({"id": tc.id, "name": name, "args": args, "result": result})
        return results

    @staticmethod
    def _maybe_mark_turn_completion(
        name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "no_action" or args.get("finish_after_success") is not True:
            return result
        if AgentRunner._tool_result_blocks_completion(result):
            return result
        marked = dict(result)
        completion = dict(marked.get("turn_completion") or {})
        completion["allowed"] = True
        completion.setdefault("reason", "finish_after_success")
        marked["turn_completion"] = completion
        return marked

    @staticmethod
    def _tool_result_blocks_completion(result: dict[str, Any]) -> bool:
        if result.get("ok") is False:
            return True
        if result.get("errors"):
            return True
        status = result.get("status")
        pending_statuses = {
            "needs_review",
            "needs_review_again",
            "stale",
            "failed",
            "partial",
            "unsupported",
            "need_tool_search",
        }
        if isinstance(status, str):
            return status in pending_statuses
        if isinstance(status, (list, tuple, set)):
            return any(str(item) in pending_statuses for item in status)
        return False

    @staticmethod
    def _all_non_no_action_results_allow_completion(
        tc_results: list[dict[str, Any]],
    ) -> bool:
        non_no_action = [r for r in tc_results if r["name"] != "no_action"]
        if not non_no_action:
            return False
        return all(
            r["result"].get("turn_completion", {}).get("allowed") is True
            for r in non_no_action
        )


def _kv_prompt_diagnostics(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    loop: int,
    model: str = "",
) -> dict[str, Any]:
    """生成轻量 KV 诊断信息，只记录结构和哈希，不记录正文。"""
    normalized = normalize_messages(messages)
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    roles = [str(m.get("role") or "") for m in normalized]
    joined_content = "\n".join(str(m.get("content") or "") for m in normalized)
    role_char_counts: dict[str, int] = {}
    for message in normalized:
        role = str(message.get("role") or "")
        role_char_counts[role] = role_char_counts.get(role, 0) + len(
            str(message.get("content") or "")
        )
    tools_text = json.dumps(
        tools or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    tool_schema_modes = _tool_schema_mode_counts(tools or [])
    diag: dict[str, Any] = {
        "kv_loop": int(loop),
        "kv_message_count": len(normalized),
        "kv_roles_hash": _short_hash("|".join(roles)),
        "kv_system_count": sum(1 for role in roles if role == "system"),
        "kv_assistant_count": sum(1 for role in roles if role == "assistant"),
        "kv_tool_count": sum(1 for role in roles if role == "tool"),
        "kv_user_count": sum(1 for role in roles if role == "user"),
        "kv_content_char_count": len(joined_content),
        "kv_serialized_char_count": len(serialized),
        "kv_system_char_count": role_char_counts.get("system", 0),
        "kv_user_char_count": role_char_counts.get("user", 0),
        "kv_assistant_char_count": role_char_counts.get("assistant", 0),
        "kv_tool_char_count": role_char_counts.get("tool", 0),
        "kv_tools_count": len(tools or []),
        "kv_tools_hash": _short_hash(tools_text),
        "kv_tools_char_count": len(tools_text),
        "kv_tools_full_count": tool_schema_modes["full"],
        "kv_tools_stub_count": tool_schema_modes["stub"],
        "kv_prefix_8k_hash": _short_hash(serialized[:8192]),
        "kv_prefix_16k_hash": _short_hash(serialized[:16384]),
        "kv_prefix_24k_hash": _short_hash(serialized[:24576]),
        "kv_has_send_receipt": "<send_receipt" in joined_content,
        "kv_send_receipt_block_count": joined_content.count("<send_receipt"),
        "kv_send_receipt_char_count": _tagged_block_char_count(
            joined_content,
            "send_receipt",
        ),
        "kv_task_context_block_count": joined_content.count("<task_context"),
        "kv_task_context_char_count": _tagged_block_char_count(
            joined_content,
            "task_context",
        ),
        "kv_has_recent_group_messages": "<recent_group_messages" in joined_content,
        "kv_recent_group_message_line_count": joined_content.count(" msg_id="),
        "kv_has_rag": "<retrieved_conversation_context" in joined_content,
        "kv_rag_block_count": joined_content.count("<retrieved_conversation_context"),
        "kv_rag_char_count": _tagged_block_char_count(
            joined_content,
            "retrieved_conversation_context",
        ),
    }
    return diag


def _short_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _tool_schema_mode_counts(tools: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"full": 0, "stub": 0}
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        parameters = function.get("parameters") if isinstance(function, dict) else None
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if isinstance(properties, dict) and "_tool_search_required" in properties:
            counts["stub"] += 1
        else:
            counts["full"] += 1
    return counts


def _tagged_block_char_count(text: str, tag_name: str) -> int:
    pattern = re.compile(
        rf"<{re.escape(tag_name)}(?:\s[^>]*)?>.*?</{re.escape(tag_name)}>",
        re.DOTALL,
    )
    return sum(len(match.group(0)) for match in pattern.finditer(text))


def _executor_accepts_tool_call_id(executor: ToolExecutor) -> bool:
    try:
        signature = inspect.signature(executor)
    except (TypeError, ValueError):
        return False
    positional_count = 0
    for param in signature.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "tool_call_id":
            return True
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_count += 1
    return positional_count >= 3
