"""Summary/compaction helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change summary trigger, archive, rolling-summary, or
important-memory save logic while moving methods.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from utils import get_time
from utils.token_budget import TokenEstimator

from .pipeline_history import _record_timestamp

logger = logging.getLogger(__name__)


class PipelineSummaryMixin:
    def _schedule_summarize(self) -> None:
        """后台触发 compaction，避免在回复热路径同步调用总结模型。"""
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            return
        if self._summary_task is not None and not self._summary_task.done():
            return

        async def _runner() -> None:
            try:
                await self._maybe_summarize()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"总结触发失败（忽略）: {e}")

        self._summary_task = asyncio.create_task(_runner())

    async def _maybe_summarize(self) -> None:
        """按 token 阈值压缩活跃 history：先归档原文，再截出活跃区。"""
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            return

        records = await self.history.records()
        if not records:
            return
        estimator = self._token_estimator()
        active_tokens = estimator.estimate_messages(records)
        budget = self._context_budget()
        summarize_cfg = self.behavior_cfg.summarize
        trigger = summarize_cfg.trigger_at_tokens
        if trigger is None:
            trigger = int(
                budget.max_context_tokens
                * summarize_cfg.trigger_at_context_percent
                / 100
            )
        target_after = summarize_cfg.target_after_tokens
        if target_after is None:
            target_after = int(
                budget.max_context_tokens
                * summarize_cfg.target_after_context_percent
                / 100
            )
        if active_tokens < trigger:
            return
        target_after = max(1, min(target_after, active_tokens - 1))

        logger.info(
            "活跃历史达 %s tokens ≥ %s，触发滚动摘要 compaction",
            active_tokens,
            trigger,
        )

        slice_records = self._select_compaction_slice(
            records,
            active_tokens=active_tokens,
            target_after_tokens=target_after,
            estimator=estimator,
        )
        if not slice_records:
            logger.warning("compaction 未选出可归档切片，跳过")
            return

        important_text = self.important.text()
        result = await self.summary_agent.summarize_rolling(
            slice_records,
            self.rolling_summary.text(),
            important_text,
        )
        if not result:
            logger.warning("滚动摘要 Agent 返回 None，跳过本次截断")
            return

        summary_text = self._trim_text_to_token_budget(
            str(result.get("summary_text") or "").strip(),
            budget.summary_token_budget,
            estimator,
        )
        new_important_items = result.get("new_important", [])
        if not summary_text:
            logger.warning("滚动摘要为空，跳过本次截断")
            return

        await self.archive.append_many(slice_records)
        await self.rolling_summary.update(
            summary_text,
            archived_until={
                "last_compaction_count": len(slice_records),
                "last_timestamp": _record_timestamp(slice_records[-1]),
            },
            updated_at=get_time(),
        )
        saved_important_count = 0
        if isinstance(new_important_items, list) and new_important_items:
            for item in new_important_items:
                content = (item.get("content") or "").strip() if isinstance(item, dict) else ""
                if content:
                    scope = item.get("scope") if isinstance(item.get("scope"), str) else None
                    pinned = item.get("pinned") if isinstance(item.get("pinned"), bool) else False
                    save_result = await self.important.save(
                        content,
                        scope=scope,
                        pinned=pinned,
                    )
                    if save_result.get("saved"):
                        saved_important_count += 1

        cut_point = len(slice_records)
        await self.history.truncate_head(cut_point)

        await self.history.add_system_note(
            f"[滚动摘要] 已归档并移出活跃历史 {cut_point} 条；"
            f"新增重要记忆 {saved_important_count} 条"
        )

    @staticmethod
    def _select_compaction_slice(
        records: list[dict[str, Any]],
        *,
        active_tokens: int,
        target_after_tokens: int,
        estimator: TokenEstimator,
    ) -> list[dict[str, Any]]:
        """从最旧记录开始选择需要归档的切片。"""
        need_remove = max(1, active_tokens - target_after_tokens)
        selected: list[dict[str, Any]] = []
        removed = 0
        # 至少保留最后一条活跃记录，避免 history 被清空后 task context 太孤立。
        for record in records[:-1]:
            selected.append(record)
            removed += estimator.estimate_messages([record])
            if removed >= need_remove:
                break
        return selected
