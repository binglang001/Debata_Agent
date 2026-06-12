"""AgentRunner —— 通用的多轮工具循环执行框架。

替代旧版 llm_client.run_agent_loop。差异：
    - 直接面向 IProvider（不再绑死 DeepSeek）
    - 工具执行通过外部注入的 executor，避免循环依赖
    - 显式记录 reasoning 过程，方便 UI 可视化
    - finish_reason 结构化（替代旧版 final 字符串判定）

循环退出条件（保留 V1 语义）：
    1. AI 调用了 no_action → finish_reason='no_action'
    2. AI 全部调用 send_* 且 send_only=True 且全部成功 → 'send_only_complete'
    3. AI 调用的全是 no_feedback 类工具且全部成功 → 'all_no_feedback'
    4. AI 未调用工具，提示重试后仍不调用 → 'no_tool_after_retry'
    5. 达到 max_loops → 'max_loops'，随后追加一次无工具收尾，让模型说明部分结果
"""

from __future__ import annotations

import hashlib
import json
import logging
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


# 工具属于"调用成功后无需 LLM 反馈"的类别——它们的结果对后续没影响，
# 调用完直接结束循环
DEFAULT_NO_FEEDBACK_TOOLS: set[str] = {
    "save_important_memory",
    "delete_important_memory",
    "no_action",
    "set_friend_add_request",
    "set_group_add_request",
    "schedule_wakeup",
}

# 发送类工具：send_only=True 时同样算作终止信号
SEND_TOOL_NAMES: set[str] = {
    "send_private_messages",
    "send_group_message",
    "send_voice_message",
}


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
        finish_reason: FinishReason = "max_loops"
        loop_count = 0
        prompt_tokens_total = 0
        effective_max_loops = max(1, int(max_loops or self.cfg.max_loops))
        refocus_interval = self.cfg.refocus_interval
        has_pending_send_actions = False

        reasoning = self._to_provider_reasoning(self.cfg.reasoning)
        tool_names_dbg = [t["function"]["name"] for t in tools] if tools else []
        logger.info(
            f"AgentRunner[{self.provider.name}] 启动 model={self.cfg.model}, "
            f"tools={tool_names_dbg}, refocus={refocus_interval}, "
            f"max_loops={effective_max_loops}, "
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

        while loop_count < effective_max_loops:
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
                        f"若已完成请用 send_only=true 或 no_action 收尾。"
                    ),
                }
                msgs.append(refocus)
                records.append(refocus)
                logger.debug(f"Task Contract 重注入（轮次 {loop_count}）")

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
                if await append_pending_context() and loop_count < effective_max_loops:
                    continue
                content = (result.content or "").strip()
                if has_pending_send_actions:
                    final_content = content
                    finish_reason = "send_only_complete"
                    break
                if loop_count < effective_max_loops:
                    # 还有下一轮：丢弃纯文本草稿，只插入系统纠正后继续。
                    # 不能把无效 assistant 文本放回上下文，否则下一轮可能把
                    # 内部分析/RAG 解释原样当作可发送消息。
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
                # 最后一次仍未调用工具，丢弃文本
                final_content = ""
                finish_reason = "no_tool_after_retry"
                break

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
            if any(
                r["name"] in SEND_TOOL_NAMES and r["result"].get("ok", True)
                for r in tc_results
            ):
                has_pending_send_actions = True
            for tcr in tc_results:
                tool_record = {
                    "role": "tool",
                    "tool_call_id": tcr["id"],
                    "content": json.dumps(tcr["result"], ensure_ascii=False),
                }
                msgs.append(tool_record)
                records.append(tool_record)

            stop_results = [
                r for r in tc_results if r["result"].get("stop_after_tool")
            ]
            if stop_results:
                final_content = result.content or ""
                finish_reason = "tool_stop"
                break

            if await append_pending_context() and loop_count < effective_max_loops:
                continue

            # === 终止条件检查 ===
            if any(
                r["name"] == "no_action" and r["result"].get("ok", True)
                for r in tc_results
            ):
                final_content = "NO_ACTIONS"
                finish_reason = "no_action"
                break

            if self._all_no_feedback(tc_results):
                final_content = result.content or ""
                finish_reason = self._classify_no_feedback(tc_results)
                break

        if loop_count >= effective_max_loops and finish_reason == "max_loops":
            logger.warning(f"达到最大循环次数 {effective_max_loops}")
            limit_record = {
                "role": "system",
                "content": (
                    f"工具循环达到上限 {effective_max_loops} 轮，"
                    "现在禁止继续调用工具。请基于已有消息和工具结果，"
                    "用自然语言给出当前部分结果、已完成事项、未完成原因和建议下一步。"
                ),
            }
            msgs.append(limit_record)
            records.append(limit_record)
            final_result = await self._finalize_after_max_loops(
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

    async def _finalize_after_max_loops(
        self,
        messages: list[dict[str, Any]],
        *,
        usage_recorder: UsageRecorder | None,
        status_callback: StatusCallback | None,
        status_label: str,
    ) -> CompletionResult | None:
        """工具轮数用尽后做一次无工具收尾。

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
                operation="agent_loop_max_loops_final",
                **_kv_prompt_diagnostics(
                    messages,
                    None,
                    loop=0,
                    model=self.cfg.model,
                ),
            )
            return result
        except Exception as e:
            logger.warning("AgentRunner 达到上限后的无工具收尾失败: %s", e)
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
                result = await executor(name, args)
                if not isinstance(result, dict):
                    result = {"ok": True, "value": result}
            except Exception as e:
                logger.exception(f"工具 {name} 执行失败: {e}")
                result = {"ok": False, "error": str(e)}

            results.append({"id": tc.id, "name": name, "args": args, "result": result})
        return results

    def _all_no_feedback(self, tc_results: list[dict[str, Any]]) -> bool:
        """所有工具调用都不需要 LLM 反馈才能终止。"""
        for r in tc_results:
            name = r["name"]
            ok = r["result"].get("ok", True)
            if name in SEND_TOOL_NAMES:
                if name == "send_voice_message":
                    if not ok:
                        return False
                elif not r["args"].get("send_only", False) or not ok:
                    return False
            elif name in self.no_feedback_tools:
                if not ok:
                    return False
            else:
                return False
        return True

    @staticmethod
    def _classify_no_feedback(tc_results: list[dict[str, Any]]) -> FinishReason:
        for r in tc_results:
            if r["name"] in SEND_TOOL_NAMES:
                return "send_only_complete"
        return "all_no_feedback"


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
    tools_text = json.dumps(
        tools or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    diag: dict[str, Any] = {
        "kv_loop": int(loop),
        "kv_message_count": len(normalized),
        "kv_roles_hash": _short_hash("|".join(roles)),
        "kv_system_count": sum(1 for role in roles if role == "system"),
        "kv_assistant_count": sum(1 for role in roles if role == "assistant"),
        "kv_tool_count": sum(1 for role in roles if role == "tool"),
        "kv_tools_count": len(tools or []),
        "kv_tools_hash": _short_hash(tools_text),
        "kv_prefix_8k_hash": _short_hash(serialized[:8192]),
        "kv_prefix_16k_hash": _short_hash(serialized[:16384]),
        "kv_prefix_24k_hash": _short_hash(serialized[:24576]),
        "kv_has_send_receipt": "<send_receipt" in joined_content,
        "kv_has_recent_group_messages": "<recent_group_messages" in joined_content,
        "kv_has_rag": "<retrieved_conversation_context" in joined_content,
    }
    return diag


def _short_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
