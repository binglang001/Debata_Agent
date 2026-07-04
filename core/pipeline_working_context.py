"""Working-context helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change token-budget, memory, RAG, or working-history
selection logic while moving methods.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from utils.token_budget import TokenBudget, TokenEstimator

from .pipeline_context import _recommended_context_budget

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MainPromptBudgetResult:
    ok: bool
    messages: list[dict[str, Any]]
    estimated_tokens: int
    budget_tokens: int
    pre_summary_status: str = ""
    attempts: list[str] = field(default_factory=list)
    failure_reason: str = ""


class PipelineWorkingContextMixin:
    def _context_budget(self) -> TokenBudget:
        cfg = self.behavior_cfg.context
        model = getattr(self.chat_agent.cfg, "model", "")
        max_context = cfg.max_context_tokens or _recommended_context_budget(
            model,
            self.model_context_length,
        )
        return TokenBudget(
            max_context_tokens=max_context,
            reserve_output_tokens=cfg.reserve_output_tokens,
            memory_token_budget=cfg.memory_token_budget,
            summary_token_budget=cfg.summary_token_budget,
        )

    def _token_estimator(self) -> TokenEstimator:
        return TokenEstimator(
            model=getattr(self.chat_agent.cfg, "model", ""),
            calib_ratio=self._token_calib_ratio,
        )

    def _calibrate_tokens(self, estimated: int, actual_prompt_tokens: int) -> None:
        if estimated <= 0 or actual_prompt_tokens <= 0:
            return
        estimator = self._token_estimator()
        estimator.update_calibration(estimated, actual_prompt_tokens)
        self._token_calib_ratio = estimator.calib_ratio
        logger.debug(
            "token 估算校准：estimated=%s actual=%s ratio=%.3f",
            estimated,
            actual_prompt_tokens,
            self._token_calib_ratio,
        )

    def _rolling_summary_text(self, estimator: TokenEstimator | None = None) -> str:
        if self.rolling_summary is None:
            return ""
        text = self.rolling_summary.text()
        if not text:
            return ""
        estimator = estimator or self._token_estimator()
        return self._trim_text_to_token_budget(
            text,
            self._context_budget().summary_token_budget,
            estimator,
        )

    async def _important_memory_text(
        self,
        conversation_id: str | None,
        *,
        token_budget: int | None = None,
        member_qqs: set[int] | None = None,
    ) -> str:
        """按当前会话选择重要记忆注入文本，不受 RAG 开关影响。"""
        estimator = self._token_estimator()
        budget = token_budget or self._context_budget().memory_token_budget
        return self.important.text_for_context(
            conversation_id,
            token_budget=budget,
            estimator=estimator,
            member_qqs=member_qqs,
        )

    async def _rag_context_text(
        self,
        conversation_id: str | None,
        *,
        query: str | None,
        before_ts: str | None = None,
        token_budget: int | None = None,
    ) -> str:
        """按当前 query 检索历史对话片段。RAG 关闭或不可用时返回空。"""
        if (
            self.features_cfg.long_term_memory.mode != "rag"
            or self.rag_memory is None
            or not query
        ):
            return ""
        estimator = self._token_estimator()
        budget = token_budget or self._context_budget().memory_token_budget
        return await self.rag_memory.retrieve_for_query(
            query,
            conversation_id=conversation_id,
            before_ts=before_ts,
            top_k=self.features_cfg.long_term_memory.rag_top_k,
            token_budget=budget,
            estimator=estimator,
        )

    @staticmethod
    def _trim_text_to_token_budget(
        text: str,
        budget: int,
        estimator: TokenEstimator,
    ) -> str:
        if not text or estimator.estimate_text(text) <= budget:
            return text
        marker = "\n...[滚动摘要因上下文预算截断]...\n"
        marker_cost = estimator.estimate_text(marker)
        if budget <= marker_cost + 16:
            return text[: max(1, budget * 2)]

        head_budget = max(1, (budget - marker_cost) // 2)
        tail_budget = max(1, budget - marker_cost - head_budget)

        def fit_prefix(limit: int) -> str:
            lo, hi = 0, len(text)
            best = ""
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = text[:mid]
                if estimator.estimate_text(candidate) <= limit:
                    best = candidate
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best.rstrip()

        def fit_suffix(limit: int) -> str:
            lo, hi = 0, len(text)
            best = ""
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = text[len(text) - mid :]
                if estimator.estimate_text(candidate) <= limit:
                    best = candidate
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best.lstrip()

        return f"{fit_prefix(head_budget)}{marker}{fit_suffix(tail_budget)}"

    async def _select_working_history(
        self,
        conversation_id: str | None,
    ) -> list[dict[str, Any]]:
        """返回 rolling summary 游标之后的完整活跃时间线。"""
        _ = conversation_id
        return await self._active_history_records()

    async def _select_proactive_router_history(self) -> list[dict[str, Any]]:
        """主动路由专用小窗口；真正行动轮仍使用正常工作窗口。"""
        records = await self._active_history_records()
        return self._select_recent_records_for_budget(
            records,
            working_budget=min(
                self._working_history_budget(),
                self.behavior_cfg.proactive_router_history_token_budget,
            ),
        )

    async def _active_history_records(self) -> list[dict[str, Any]]:
        records = await self.history.records()
        active_start = self._active_start_index_for_prompt(records)
        return records[active_start:]

    def _active_start_index_for_prompt(self, records: list[dict[str, Any]]) -> int:
        if self.rolling_summary is None:
            return 0
        helper = getattr(self, "_active_start_index_for_records", None)
        if callable(helper):
            return helper(records)
        return max(0, min(self.rolling_summary.active_start_index(), len(records)))

    def _working_history_budget(self) -> int:
        budget = self._context_budget()
        context_cfg = self.behavior_cfg.context
        return max(
            1,
            budget.total_input_budget
            - budget.memory_token_budget
            - budget.summary_token_budget
            - context_cfg.prompt_overhead_estimate_tokens,
        )

    def _warn_context_compaction_invariants(self) -> None:
        working_budget = self._working_history_budget()
        summarize = self.behavior_cfg.summarize
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            logger.warning(
                "未启用滚动摘要/归档压缩；长会话超过主模型输入预算后会显式跳过模型调用"
            )
            return
        trigger = summarize.trigger_at_tokens
        if trigger is None:
            trigger = int(
                self._context_budget().max_context_tokens
                * summarize.trigger_at_context_percent
                / 100
            )
        if trigger >= working_budget:
            logger.warning(
                "滚动摘要触发线高于活跃记录预算：trigger=%s working_budget=%s；"
                "长会话可能先触发主模型预算预检失败",
                trigger,
                working_budget,
            )

    def _estimate_main_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        estimator: TokenEstimator | None = None,
    ) -> int:
        estimator = estimator or self._token_estimator()
        estimated = estimator.estimate_messages(messages)
        if tools_schema:
            estimated += estimator.estimate_text(str(tools_schema))
        return estimated

    async def _prepare_main_prompt_for_model(
        self,
        *,
        conversation_id: str | None,
        phase: str,
        tools_schema: list[dict[str, Any]] | None,
        rebuild_messages: Callable[[], Awaitable[list[dict[str, Any]]]],
    ) -> MainPromptBudgetResult:
        pre_summary = await self._maybe_summarize(reason=f"{phase}_preflight")
        estimator = self._token_estimator()
        budget_tokens = self._context_budget().total_input_budget
        messages = await rebuild_messages()
        estimated = self._estimate_main_prompt_tokens(messages, tools_schema, estimator)
        if estimated <= budget_tokens:
            return MainPromptBudgetResult(
                ok=True,
                messages=messages,
                estimated_tokens=estimated,
                budget_tokens=budget_tokens,
                pre_summary_status=pre_summary.status,
            )

        attempts: list[str] = []
        first_target = await self._first_budget_retry_target(estimator)
        if first_target is not None:
            first = await self._compact_active_history(
                target_after_tokens=first_target,
                reason=f"{phase}_budget_first",
            )
            attempts.append(
                f"first:{first.status}:{first.reason}:archived={first.archived_count}"
            )
            messages = await rebuild_messages()
            estimated = self._estimate_main_prompt_tokens(messages, tools_schema, estimator)
            if first.success and estimated <= budget_tokens:
                return MainPromptBudgetResult(
                    ok=True,
                    messages=messages,
                    estimated_tokens=estimated,
                    budget_tokens=budget_tokens,
                    pre_summary_status=pre_summary.status,
                    attempts=attempts,
                )
            retry_target = await self._retry_budget_target(
                estimator,
                first_target=first_target,
            )
        else:
            attempts.append("first:failed:no_compressible_active_history")
            retry_target = await self._retry_budget_target(
                estimator,
                first_target=None,
            )

        retry = await self._compact_active_history(
            target_after_tokens=retry_target,
            reason=f"{phase}_budget_retry_config_target",
        )
        attempts.append(
            f"retry:{retry.status}:{retry.reason}:archived={retry.archived_count}"
        )
        messages = await rebuild_messages()
        estimated = self._estimate_main_prompt_tokens(messages, tools_schema, estimator)
        if retry.success and estimated <= budget_tokens:
            return MainPromptBudgetResult(
                ok=True,
                messages=messages,
                estimated_tokens=estimated,
                budget_tokens=budget_tokens,
                pre_summary_status=pre_summary.status,
                attempts=attempts,
            )

        await self._record_main_prompt_budget_failure(
            conversation_id=conversation_id,
            phase=phase,
            estimated_tokens=estimated,
            budget_tokens=budget_tokens,
            attempts=attempts,
        )
        return MainPromptBudgetResult(
            ok=False,
            messages=messages,
            estimated_tokens=estimated,
            budget_tokens=budget_tokens,
            pre_summary_status=pre_summary.status,
            attempts=attempts,
            failure_reason="over_budget_after_retry",
        )

    async def _first_budget_retry_target(
        self,
        estimator: TokenEstimator,
    ) -> int | None:
        active = await self._active_history_records()
        if len(active) <= 1:
            return None
        active_tokens = estimator.estimate_messages(active)
        return self._summary_target_after_tokens(active_tokens=active_tokens)

    async def _retry_budget_target(
        self,
        estimator: TokenEstimator,
        *,
        first_target: int | None,
    ) -> int:
        active = await self._active_history_records()
        if not active:
            return 1
        active_tokens = estimator.estimate_messages(active)
        if active_tokens <= 1:
            return 1
        retry_percent = self.behavior_cfg.summarize.retry_target_after_context_percent
        retry_target = int(
            self._context_budget().max_context_tokens
            * retry_percent
            / 100
        )
        retry_target = max(1, min(retry_target, active_tokens - 1))
        if first_target is not None:
            retry_target = min(retry_target, max(1, first_target))
        return retry_target

    async def _record_main_prompt_budget_failure(
        self,
        *,
        conversation_id: str | None,
        phase: str,
        estimated_tokens: int,
        budget_tokens: int,
        attempts: list[str],
    ) -> None:
        note = (
            "[上下文预算] 主模型输入预检失败，已跳过本轮模型调用；"
            f"phase={phase}，estimated={estimated_tokens} tokens，"
            f"budget={budget_tokens} tokens，attempts={'; '.join(attempts)}。"
        )
        logger.error(note)
        await self.history.add_system_note(
            note,
            conversation_id=conversation_id or "system:context_budget",
        )

    def _select_recent_records_for_budget(
        self,
        records: list[dict[str, Any]],
        *,
        working_budget: int,
    ) -> list[dict[str, Any]]:
        """从活跃记录尾部按 token 预算取最近后缀。"""
        if not records:
            return []
        estimator = self._token_estimator()
        selected: list[dict[str, Any]] = []
        used = 0
        for record in reversed(records):
            cost = estimator.estimate_messages([record])
            if selected and used + cost > working_budget:
                break
            selected.append(record)
            used += cost
        return list(reversed(selected))
