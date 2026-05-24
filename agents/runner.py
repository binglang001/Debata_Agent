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
    5. 达到 max_loops → 'max_loops'
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app_config.schema import AgentConfig, ReasoningConfig as CfgReasoning
from providers.base import (
    CompletionResult,
    IProvider,
    ProviderError,
    ProviderTimeoutError,
    ReasoningConfig,
    ToolCall,
)

from .base import AgentRunResult, FinishReason, ToolExecutor

logger = logging.getLogger(__name__)


# 工具属于"调用成功后无需 LLM 反馈"的类别——它们的结果对后续没影响，
# 调用完直接结束循环
DEFAULT_NO_FEEDBACK_TOOLS: set[str] = {
    "save_important_memory",
    "delete_important_memory",
    "no_action",
    "set_friend_add_request",
    "set_group_add_request",
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
        refocus_interval = self.cfg.refocus_interval

        reasoning = self._to_provider_reasoning(self.cfg.reasoning)
        tool_names_dbg = [t["function"]["name"] for t in tools] if tools else []
        logger.info(
            f"AgentRunner[{self.provider.name}] 启动 model={self.cfg.model}, "
            f"tools={tool_names_dbg}, refocus={refocus_interval}, "
            f"task_contract={task_contract[:60] if task_contract else None!r}"
        )

        while loop_count < self.cfg.max_loops:
            loop_count += 1

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
                result = await self.provider.chat_completion(
                    msgs,
                    model=self.cfg.model,
                    tools=tools,
                    temperature=self.cfg.temperature,
                    top_p=self.cfg.top_p,
                    max_tokens=self.cfg.max_tokens,
                    reasoning=reasoning,
                    stream=True,
                    timeout=self.cfg.first_token_timeout * 4 + 60.0,
                    first_token_timeout=self.cfg.first_token_timeout,
                )
            except ProviderTimeoutError as e:
                logger.error(f"AgentRunner 超时（轮次 {loop_count}）: {e}")
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                )
            except ProviderError as e:
                logger.error(f"AgentRunner API 错误（轮次 {loop_count}）: {e}")
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                )
            except Exception as e:
                logger.exception(f"AgentRunner 未知错误（轮次 {loop_count}）: {e}")
                return AgentRunResult(
                    final_content="",
                    records=records,
                    loop_count=loop_count,
                    finish_reason="api_error",
                    reasoning_logs=reasoning_logs,
                )

            if result.reasoning_content:
                reasoning_logs.append(result.reasoning_content)

            assistant_record = self._build_assistant_record(result)
            msgs.append(assistant_record)
            records.append(assistant_record)
            content_preview = (result.content or "")[:60]
            logger.info(
                f"轮次 {loop_count}: content={content_preview!r}, "
                f"tool_calls={len(result.tool_calls)}"
            )

            # === 分支 1：无工具调用 → 引导重试或终止 ===
            if not result.tool_calls:
                content = (result.content or "").strip()
                if loop_count < self.cfg.max_loops:
                    # 还有下一轮：插入系统纠正后继续
                    err = {
                        "role": "system",
                        "content": (
                            "错误：未调用工具。必须调用 send_* 发消息或 no_action 不操作。"
                            "纯文本无效。"
                        ),
                    }
                    msgs.append(err)
                    records.append(err)
                    continue
                # 最后一次仍未调用工具，丢弃文本
                final_content = content
                finish_reason = "no_tool_after_retry"
                break

            # === 分支 2：执行所有工具调用 ===
            tc_results = await self._execute_tools(result.tool_calls, tool_executor)
            for tcr in tc_results:
                tool_record = {
                    "role": "tool",
                    "tool_call_id": tcr["id"],
                    "content": json.dumps(tcr["result"], ensure_ascii=False),
                }
                msgs.append(tool_record)
                records.append(tool_record)

            # === 终止条件检查 ===
            if any(r["name"] == "no_action" for r in tc_results):
                final_content = "NO_ACTIONS"
                finish_reason = "no_action"
                break

            if self._all_no_feedback(tc_results):
                final_content = result.content or ""
                finish_reason = self._classify_no_feedback(tc_results)
                break

        if loop_count >= self.cfg.max_loops:
            logger.warning(f"达到最大循环次数 {self.cfg.max_loops}")

        return AgentRunResult(
            final_content=final_content,
            records=records,
            loop_count=loop_count,
            finish_reason=finish_reason,
            reasoning_logs=reasoning_logs,
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
        if result.reasoning_content:
            record["reasoning_content"] = result.reasoning_content
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
                if not r["args"].get("send_only", False) or not ok:
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
