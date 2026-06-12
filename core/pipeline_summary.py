"""Summary/compaction helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change summary trigger, archive, rolling-summary, or
important-memory save logic while moving methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from utils import get_time
from utils.token_budget import TokenEstimator

from .pipeline_history import _record_timestamp

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SummaryAttemptResult:
    status: str
    reason: str = ""
    active_start_before: int = 0
    active_start_after: int = 0
    active_records: int = 0
    active_tokens: int = 0
    archived_count: int = 0
    target_after_tokens: int | None = None
    partial_archive_committed: bool = False
    archive_reused: bool = False

    @property
    def success(self) -> bool:
        return self.status == "success"


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

    async def _maybe_summarize(
        self,
        *,
        force: bool = False,
        target_after_tokens: int | None = None,
        reason: str = "scheduled",
    ) -> SummaryAttemptResult:
        """按阈值压缩活跃 history：归档原文，只移动活跃窗口起点。"""
        async with self._summary_compaction_lock():
            return await self._maybe_summarize_unlocked(
                force=force,
                target_after_tokens=target_after_tokens,
                reason=reason,
            )

    def _summary_compaction_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_summary_compaction_lock_obj", None)
        if lock is None:
            lock = asyncio.Lock()
            self._summary_compaction_lock_obj = lock
        return lock

    async def _maybe_summarize_unlocked(
        self,
        *,
        force: bool,
        target_after_tokens: int | None,
        reason: str,
    ) -> SummaryAttemptResult:
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            return SummaryAttemptResult(status="not_configured", reason=reason)

        records = await self.history.records()
        if not records:
            return SummaryAttemptResult(status="not_needed", reason="empty_history")
        active_start = self._active_start_index_for_records(records)
        active_records = records[active_start:]
        if not active_records:
            return SummaryAttemptResult(
                status="not_needed",
                reason="empty_active_history",
                active_start_before=active_start,
                active_start_after=active_start,
            )
        estimator = self._token_estimator()
        active_tokens = estimator.estimate_messages(active_records)
        budget = self._context_budget()
        summarize_cfg = self.behavior_cfg.summarize
        trigger = summarize_cfg.trigger_at_tokens
        if trigger is None:
            trigger = int(
                budget.max_context_tokens
                * summarize_cfg.trigger_at_context_percent
                / 100
            )
        token_triggered = active_tokens >= trigger
        if not force and not token_triggered:
            return SummaryAttemptResult(
                status="not_needed",
                reason="below_trigger",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active_records),
                active_tokens=active_tokens,
            )
        target_after = self._summary_target_after_tokens(
            active_tokens=active_tokens,
            override=target_after_tokens,
        )
        if target_after is None:
            return SummaryAttemptResult(
                status="failed",
                reason="invalid_target",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active_records),
                active_tokens=active_tokens,
            )
        target_after = max(1, min(target_after, active_tokens - 1))

        logger.info(
            "活跃历史触发滚动摘要 compaction reason=%s tokens=%s trigger=%s "
            "records=%s",
            reason,
            active_tokens,
            trigger,
            len(active_records),
        )

        return await self._compact_active_history_unlocked(
            target_after_tokens=target_after,
            reason=reason,
            active_records=active_records,
            active_start_index=active_start,
            active_tokens=active_tokens,
            estimator=estimator,
        )

    async def _compact_active_history(
        self,
        *,
        target_after_tokens: int,
        reason: str,
        active_records: list[dict[str, Any]] | None = None,
        active_start_index: int | None = None,
        active_tokens: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> SummaryAttemptResult:
        async with self._summary_compaction_lock():
            return await self._compact_active_history_unlocked(
                target_after_tokens=target_after_tokens,
                reason=reason,
                active_records=active_records,
                active_start_index=active_start_index,
                active_tokens=active_tokens,
                estimator=estimator,
            )

    async def _compact_active_history_unlocked(
        self,
        *,
        target_after_tokens: int,
        reason: str,
        active_records: list[dict[str, Any]] | None = None,
        active_start_index: int | None = None,
        active_tokens: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> SummaryAttemptResult:
        if self.summary_agent is None or self.archive is None or self.rolling_summary is None:
            return SummaryAttemptResult(status="not_configured", reason=reason)

        records = await self.history.records()
        active_start = (
            self._active_start_index_for_records(records)
            if active_start_index is None
            else max(0, min(active_start_index, len(records)))
        )
        active = active_records if active_records is not None else records[active_start:]
        if not active:
            return SummaryAttemptResult(
                status="not_needed",
                reason="empty_active_history",
                active_start_before=active_start,
                active_start_after=active_start,
            )
        estimator = estimator or self._token_estimator()
        active_tokens = active_tokens or estimator.estimate_messages(active)
        target_after = max(1, min(target_after_tokens, active_tokens - 1))

        important_text = self.important.text()
        slice_records = self._select_compaction_slice(
            active,
            active_tokens=active_tokens,
            target_after_tokens=target_after,
            estimator=estimator,
        )
        if not slice_records:
            logger.warning("compaction 未选出可归档切片，跳过")
            return SummaryAttemptResult(
                status="failed",
                reason="empty_slice",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=active_tokens,
                target_after_tokens=target_after,
            )

        try:
            result = await self.summary_agent.summarize_rolling(
                slice_records,
                self.rolling_summary.text(),
                important_text,
            )
        except Exception as e:
            logger.warning("滚动摘要 Agent 调用失败: %s", e)
            return SummaryAttemptResult(
                status="failed",
                reason="summary_agent_error",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=active_tokens,
                target_after_tokens=target_after,
            )
        if not result:
            logger.warning("滚动摘要 Agent 返回 None，跳过本次压缩")
            return SummaryAttemptResult(
                status="failed",
                reason="empty_result",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=active_tokens,
                target_after_tokens=target_after,
            )

        budget = self._context_budget()
        summary_text = self._trim_text_to_token_budget(
            str(result.get("summary_text") or "").strip(),
            budget.summary_token_budget,
            estimator,
        )
        new_important_items = result.get("new_important", [])
        if not summary_text:
            logger.warning("滚动摘要为空，跳过本次压缩")
            return SummaryAttemptResult(
                status="failed",
                reason="empty_summary_text",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=active_tokens,
                target_after_tokens=target_after,
            )

        new_active_start = active_start + len(slice_records)
        archive_key = self._compaction_archive_key(active_start, slice_records)
        partial_archives = self._partial_compaction_archives()
        archive_reused = archive_key in partial_archives
        try:
            if archive_reused:
                logger.warning(
                    "检测到上次滚动摘要 archive 已提交但游标未更新，"
                    "本次重试跳过重复 archive append: reason=%s active_start=%s count=%s",
                    reason,
                    active_start,
                    len(slice_records),
                )
            else:
                await self.archive.append_many(slice_records)
                partial_archives[archive_key] = {
                    "active_start": active_start,
                    "count": len(slice_records),
                    "reason": reason,
                }
            await self.rolling_summary.update(
                summary_text,
                archived_until={
                    "last_compaction_count": len(slice_records),
                    "last_timestamp": _record_timestamp(slice_records[-1]),
                },
                updated_at=get_time(),
                active_start_index=new_active_start,
            )
        except Exception as e:
            logger.warning(
                "滚动摘要提交失败，未移动活跃起点；如果 archive 已提交，"
                "同进程重试会跳过重复归档: %s",
                e,
            )
            return SummaryAttemptResult(
                status="failed",
                reason="commit_error",
                active_start_before=active_start,
                active_start_after=active_start,
                active_records=len(active),
                active_tokens=active_tokens,
                target_after_tokens=target_after,
                partial_archive_committed=archive_key in partial_archives,
                archive_reused=archive_reused,
            )
        partial_archives.pop(archive_key, None)

        saved_important_count = 0
        if isinstance(new_important_items, list) and new_important_items:
            for item in new_important_items:
                content = (item.get("content") or "").strip() if isinstance(item, dict) else ""
                if content:
                    scope = item.get("scope") if isinstance(item.get("scope"), str) else None
                    pinned = item.get("pinned") if isinstance(item.get("pinned"), bool) else False
                    try:
                        save_result = await self.important.save(
                            content,
                            scope=scope,
                            pinned=pinned,
                        )
                    except Exception as e:
                        logger.warning("滚动摘要提取重要记忆保存失败: %s", e)
                        continue
                    if save_result.get("saved"):
                        saved_important_count += 1

        await self.history.add_system_note(
            f"[滚动摘要] 已归档并移动活跃窗口起点 {len(slice_records)} 条；"
            f"新增重要记忆 {saved_important_count} 条"
        )
        return SummaryAttemptResult(
            status="success",
            reason=reason,
            active_start_before=active_start,
            active_start_after=new_active_start,
            active_records=len(active),
            active_tokens=active_tokens,
            archived_count=len(slice_records),
            target_after_tokens=target_after,
            archive_reused=archive_reused,
        )

    def _partial_compaction_archives(self) -> dict[str, dict[str, Any]]:
        partial = getattr(self, "_summary_partial_archives", None)
        if partial is None:
            partial = {}
            self._summary_partial_archives = partial
        return partial

    @staticmethod
    def _compaction_archive_key(
        active_start: int,
        slice_records: list[dict[str, Any]],
    ) -> str:
        payload = json.dumps(
            slice_records,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"{active_start}:{len(slice_records)}:{digest}"

    def _summary_target_after_tokens(
        self,
        *,
        active_tokens: int,
        override: int | None = None,
    ) -> int | None:
        if active_tokens <= 1:
            return None
        summarize_cfg = self.behavior_cfg.summarize
        target_after = override
        if target_after is None:
            target_after = summarize_cfg.target_after_tokens
        if target_after is None:
            target_after = int(
                self._context_budget().max_context_tokens
                * summarize_cfg.target_after_context_percent
                / 100
            )
        return max(1, min(int(target_after), active_tokens - 1))

    def _active_start_index_for_records(self, records: list[dict[str, Any]]) -> int:
        if self.rolling_summary is None:
            return 0
        active_start = max(0, min(self.rolling_summary.active_start_index(), len(records)))
        return self._provider_safe_active_start_index(records, active_start)

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
        safe_len = PipelineSummaryMixin._provider_safe_prefix_len(records, len(selected))
        return records[:safe_len]

    @staticmethod
    def _provider_safe_prefix_len(records: list[dict[str, Any]], cut: int) -> int:
        """调整切点，避免把 assistant/tool 调用组切开。"""
        cut = max(0, min(cut, max(0, len(records) - 1)))
        if cut <= 0:
            return cut

        for start, end in PipelineSummaryMixin._assistant_tool_group_ranges(records):
            if start < cut < end:
                # 优先向后扩到工具调用组末尾；如果会清空活跃区，再退回组开始。
                return end if end < len(records) else start
        return cut

    @staticmethod
    def _provider_safe_active_start_index(
        records: list[dict[str, Any]],
        active_start: int,
    ) -> int:
        """修正旧游标，避免活跃窗口从孤立 tool 结果开始。"""
        active_start = max(0, min(active_start, len(records)))
        if active_start <= 0 or active_start >= len(records):
            return active_start

        for start, end in PipelineSummaryMixin._assistant_tool_group_ranges(records):
            if start < active_start < end:
                return start
        return active_start

    @staticmethod
    def _assistant_tool_group_ranges(records: list[dict[str, Any]]) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        idx = 0
        while idx < len(records):
            record = records[idx]
            if record.get("role") != "assistant":
                idx += 1
                continue
            tool_call_ids = PipelineSummaryMixin._assistant_tool_call_ids(record)
            if not tool_call_ids:
                idx += 1
                continue

            seen: set[str] = set()
            end = idx + 1
            while end < len(records) and records[end].get("role") == "tool":
                tool_call_id = str(records[end].get("tool_call_id") or "")
                if tool_call_id not in tool_call_ids:
                    break
                seen.add(tool_call_id)
                end += 1
                if seen >= tool_call_ids:
                    break
            ranges.append((idx, end))
            idx = max(end, idx + 1)
        return ranges

    @staticmethod
    def _assistant_tool_call_ids(record: dict[str, Any]) -> set[str]:
        result: set[str] = set()
        for tool_call in record.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id") or "")
            if call_id:
                result.add(call_id)
        return result
