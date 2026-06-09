"""Working-context helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change token-budget, memory, RAG, or working-history
selection logic while moving methods.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from utils.token_budget import TokenBudget, TokenEstimator

from .pipeline_context import _recommended_context_budget
from .pipeline_history import (
    _record_conversation_id,
    _working_history_noise_indices,
    _working_history_optional_runtime_indices,
)

logger = logging.getLogger(__name__)

_SEND_RECEIPT_BLOCK_RE = re.compile(
    r"<send_receipt>\s*(.*?)\s*</send_receipt>",
    re.DOTALL,
)
_SEND_RECEIPT_JSON_KEYS = {
    "type",
    "send_id",
    "conversation_id",
    "status",
    "interrupted",
    "sent",
    "unsent",
    "new_messages",
    "recalled_messages",
    "errors",
    "accepted_messages",
}


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
    ) -> str:
        """按当前会话选择重要记忆注入文本，不受 RAG 开关影响。"""
        estimator = self._token_estimator()
        budget = token_budget or self._context_budget().memory_token_budget
        return self.important.text_for_context(
            conversation_id,
            token_budget=budget,
            estimator=estimator,
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
        """按 token 预算选择统一近期时间线。

        conversation_id 只用于保证当前会话最近若干条不被高频群聊挤掉；
        工作窗口本身仍来自同一条全局 history，不按会话过滤。
        """
        records = await self.history.records()
        return self._select_history_records(
            records,
            working_budget=self._working_history_budget(),
            conversation_id=conversation_id,
            ensure_current_records=(
                self.behavior_cfg.context.current_conversation_min_records
            ),
            log_context=conversation_id,
            log_level=logging.INFO,
        )

    async def _select_proactive_router_history(self) -> list[dict[str, Any]]:
        """主动路由专用小窗口；真正行动轮仍使用正常工作窗口。"""
        records = await self.history.records()
        return self._select_history_records(
            records,
            working_budget=min(
                self._working_history_budget(),
                self.behavior_cfg.proactive_router_history_token_budget,
            ),
            conversation_id=None,
            ensure_current_records=0,
            log_context="proactive_router",
            log_level=logging.DEBUG,
        )

    def _working_history_budget(self) -> int:
        budget = self._context_budget()
        context_cfg = self.behavior_cfg.context
        return max(
            context_cfg.min_working_history_tokens,
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
                "未启用滚动摘要/归档压缩；长会话超过工作窗口后会逐条淘汰历史，KV 缓存命中率会下降"
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
                "滚动摘要触发线高于工作窗口预算：trigger=%s working_budget=%s；"
                "长会话可能先发生窗口淘汰，导致 KV 缓存前缀逐轮重建",
                trigger,
                working_budget,
            )

    def _select_history_records(
        self,
        records: list[dict[str, Any]],
        *,
        working_budget: int,
        conversation_id: str | None,
        ensure_current_records: int,
        log_context: str | None,
        log_level: int,
    ) -> list[dict[str, Any]]:
        estimator = self._token_estimator()
        context_cfg = self.behavior_cfg.context
        selected_indices: set[int] = set()
        noise_indices = _working_history_noise_indices(
            records,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
            runtime_record_keep_count=context_cfg.runtime_record_keep_count,
            send_receipt_keep_count=context_cfg.send_receipt_keep_count,
            no_action_keep_count=context_cfg.no_action_keep_count,
        )
        optional_runtime_indices = _working_history_optional_runtime_indices(
            records,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
            runtime_record_keep_count=context_cfg.runtime_record_keep_count,
            send_receipt_keep_count=context_cfg.send_receipt_keep_count,
            no_action_keep_count=context_cfg.no_action_keep_count,
        )
        used = 0

        def add_index(index: int, *, force: bool = False) -> bool:
            nonlocal used
            if index in selected_indices:
                return True
            if not force and index in noise_indices:
                return True
            cost = estimator.estimate_messages([records[index]])
            if not force and selected_indices and used + cost > working_budget:
                return False
            selected_indices.add(index)
            used += cost
            return True

        if conversation_id and ensure_current_records > 0:
            current_indices: list[int] = []
            for idx in range(len(records) - 1, -1, -1):
                if _record_conversation_id(records[idx]) == conversation_id:
                    current_indices.append(idx)
                    if len(current_indices) >= ensure_current_records:
                        break
            for idx in reversed(current_indices):
                add_index(idx, force=True)

        for idx in range(len(records) - 1, -1, -1):
            if idx in selected_indices:
                continue
            if idx in optional_runtime_indices:
                continue
            if not add_index(idx):
                break

        for idx in range(len(records) - 1, -1, -1):
            if idx in selected_indices:
                continue
            if idx not in optional_runtime_indices:
                continue
            if not add_index(idx):
                break

        selected = [records[idx] for idx in sorted(selected_indices)]
        dropped = len(records) - len(selected)
        if dropped > 0:
            logger.log(
                log_level,
                "上下文预算裁剪：view=%s 丢弃活跃区较早记录 %s 条 "
                "(working_budget≈%s tokens, used≈%s tokens)",
                log_context,
                dropped,
                working_budget,
                used,
            )
        filtered = self._filter_working_history_runtime_noise(
            selected,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
            log_context=log_context,
            log_level=log_level,
        )
        return self._normalize_working_history_send_receipts(filtered)

    def _filter_working_history_runtime_noise(
        self,
        records: list[dict[str, Any]],
        *,
        conversation_id: str | None,
        ensure_current_records: int,
        log_context: str | None,
        log_level: int,
    ) -> list[dict[str, Any]]:
        """Drop old runtime-only records from the prompt view, not from history.

        The working window remains a unified cross-conversation timeline. This filter only
        prevents old task snapshots, clean send-status records, and complete
        no_action assistant/tool blocks from being replayed into every model call
        after they are no longer useful for immediate decision-making. Tool
        blocks are dropped only when the full assistant/tool pair is present.
        """
        if not records:
            return records

        context_cfg = self.behavior_cfg.context
        drop_indices = _working_history_noise_indices(
            records,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
            runtime_record_keep_count=context_cfg.runtime_record_keep_count,
            send_receipt_keep_count=context_cfg.send_receipt_keep_count,
            no_action_keep_count=context_cfg.no_action_keep_count,
        )

        if not drop_indices:
            return records

        filtered = [
            record for idx, record in enumerate(records)
            if idx not in drop_indices
        ]
        logger.log(
            log_level,
            "上下文运行时瘦身：view=%s 移除旧运行时记录 %s 条 "
            "(保留当前会话最近 %s 条、全局近期 runtime %s 条、近期 send_receipt %s 条)",
            log_context,
            len(drop_indices),
            ensure_current_records,
            context_cfg.runtime_record_keep_count,
            context_cfg.send_receipt_keep_count,
        )
        return filtered

    def _normalize_working_history_send_receipts(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """只归一化 prompt view 中旧版 JSON 回执，不回写原始历史。"""
        normalized: list[dict[str, Any]] = []
        changed = False
        for record in records:
            content = record.get("content")
            if not isinstance(content, str) or "<send_receipt" not in content:
                normalized.append(record)
                continue
            new_content = self._normalize_send_receipt_content(content, record)
            if new_content == content:
                normalized.append(record)
                continue
            copied = dict(record)
            copied["content"] = new_content
            normalized.append(copied)
            changed = True
        return normalized if changed else records

    def _normalize_send_receipt_content(
        self,
        content: str,
        record: dict[str, Any],
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            receipt = self._parse_legacy_send_receipt_json(match.group(1))
            if receipt is None:
                return match.group(0)
            if not receipt.get("conversation_id"):
                conversation_id = _record_conversation_id(record)
                if conversation_id:
                    receipt = dict(receipt)
                    receipt["conversation_id"] = conversation_id
            return self._format_send_receipt(receipt)

        return _SEND_RECEIPT_BLOCK_RE.sub(replace, content)

    @staticmethod
    def _parse_legacy_send_receipt_json(text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped:
            return None

        decoder = json.JSONDecoder()
        candidates = [stripped]
        first_brace = stripped.find("{")
        if first_brace > 0:
            candidates.append(stripped[first_brace:])

        for candidate in candidates:
            if not candidate.startswith("{"):
                continue
            try:
                payload, end = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if candidate[end:].strip():
                continue
            if not isinstance(payload, dict):
                continue
            if not (_SEND_RECEIPT_JSON_KEYS & payload.keys()):
                continue
            return payload
        return None
