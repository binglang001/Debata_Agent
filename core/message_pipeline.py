"""消息处理管道 —— 整个项目的心脏。

负责：
    1. 接收 IncomingMessage（来自 EventBus）
    2. 速率限制（非好友）
    3. 重建可读文本（CQ 码解析 + 附加媒体 URL）
    4. 关键词强制保存重要记忆（RAG 只影响历史召回，不关闭 important.json）
    5. 合并窗口暂存
    6. 批处理触发：合并窗口到时，组装 messages → 调 ChatAgent
    7. 发送类工具走同步快路径或每会话异步发送队列
    8. 历史持久化 + 总结触发

这是 P1.8 的核心模块，已装实完整链路：消息合并 / 异步发送 / 媒体抽取 /
总结触发 / 工具循环 / 唤醒响应。

----------
依赖注入图（按构造器参数顺序）：

    adapter         IAdapter            发送和拉取媒体 URL
    chat_agent      ChatAgent           Pro 主聊天模型
    persona         Persona             人格
    history         HistoryManager      对话历史
    important       ImportantMemoryManager 重要记忆
    tool_registry   ToolRegistry        工具集合
    wakeup_scheduler WakeupScheduler    延时唤醒
    rate_limiter    RateLimiter | None  非好友速率限制
    pending_requests PendingRequestStore  好友/群请求暂存
    summary_agent   SummaryAgent | None  历史总结（可选）
    behavior_cfg    BehaviorConfig      合并窗口/typing 等运行时参数
    features_cfg    FeaturesConfig      用来决定 memory_mode 等
    emoji_dir       Path | None         表情包目录
    workspace_dir Path | None      文件上传白名单
    vision/web_search/weather  feature service 实例（按需）

----------
状态管理：
    self.batch          MessageBatch     —— 待批处理队列
    self.reply_lock     asyncio.Lock     —— 确保同一时刻只有一个 Agent 在跑
    self._batch_task    asyncio.Task     —— 当前批处理任务
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from adapters.base import IAdapter
from adapters.types import IncomingMessage, Target
from agents import ChatAgent, Persona, SummaryAgent, build_messages
from agents.base import AgentRunResult
from app_config.schema import BehaviorConfig, FeaturesConfig, WhitelistConfig
from memory import (
    ArchiveStore,
    HistoryManager,
    ImportantMemoryManager,
    RollingSummaryStore,
)
from tools import (
    IVisionService,
    IWeatherService,
    IWebSearchService,
    ToolContext,
    ToolRegistry,
    try_save_from_user,
)
from utils import get_time
from utils.token_budget import TokenBudget, TokenEstimator

from .agent_task_helpers import (
    _agent_record_matches as _agent_record_matches,
)
from .agent_task_helpers import (
    _agent_task_dedupe_key as _agent_task_dedupe_key,
)
from .agent_task_helpers import (
    _agent_task_partial_text as _agent_task_partial_text,
)
from .agent_task_helpers import (
    _agent_task_prompt_hash as _agent_task_prompt_hash,
)
from .agent_task_helpers import (
    _agent_task_source_hash as _agent_task_source_hash,
)
from .agent_task_helpers import (
    _agent_task_timeout_seconds as _agent_task_timeout_seconds,
)
from .agent_task_helpers import (
    _clamp_agent_task_max_loops as _clamp_agent_task_max_loops,
)
from .agent_task_helpers import (
    _file_head_tail_preview as _file_head_tail_preview,
)
from .agent_task_helpers import (
    _first_meaningful_line as _first_meaningful_line,
)
from .agent_task_helpers import (
    _is_within as _is_within,
)
from .agent_task_helpers import (
    _record_has_message_id as _record_has_message_id,
)
from .agent_task_helpers import (
    _resolve_agent_workspace_path as _resolve_agent_workspace_path,
)
from .agent_task_helpers import (
    _safe_agent_task_filename as _safe_agent_task_filename,
)
from .agent_task_helpers import (
    _summarize_agent_task_manifest as _summarize_agent_task_manifest,
)
from .agent_task_helpers import (
    _workspace_rel as _workspace_rel,
)
from .chat_timeline import ChatTimelineStore
from .media_pipeline import MediaPipelineMixin
from .pipeline_context import (
    _earliest_record_ts as _earliest_record_ts,
)
from .pipeline_context import (
    _filter_tool_schemas as _filter_tool_schemas,
)
from .pipeline_context import (
    _make_runtime_context_record as _make_runtime_context_record,
)
from .pipeline_context import (
    _make_task_context_record as _make_task_context_record,
)
from .pipeline_context import (
    _recommended_context_budget as _recommended_context_budget,
)
from .pipeline_history import (
    _WORKING_HISTORY_NO_ACTION_KEEP as _WORKING_HISTORY_NO_ACTION_KEEP,
)
from .pipeline_history import (
    _WORKING_HISTORY_RECENT_RUNTIME_RECORDS as _WORKING_HISTORY_RECENT_RUNTIME_RECORDS,
)
from .pipeline_history import (
    _WORKING_HISTORY_SEND_RECEIPT_KEEP as _WORKING_HISTORY_SEND_RECEIPT_KEEP,
)
from .pipeline_history import (
    _assistant_tool_call_names as _assistant_tool_call_names,
)
from .pipeline_history import (
    _no_action_pair_indices as _no_action_pair_indices,
)
from .pipeline_history import (
    _record_conversation_id as _record_conversation_id,
)
from .pipeline_history import (
    _record_timestamp as _record_timestamp,
)
from .pipeline_history import (
    _runtime_context_kind as _runtime_context_kind,
)
from .pipeline_history import (
    _tool_result_is_no_action as _tool_result_is_no_action,
)
from .pipeline_history import (
    _working_history_force_keep_indices as _working_history_force_keep_indices,
)
from .pipeline_history import (
    _working_history_noise_indices as _working_history_noise_indices,
)
from .pipeline_history import (
    _working_history_optional_runtime_indices as _working_history_optional_runtime_indices,
)
from .send_manager import (
    _AsyncSendManager as _AsyncSendManager,
)
from .send_manager import (
    _InboundRef as _InboundRef,
)
from .send_manager import (
    _SendAttempt as _SendAttempt,
)
from .send_manager import (
    _SendConversationState as _SendConversationState,
)
from .send_manager import (
    _SendJob as _SendJob,
)
from .send_manager import (
    _text_mentions_self_or_role as _text_mentions_self_or_role,
)
from .state import MessageBatch, PendingMessageItem, PendingRequestStore, RateLimiter
from .wakeup import WakeupScheduler

logger = logging.getLogger(__name__)





# 速率超限时的提示模板（占位符运行时替换）
_RATE_LIMIT_REPLY_TEMPLATE = "已超出速率限制（{window_seconds} 秒内最多 {max_messages} 条），请添加机器人为好友后继续使用"
_PREFIX_ESTIMATE_TOKENS = 12_000
_CURRENT_CONVERSATION_MIN_RECORDS = 8
_PROACTIVE_ROUTER_HISTORY_BUDGET = 16_384
_SLOW_BATCH_STAGE_SECONDS = 1.0
_OUT_OF_BAND_DENIED_TOOLS = frozenset(
    {
        "start_agent_task",
        "summarize_conversation",
        "summarize_chat_history",
    }
)


def _log_slow_batch_stage(
    stage: str,
    started_at: float,
    *,
    conversation_id: str,
    extra: str = "",
) -> None:
    elapsed = time.monotonic() - started_at
    if elapsed < _SLOW_BATCH_STAGE_SECONDS:
        return
    suffix = f" {extra}" if extra else ""
    logger.warning(
        "批处理阶段耗时过长 stage=%s conversation_id=%s elapsed=%.3fs%s",
        stage,
        conversation_id,
        elapsed,
        suffix,
    )





class MessagePipeline(MediaPipelineMixin):
    """消息处理管道。

    生命周期：
        pipeline = MessagePipeline(...)
        # 启动后通过 EventBus 接 enqueue
        await pipeline.shutdown()  # 程序关闭时
    """

    def __init__(
        self,
        *,
        adapter: IAdapter,
        chat_agent: ChatAgent,
        persona: Persona,
        history: HistoryManager,
        important: ImportantMemoryManager,
        archive: ArchiveStore | None = None,
        rolling_summary: RollingSummaryStore | None = None,
        tool_registry: ToolRegistry,
        wakeup_scheduler: WakeupScheduler,
        pending_requests: PendingRequestStore,
        behavior_cfg: BehaviorConfig,
        features_cfg: FeaturesConfig,
        whitelist: WhitelistConfig | None = None,
        emoji_dir: Path | None = None,
        workspace_dir: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        summary_agent: SummaryAgent | None = None,
        model_context_length: int | None = None,
        vision: IVisionService | None = None,
        web_search: IWebSearchService | None = None,
        weather: IWeatherService | None = None,
        asr: Any = None,
        tts: Any = None,
        rag_memory: Any = None,
    ) -> None:
        self.adapter = adapter
        self.chat_agent = chat_agent
        self.persona = persona
        self.history = history
        self.important = important
        self.archive = archive
        self.rolling_summary = rolling_summary
        self.tool_registry = tool_registry
        self.wakeup_scheduler = wakeup_scheduler
        self.pending_requests = pending_requests
        self.behavior_cfg = behavior_cfg
        self.features_cfg = features_cfg
        # whitelist=None 时按 verify 默认行为（不主动过滤入站）
        self.whitelist = whitelist or WhitelistConfig()
        self._whitelist_qq_ids = set(self.whitelist.qq_ids)
        self._whitelist_group_ids = set(self.whitelist.group_ids)
        self.emoji_dir = emoji_dir
        self.workspace_dir = workspace_dir
        self.rate_limiter = rate_limiter
        self.summary_agent = summary_agent
        self.model_context_length = model_context_length
        self.vision = vision
        self.web_search = web_search
        self.weather = weather
        self.asr = asr
        self.tts = tts
        self.rag_memory = rag_memory

        self.batch = MessageBatch()
        self.reply_lock = asyncio.Lock()
        self._batch_task: asyncio.Task | None = None
        # 退出 _batch_loop 后的兜底 task；保留引用避免 GC
        self._requeue_task: asyncio.Task | None = None
        self._summary_task: asyncio.Task | None = None
        self._token_calib_ratio = 1.0
        self.last_activity_at = time.monotonic()
        self._inbound_seq = 0
        self._send_manager = _AsyncSendManager(self)
        self._send_receipt_tasks: dict[str, asyncio.Task] = {}
        self._agent_task_meta: dict[str, dict[str, Any]] = {}
        self.chat_timeline = ChatTimelineStore(max_per_conversation=1000)
        self._self_id_by_conversation: dict[str, str] = {}
        self._warn_context_compaction_invariants()

    def mark_activity(self) -> None:
        """刷新活动时间。主动思考只在足够空闲后触发。"""
        self.last_activity_at = time.monotonic()

    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.last_activity_at)

    # ============================================================
    # 入口：EventBus 调用此方法
    # ============================================================

    async def enqueue(self, event: IncomingMessage) -> None:
        """接收一条入站消息，做前置检查后加入批处理队列。"""
        enqueue_t0 = time.monotonic()
        if not event.text and not event.media:
            # 空消息（无文本无媒体）忽略
            return
        self.mark_activity()

        # 白名单拦截（仅 mode=whitelist 时严格按名单过滤；open/verify 都放行）
        if self.whitelist.mode == "whitelist":
            if event.is_group():
                try:
                    gid = int(event.group_id or 0)
                except (TypeError, ValueError):
                    gid = 0
                if gid not in self._whitelist_group_ids:
                    logger.debug(
                        f"群消息被白名单拦截：group_id={event.group_id} 不在 {self._whitelist_group_ids}"
                    )
                    return
            else:
                try:
                    uid = int(event.user_id or 0)
                except (TypeError, ValueError):
                    uid = 0
                if uid not in self._whitelist_qq_ids:
                    logger.debug(
                        f"私聊消息被白名单拦截：user_id={event.user_id} 不在 {self._whitelist_qq_ids}"
                    )
                    return

        # 速率限制只针对私聊陌生人。群聊本身由群白名单/审核控制，不按群成员逐个限速。
        if (
            self.rate_limiter
            and not event.is_group()
            and await self.rate_limiter.check_and_log(event.user_id)
        ):
            await self._send_rate_limit_reply(event)
            return

        conversation_id = self._conversation_id_from_event(event)

        # 关键词强制保存（命中即写入重要记忆）
        keyword_saved = False
        if event.text:
            keyword_result = await try_save_from_user(
                event.text,
                self.important,
                enabled=self.features_cfg.long_term_memory.keyword_trigger_save,
                scope=conversation_id,
            )
            keyword_saved = bool(keyword_result and keyword_result.get("saved"))

        # 重建可读文本（CQ 码 + 媒体）
        text = await self._build_readable_text(event)
        self._inbound_seq += 1
        inbound_seq = self._inbound_seq
        received_at = time.monotonic()
        logger.debug(
            "入站消息 received_at_ms=%s conversation_id=%s msg_id=%s user_id=%s",
            int(time.time() * 1000),
            conversation_id,
            event.message_id,
            event.user_id,
        )
        self._self_id_by_conversation[conversation_id] = str(getattr(event, "self_id", "") or "")

        item = PendingMessageItem(
            message_id=event.message_id,
            user_id=event.user_id,
            nickname=event.nickname,
            location=f"群聊 {event.group_id}" if event.is_group() else "私聊",
            text=text,
            conversation_id=conversation_id,
            inbound_seq=inbound_seq,
            received_at=received_at,
            raw_event=event,
            keyword_saved=keyword_saved,
        )

        self.chat_timeline.append_inbound_event(
            event,
            conversation_id=conversation_id,
            text=text,
            timestamp=getattr(event, "timestamp", None),
        )
        await self.batch.append(item)
        self._send_manager.notify_inbound(item)
        logger.debug(
            "入站消息预处理完成 conversation_id=%s msg_id=%s text_len=%s keyword_saved=%s elapsed=%.3fs",
            conversation_id,
            event.message_id,
            len(text),
            keyword_saved,
            time.monotonic() - enqueue_t0,
        )
        if self._send_manager.should_defer_batch(conversation_id):
            return
        # 启动批处理任务（如未运行）
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_loop(event.source_target))

    @staticmethod
    def _conversation_id_from_event(event: IncomingMessage) -> str:
        """把平台事件映射成统一历史流里的会话标签。"""
        if event.group_id:
            return f"group:{event.group_id}"
        target = getattr(event, "source_target", None)
        if target is not None and getattr(target, "scope", None) == "private":
            return f"private:{target.target_id}"
        return f"private:{event.user_id}"

    # ============================================================
    # 批处理主循环
    # ============================================================

    async def _batch_loop(self, return_target: Target) -> None:
        """合并窗口循环：等待 → 取一批 → 处理 → 若被中断则重循环。

        return_target 用于"如果发送被打断，剩余 actions 失败的回执发去哪"等场景。
        """
        await asyncio.sleep(self.behavior_cfg.merge_window_seconds)

        while True:
            items = await self.batch.drain()
            if not items:
                break

            interrupted = False
            grouped: dict[str, list[PendingMessageItem]] = {}
            for item in items:
                grouped.setdefault(item.conversation_id, []).append(item)

            for group_items in grouped.values():
                try:
                    interrupted = await self._process_batch(group_items) or interrupted
                except Exception as e:
                    logger.exception(f"处理批次失败: {e}")
                    break

            if not interrupted:
                break

            # 被中断说明有新消息插入，再等一个窗口
            await asyncio.sleep(self.behavior_cfg.merge_window_seconds)

        # 退出前异步回查：处理期间可能有新消息正好入队
        async def _requeue_check():
            await asyncio.sleep(0)
            if not self.batch.is_empty_unsafe() and (
                self._batch_task is None or self._batch_task.done()
            ):
                self._batch_task = asyncio.create_task(self._batch_loop(return_target))

        # 保留引用，避免 task 在 await 跨边界时被 GC
        self._requeue_task = asyncio.create_task(_requeue_check())

    def _schedule_deferred_batch(self, conversation_id: str) -> None:
        """发送收尾竞态解除后，恢复处理此前被 defer 的入站消息。"""

        async def _start_if_pending() -> None:
            async with self.batch.lock:
                items = [
                    item
                    for item in await self.batch.peek_locked()
                    if item.conversation_id == conversation_id
                ]
            if not items:
                return
            if self._batch_task is not None and not self._batch_task.done():
                return
            self._batch_task = asyncio.create_task(
                self._batch_loop(items[-1].raw_event.source_target)
            )

        self._requeue_task = asyncio.create_task(_start_if_pending())

    def _build_user_record(
        self,
        items: list[PendingMessageItem],
        now: str | None = None,
    ) -> dict[str, Any]:
        """把同一会话的一批入站消息合成一条 user 历史记录。"""
        now = now or get_time()
        conversation_id = items[-1].conversation_id if items else "legacy:unknown"
        lines: list[str] = []
        meta_messages: list[dict[str, Any]] = []
        for item in items:
            lines.append(
                f"【{now} {item.location} {item.nickname}({item.user_id}) "
                f"msg_id={item.message_id}】{item.text}"
            )
            raw = item.raw_event
            target = getattr(raw, "source_target", None)
            scope = getattr(raw, "scope", None) or getattr(target, "scope", None)
            target_id = getattr(target, "target_id", "")
            group_id = getattr(raw, "group_id", None)
            meta_messages.append(
                {
                    "scope": scope or ("group" if group_id else "private"),
                    "target_id": target_id or group_id or item.user_id,
                    "group_id": group_id,
                    "user_id": item.user_id,
                    "nickname": item.nickname,
                    "message_id": item.message_id,
                    "timestamp": getattr(raw, "timestamp", None),
                    "location": item.location,
                    "text": item.text,
                    "inbound_seq": item.inbound_seq,
                    "received_at": item.received_at,
                }
            )
        return {
            "role": "user",
            "content": "\n".join(lines),
            "metadata": {"timestamp": now, "messages": meta_messages},
            "conversation_id": conversation_id,
        }

    async def _process_batch(self, items: list[PendingMessageItem]) -> bool:
        """处理一批消息。返回 True 表示发送被中断（需要重循环）。

        步骤：
            1. 合并文本写入 history（user 消息）
            2. 构造 ToolContext + 调 ChatAgent
            3. 写 records 到 history
            4. 执行遗留 collected 兜底动作（常规发送已在工具内即时完成）
            5. 触发可选总结
        """
        batch_t0 = time.monotonic()
        now = get_time()
        user_record = self._build_user_record(items, now)
        conversation_id = user_record.get("conversation_id") or "legacy:unknown"
        stage_t0 = time.monotonic()
        await self.history.add_records([user_record], conversation_id=conversation_id)
        _log_slow_batch_stage("history_add_user", stage_t0, conversation_id=conversation_id)
        logger.info(f"合并处理 {len(items)} 条消息")

        # 构造给 LLM 的 messages（emoji_hint / pending_requests 已在 _build_task_context 内拼装）
        stage_t0 = time.monotonic()
        task_context = self._build_task_context(now, conversation_id)
        task_context_record = _make_task_context_record(
            task_context,
            conversation_id=conversation_id,
        )
        _log_slow_batch_stage(
            "build_task_context",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"context_len={len(task_context)}",
        )

        stage_t0 = time.monotonic()
        history_window = await self._select_working_history(conversation_id)
        _log_slow_batch_stage(
            "select_working_history",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"records={len(history_window)}",
        )
        estimator = self._token_estimator()

        stage_t0 = time.monotonic()
        important_text = await self._important_memory_text(
            conversation_id,
        )
        _log_slow_batch_stage(
            "important_memory_text",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"memory_len={len(important_text)}",
        )

        stage_t0 = time.monotonic()
        rag_context_text = await self._rag_context_text(
            conversation_id,
            query=items[-1].text if items else None,
            before_ts=_earliest_record_ts(history_window),
        )
        _log_slow_batch_stage(
            "rag_context_text",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"rag_len={len(rag_context_text)}",
        )
        logger.debug(
            "批处理上下文准备完成 conversation_id=%s memory_len=%s rag_len=%s elapsed=%.3fs",
            conversation_id,
            len(important_text),
            len(rag_context_text),
            time.monotonic() - batch_t0,
        )

        stage_t0 = time.monotonic()
        rolling_summary_text = self._rolling_summary_text(estimator)
        _log_slow_batch_stage(
            "rolling_summary_text",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"summary_len={len(rolling_summary_text)}",
        )

        stage_t0 = time.monotonic()
        messages = build_messages(
            persona=self.persona,
            history=history_window,
            important_memory_text=important_text,
            rag_context_text=rag_context_text,
            rolling_summary_text=rolling_summary_text,
            current_context_record=task_context_record,
            memory_mode=self.features_cfg.long_term_memory.mode,
        )
        _log_slow_batch_stage(
            "build_messages",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"messages={len(messages)}",
        )

        # 构造 ToolContext
        stage_t0 = time.monotonic()
        default_target = items[-1].raw_event.source_target if items else None
        latest_user_text = "\n".join(item.text for item in items)
        batch_trigger_message_ids = self._batch_trigger_message_ids(items)
        batch_focus_user_ids = self._batch_focus_user_ids(items, batch_trigger_message_ids)
        ctx = self._build_tool_context(
            default_target=default_target,
            latest_user_text=latest_user_text,
            conversation_id=conversation_id,
            trigger_message_id=items[-1].message_id if items else None,
            trigger_inbound_seq=items[-1].inbound_seq if items else 0,
            trigger_user_id=items[-1].user_id if items else None,
            seen_inbound_seq=items[-1].inbound_seq if items else 0,
            focus_user_ids=batch_focus_user_ids,
            trigger_message_ids=batch_trigger_message_ids,
        )
        executor = self.tool_registry.get_executor(ctx)
        tools_schema = self.tool_registry.get_schemas()
        _log_slow_batch_stage(
            "build_tool_context_schema",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"tools={len(tools_schema)}",
        )

        stage_t0 = time.monotonic()
        estimated_prompt_tokens = estimator.estimate_messages(messages)
        if tools_schema:
            estimated_prompt_tokens += estimator.estimate_text(str(tools_schema))
        _log_slow_batch_stage(
            "estimate_prompt_tokens",
            stage_t0,
            conversation_id=conversation_id,
            extra=f"estimated={estimated_prompt_tokens}",
        )

        # 只串行模型轮；Phase 0 后台发送不占 reply_lock。
        stage_t0 = time.monotonic()
        async with self.reply_lock:
            _log_slow_batch_stage(
                "reply_lock_wait",
                stage_t0,
                conversation_id=conversation_id,
            )
            self._send_manager.begin_model_turn(conversation_id)
            model_t0 = time.monotonic()
            try:
                result = await self.chat_agent.run(
                    messages,
                    tools=tools_schema,
                    tool_executor=executor,
                    task_contract=None,  # 暂不强制 task contract，由 prompt 末尾的提示词承担焦点引导
                    pending_context_provider=lambda: self._consume_send_receipts(
                        conversation_id
                    ),
                )
            finally:
                self._send_manager.end_model_turn(conversation_id)
            logger.debug(
                "模型轮完成 conversation_id=%s finish=%s loops=%s elapsed=%.3fs",
                conversation_id,
                result.finish_reason,
                result.loop_count,
                time.monotonic() - model_t0,
            )

            # 写 records
            records_to_add = [
                record
                for record in [task_context_record, *list(result.records or [])]
                if record is not None
            ]
            if records_to_add:
                await self.history.add_records(
                    records_to_add,
                    conversation_id=conversation_id,
                )

            # Phase 0 后发送类工具已在工具调用内即时发送；这里仅保留兼容兜底。
            await self._execute_collected(ctx.collected)
            self._calibrate_tokens(estimated_prompt_tokens, result.prompt_tokens)
            self.mark_activity()

        self._schedule_summarize()
        logger.debug(
            "批处理完成 conversation_id=%s items=%s elapsed=%.3fs",
            conversation_id,
            len(items),
            time.monotonic() - batch_t0,
        )

        return False

    # ============================================================
    # 执行遗留 collected 发送动作
    # ============================================================

    async def _execute_collected(
        self,
        collected: list[dict],
    ) -> bool:
        """逐条发送遗留 collected 动作。

        Phase 0 起，send_private_messages / send_group_message / send_voice_message
        都在工具调用内即时发送。该函数只保留给定时直接发送、旧测试与未来少量
        兼容动作使用；不再检查 batch、不中断、不丢弃剩余动作。
        """
        if not collected:
            return False

        sent = 0
        for i, action in enumerate(collected):
            # 真实发送
            msg_id = await self._do_send(action)

            # 写入 system_note（已发送记录）
            label = action.get("label", "")
            if label:
                await self.history.add_system_note(
                    f"{get_time()} msg_id={msg_id} → {label}"
                )

            sent += 1

            # 单条延迟
            delay = action.get("delay", 0.0)
            if delay > 0 and i < len(collected) - 1:
                await asyncio.sleep(delay)

        return False

    async def _consume_send_receipts(
        self,
        conversation_id: str,
    ) -> list[dict[str, Any]]:
        """把待投递发送回执转换成本轮可追加的上下文记录。"""
        receipts = self._send_manager.pop_pending_receipts(conversation_id)
        if not receipts:
            return []

        records: list[dict[str, Any]] = []
        interrupted_items = await self.batch.drain_conversation(conversation_id)
        if interrupted_items:
            records.append(self._build_user_record(interrupted_items))
            new_messages = [
                {
                    "conversation_id": item.conversation_id,
                    "time": get_time(),
                    "nickname": item.nickname,
                    "user_id": item.user_id,
                    "text": item.text,
                    "msg_id": item.message_id,
                    "qq_visible": True,
                }
                for item in interrupted_items
            ]
            for receipt in receipts:
                known = {m.get("msg_id") for m in receipt.get("new_messages", [])}
                receipt.setdefault("new_messages", []).extend(
                    m for m in new_messages if m["msg_id"] not in known
                )

        for receipt in receipts:
            records.append(
                {
                    "role": "user",
                    "content": self._format_send_receipt(receipt),
                    "conversation_id": conversation_id,
                }
            )

        self._send_manager.mark_receipts_delivered(conversation_id)
        return records

    async def _record_clean_send_receipt(self, receipt: dict[str, Any]) -> None:
        """清洁发送完成只静默入历史，不触发模型。"""
        conversation_id = str(receipt.get("conversation_id") or "")
        sent = receipt.get("sent") or []
        if not sent:
            return
        msg_ids = ", ".join(
            str(item.get("msg_id")) for item in sent if item.get("msg_id") is not None
        )
        record = _make_runtime_context_record(
            f"{get_time()} 发送完成（全部消息已发出）"
            f" send_id={receipt.get('send_id')} msg_ids=[{msg_ids}]",
            kind="send_done_snapshot",
            tag="send_status",
            conversation_id=conversation_id or None,
        )
        if record is not None:
            await self.history.add_records([record], conversation_id=conversation_id or None)

    def _schedule_send_receipt_turn(self, conversation_id: str) -> None:
        task = self._send_receipt_tasks.get(conversation_id)
        if task is not None and not task.done():
            return
        self._send_receipt_tasks[conversation_id] = asyncio.create_task(
            self._run_send_receipt_turn(conversation_id)
        )

    async def _run_send_receipt_turn(self, conversation_id: str) -> None:
        """Case B：模型已收尾后收到打断/失败回执，触发新轮处理。"""
        async with self.reply_lock:
            receipt_records = await self._consume_send_receipts(conversation_id)
            if not receipt_records:
                return
            await self.history.add_records(receipt_records, conversation_id=conversation_id)
            receipt_block = "\n".join(
                r.get("content", "")
                for r in receipt_records
                if "<send_receipt>" in str(r.get("content") or "")
            )
            task_context = (
                "<send_receipt_task priority=\"high\">\n"
                "处理下面的运行时发送回执，按 JSON 字段判断：\n"
                f"{receipt_block}\n"
                "未发出的消息不要原样自动补发，先结合新消息判断；仍需回应时发送调整后的消息。\n"
                "</send_receipt_task>"
            )
            target = self._target_from_conversation_id(conversation_id)
            await self.run_one_turn(
                task_context,
                lock_already_held=True,
                default_target=target,
                conversation_id=conversation_id,
                task_contract="处理发送回执和新消息",
                task_phase="send_receipt",
            )

    def _format_send_receipt(self, receipt: dict[str, Any]) -> str:
        return (
            "<send_receipt>\n"
            "系统说明：运行时发送状态；按 JSON 字段判断，未发不要原样自动补发，可重判后调整发送。\n"
            f"{json.dumps(receipt, ensure_ascii=False)}\n"
            "</send_receipt>"
        )

    def _target_from_conversation_id(self, conversation_id: str) -> Target | None:
        if ":" not in conversation_id:
            return None
        scope, target_id = conversation_id.split(":", 1)
        if scope not in {"private", "group"}:
            return None
        return Target(adapter=self.adapter.name, scope=scope, target_id=target_id)  # type: ignore[arg-type]

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
            ensure_current_records=_CURRENT_CONVERSATION_MIN_RECORDS,
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
                _PROACTIVE_ROUTER_HISTORY_BUDGET,
            ),
            conversation_id=None,
            ensure_current_records=0,
            log_context="proactive_router",
            log_level=logging.DEBUG,
        )

    def _working_history_budget(self) -> int:
        budget = self._context_budget()
        return max(
            4096,
            budget.total_input_budget
            - budget.memory_token_budget
            - budget.summary_token_budget
            - _PREFIX_ESTIMATE_TOKENS,
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
            trigger = int(self._context_budget().max_context_tokens * 0.75)
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
        selected_indices: set[int] = set()
        noise_indices = _working_history_noise_indices(
            records,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
        )
        optional_runtime_indices = _working_history_optional_runtime_indices(
            records,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
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
        return self._filter_working_history_runtime_noise(
            selected,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
            log_context=log_context,
            log_level=log_level,
        )

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

        drop_indices = _working_history_noise_indices(
            records,
            conversation_id=conversation_id,
            ensure_current_records=ensure_current_records,
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
            _WORKING_HISTORY_RECENT_RUNTIME_RECORDS,
            _WORKING_HISTORY_SEND_RECEIPT_KEEP,
        )
        return filtered

    async def _do_send(self, action: dict) -> str | None:
        """把单个 collected action 真实发送出去。

        失败时写一条 system_note 到历史，让 Agent 下次能看到"哪条没发出去"，
        避免 AI 误以为消息已成功传达。
        """
        scope = action.get("action", "")
        target_id = action.get("target", "")
        content = action.get("content", "")
        label = action.get("label", "")
        kind = action.get("kind", "text")
        if kind != "voice" and not content:
            return None

        # collected 字典里 "action" 字段历史上叫法是 "private"|"group"，与 Target.scope 一致
        if scope not in ("private", "group"):
            await self.history.add_system_note(
                f"⚠ 发送被丢弃：未知 scope={scope!r} target_id={target_id}"
            )
            return None

        target = Target(
            adapter=self.adapter.name,
            scope=scope,
            target_id=target_id,
        )

        try:
            if kind == "voice":
                audio_path = action.get("audio_path")
                if not audio_path:
                    await self.history.add_system_note(
                        f"⚠ 发送失败 → {label or target_id}（缺少语音文件路径）"
                    )
                    return None
                send_voice = getattr(self.adapter, "send_voice", None)
                if send_voice is None:
                    await self.history.add_system_note(
                        f"⚠ 发送失败 → {label or target_id}（当前适配器不支持发送语音）"
                    )
                    return None
                msg_id = await send_voice(target, Path(audio_path))
                self.mark_activity()
                if msg_id is not None:
                    self._record_successful_outbound(
                        action,
                        conversation_id=f"{scope}:{target_id}",
                        msg_id=str(msg_id),
                    )
                return msg_id
            msg_id = await self.adapter.send_text(target, content)
            self.mark_activity()
            if msg_id is not None:
                self._record_successful_outbound(
                    action,
                    conversation_id=f"{scope}:{target_id}",
                    msg_id=str(msg_id),
                )
            return msg_id
        except Exception as e:
            logger.error(f"发送失败 {target}: {e}")
            await self.history.add_system_note(
                f"⚠ 发送失败 → {label or target_id}（{type(e).__name__}: {e}）"
            )
            return None

    async def _send_scheduled_message(
        self,
        target: dict[str, Any],
        message_text: str,
    ) -> None:
        """执行 mode=send_message 的定时发送，不经过模型。"""
        target_type = target.get("target_type")
        target_id = target.get("target_id")
        if target_type not in {"private", "group"} or target_id is None:
            return
        content = (message_text or "").strip()
        if not content:
            return
        action = {
            "action": target_type,
            "target": str(target_id),
            "content": content,
            "label": content,
            "delay": 0.0,
        }
        await self._execute_collected([action])

    # ============================================================
    # 唤醒入口（WakeupScheduler 触发时调用）
    # ============================================================

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
            tool_denylist=_OUT_OF_BAND_DENIED_TOOLS,
        )

    # ============================================================
    # 通用 Agent 调用：供 recall_handler / request_handler / proactive_loop 复用
    # ============================================================

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

        history_window = await self._select_working_history(conversation_id)
        important_text = await self._important_memory_text(conversation_id)
        rag_context_text = await self._rag_context_text(
            conversation_id,
            query=user_event or task_context,
            before_ts=_earliest_record_ts(history_window),
        )
        messages = build_messages(
            persona=self.persona,
            history=history_window,
            important_memory_text=important_text,
            rag_context_text=rag_context_text,
            rolling_summary_text=self._rolling_summary_text(),
            current_context=task_context,
            memory_mode=self.features_cfg.long_term_memory.mode,
            user_event=user_event,
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

    # ============================================================
    # 构造 ToolContext —— 集中点
    # ============================================================

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
            workspace_dir=self.workspace_dir,
            emoji_dir=self.emoji_dir,
            typing_chars_per_second=self.behavior_cfg.typing.chars_per_second,
            typing_max_delay_seconds=self.behavior_cfg.typing.max_delay_seconds,
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

    def _same_workspace_path(self, left: Any, right: Any) -> bool:
        left_s = str(left or "").strip()
        right_s = str(right or "").strip()
        if not left_s or not right_s:
            return False
        if self.workspace_dir is None:
            return left_s.replace("\\", "/") == right_s.replace("\\", "/")
        try:
            left_path = _resolve_agent_workspace_path(left_s, self.workspace_dir)
            right_path = _resolve_agent_workspace_path(right_s, self.workspace_dir)
            return left_path.resolve(strict=False) == right_path.resolve(strict=False)
        except Exception:
            return left_s.replace("\\", "/") == right_s.replace("\\", "/")

    def _record_successful_outbound(
        self,
        action: dict[str, Any],
        *,
        conversation_id: str,
        msg_id: str,
    ) -> None:
        """记录 QQ 上真实发送成功的出站消息。"""
        self_id = self._self_id_by_conversation.get(conversation_id) or None
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
        )

    # ============================================================
    # 私有辅助 —— 待 GPT 填实的部分
    # ============================================================

    def _build_task_context(self, now: str, conversation_id: str | None = None) -> str:
        """组装本轮 task_context 文本。

        组成：当前时间 + 表情包提示 + CQ 引用提醒 + 待处理请求 + 拆句提醒
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
            recent_group = self._recent_group_context(conversation_id)
            if recent_group:
                parts.append(recent_group)

        pending_info = self.pending_requests.to_prompt_text()
        if pending_info:
            parts.append(pending_info)

        return "\n".join(parts)

    def _recent_group_context(self, conversation_id: str, *, limit: int = 10) -> str:
        """给群聊轮次追加最近真实 QQ 可见消息，帮助判断发言对象和断层。"""
        if not conversation_id.startswith("group:"):
            return ""
        messages = self.chat_timeline.recent(conversation_id, limit)
        if not messages:
            return ""
        markdown = self.chat_timeline.to_markdown(messages)
        if not markdown:
            return ""
        return (
            f'<recent_group_messages source="qq_visible" limit="{limit}">\n'
            "以下是当前群最近的真实 QQ 可见消息，用来判断最近几条消息实际在对谁说、"
            "是否有插话、引用或断层。它们不是新的用户指令。\n"
            f"{markdown}\n"
            "</recent_group_messages>"
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
        if item.text.strip().startswith(("/", "#")):
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

    # ============================================================
    # 后台子 Agent 任务
    # ============================================================

    async def _start_agent_task(
        self,
        payload: dict[str, Any],
        *,
        conversation_id: str | None,
        default_target: Target | None,
    ) -> dict[str, Any]:
        """运行资料处理子 Agent，并把结果作为当前工具结果返回。"""
        if self.workspace_dir is None:
            return {"ok": False, "error": "workspace 未配置，无法启动后台子 Agent 任务"}

        task_id = f"agent-{int(time.time() * 1000)}-{len(self._agent_task_meta) + 1}"
        source_hash = str(payload.get("_source_hash") or _agent_task_source_hash(payload.get("sources") or []))
        prompt_hash = str(payload.get("_prompt_hash") or _agent_task_prompt_hash(payload))
        output_name = str(payload.get("output_name") or "")
        dedupe_key = str(
            payload.get("_dedupe_key")
            or _agent_task_dedupe_key(
                source_hash=source_hash,
                prompt_hash=prompt_hash,
                output_name=output_name,
            )
        )
        self._agent_task_meta[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "conversation_id": conversation_id,
            "source_hash": source_hash,
            "prompt_hash": prompt_hash,
            "dedupe_key": dedupe_key,
        }
        self.mark_activity()
        return await self._run_agent_task(
            task_id,
            payload,
            conversation_id=conversation_id,
            default_target=default_target,
        )

    async def _run_agent_task(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        conversation_id: str | None,
        default_target: Target | None,
    ) -> dict[str, Any]:
        task_dir = self.workspace_dir / "agent_tasks" / task_id if self.workspace_dir else None
        try:
            if task_dir is None:
                raise RuntimeError("workspace 未配置")
            task_dir.mkdir(parents=True, exist_ok=True)
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise RuntimeError("后台子 Agent 任务缺少 prompt")

            output_format = str(payload.get("output_format") or "markdown")
            suffix = {"markdown": ".md", "json": ".json", "text": ".txt"}.get(output_format, ".md")
            output_name = _safe_agent_task_filename(
                str(payload.get("output_name") or f"result{suffix}"),
                default=f"result{suffix}",
                suffix=suffix,
            )
            output_path = task_dir / output_name
            max_loops = _clamp_agent_task_max_loops(
                payload.get("max_loops"),
                int(getattr(getattr(self.chat_agent, "cfg", None), "max_loops", 25) or 25),
            )
            first_token_timeout = float(
                getattr(getattr(self.chat_agent, "cfg", None), "first_token_timeout_seconds", 30.0)
                or 30.0
            )
            timeout_seconds = _agent_task_timeout_seconds(
                payload.get("timeout_seconds"),
                max_loops=max_loops,
                first_token_timeout=first_token_timeout,
            )
            source_manifest = await self._materialize_agent_task_sources(
                payload.get("sources") or [],
                task_dir,
            )
            manifest_path = task_dir / "sources.json"
            manifest_path.write_text(
                json.dumps(source_manifest, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            self._agent_task_meta.setdefault(task_id, {"task_id": task_id})
            self._agent_task_meta[task_id].update(
                {
                    "status": "running",
                    "output_path": _workspace_rel(output_path, self.workspace_dir),
                    "result_file": _workspace_rel(output_path, self.workspace_dir),
                    "manifest_path": _workspace_rel(manifest_path, self.workspace_dir),
                    "manifest_summary": _summarize_agent_task_manifest(source_manifest),
                    "timeout_seconds": timeout_seconds,
                }
            )
            logger.info(
                "后台子 Agent 任务启动 task_id=%s max_loops=%s timeout=%.1fs output=%s",
                task_id,
                max_loops,
                timeout_seconds,
                _workspace_rel(output_path, self.workspace_dir),
            )

            from tools import ToolContext, ToolRegistry, get_default_specs

            allowed = {
                "no_action",
                "tool_search",
                "read_file",
                "list_files",
                "write_file",
                "run_python",
                "get_forward_msg",
                "get_recent_chat_messages",
                "recall_history",
            }
            if self.vision is not None:
                allowed.add("describe_image")
            sub_registry = ToolRegistry(
                [spec for spec in get_default_specs() if spec.name in allowed]
            )
            sub_ctx = ToolContext(
                adapter=self.adapter,
                history=self.history,
                archive=self.archive,
                vision=self.vision,
                workspace_dir=self.workspace_dir,
                conversation_id=conversation_id,
                default_history_fetch_count=self.behavior_cfg.default_history_fetch_count,
                tool_result_default_budget_tokens=self.behavior_cfg.context.tool_result_default_budget_tokens,
                tool_result_default_hard_cap_tokens=self.behavior_cfg.context.tool_result_default_hard_cap_tokens,
                tool_result_budgets=dict(self.behavior_cfg.context.tool_result_budgets),
                tool_result_soft_limit_tokens=self.behavior_cfg.context.tool_result_soft_limit_tokens,
                tool_result_hard_cap_tokens=self.behavior_cfg.context.tool_result_hard_cap_tokens,
                tool_result_soft_overrides=dict(self.behavior_cfg.context.tool_result_soft_overrides),
                activity_cb=self.mark_activity,
                extras={
                    "chat_timeline": self.chat_timeline,
                    "tool_registry": sub_registry,
                    "tool_search_approved_tools": set(),
                },
            )
            output_rel = _workspace_rel(output_path, self.workspace_dir)
            manifest_rel = _workspace_rel(manifest_path, self.workspace_dir)
            sub_executor = sub_registry.get_executor(sub_ctx)

            async def _sub_executor(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
                result = await sub_executor(tool_name, args)
                if (
                    tool_name == "write_file"
                    and result.get("ok", False)
                    and self._same_workspace_path(result.get("path"), output_rel)
                ):
                    result = dict(result)
                    result["stop_after_tool"] = True
                    result["next"] = (
                        "目标结果文件已写出，本后台任务将立即结束并把结果返回给主 Agent。"
                    )
                return result

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是后台资料处理子 Agent。你只处理资料整理、提取、转换和分析任务，"
                        "不要联系用户，不要发送消息，不要改记忆。\n"
                        "你可以读取 workspace 文件、检索本地历史、读取合并转发、运行 workspace 内的 Python，"
                        "并用 write_file 写出结果。\n"
                        f"必须把完整结果写入 workspace 文件：{output_rel}。\n"
                        "写完后调用 no_action 结束。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"task_id: {task_id}\n"
                        f"输出格式: {output_format}\n"
                        f"资料清单文件: {manifest_rel}\n\n"
                        f"任务说明：\n{prompt}"
                    ),
                },
            ]
            timeout_with_existing_output = False
            try:
                result = await asyncio.wait_for(
                    self.chat_agent.run(
                        messages,
                        tools=sub_registry.get_schemas(),
                        tool_executor=_sub_executor,
                        task_contract=f"后台资料处理任务 {task_id}",
                        max_loops=max_loops,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "后台子 Agent 任务超时 task_id=%s timeout=%.1fs output_exists=%s",
                    task_id,
                    timeout_seconds,
                    output_path.exists(),
                )
                if output_path.exists():
                    timeout_with_existing_output = True
                    result = AgentRunResult(
                        final_content="后台子 Agent 超时，但目标结果文件已经写出。",
                        records=[],
                        loop_count=max_loops,
                        finish_reason="tool_stop",
                    )
                else:
                    raise RuntimeError(
                        f"后台子 Agent 任务超过 {timeout_seconds:.0f}s 仍未产出目标结果文件"
                    ) from None
            status = "partial" if result.finish_reason == "max_loops" else "completed"
            if result.finish_reason == "api_error":
                status = "failed"
            if not output_path.exists():
                fallback = (result.final_content or "").strip()
                if status == "partial":
                    fallback = _agent_task_partial_text(
                        task_id=task_id,
                        prompt=prompt,
                        result=result,
                        output_rel=output_rel,
                        max_loops=max_loops,
                    )
                elif status == "failed":
                    fallback = "后台子 Agent 调用失败，未产出可用结果。"
                elif not fallback:
                    fallback = "后台子 Agent 已结束，但没有写出结果内容。"
                output_path.write_text(fallback, encoding="utf-8")

            self._agent_task_meta.setdefault(task_id, {"task_id": task_id})
            self._agent_task_meta[task_id].update(
                {
                    "status": status,
                    "result_file": _workspace_rel(output_path, self.workspace_dir),
                    "finish_reason": result.finish_reason,
                    "loop_count": result.loop_count,
                    "timeout_with_existing_output": timeout_with_existing_output,
                }
            )
            error_text = (
                "后台任务超时，但目标结果文件已写出，已按现有结果返回。"
                if timeout_with_existing_output
                else "达到工具循环上限，已产出部分结果。"
                if status == "partial"
                else ""
            )
            content = output_path.read_text(encoding="utf-8", errors="replace")
            rel_path = _workspace_rel(output_path, self.workspace_dir)
            preview = _file_head_tail_preview(output_path)
            summary = _first_meaningful_line(content) or "后台子 Agent 已写出结果"
            return {
                "ok": status != "failed",
                "status": status,
                "brief": f"后台子 Agent 任务已结束：{summary}",
                "task_id": task_id,
                "result_file": rel_path,
                "path": rel_path,
                "content": content,
                "summary": summary,
                "error": error_text,
                "preview": preview,
                "data": {
                    "task_id": task_id,
                    "status": status,
                    "result_file": rel_path,
                    "summary": summary,
                    "manifest_file": manifest_rel,
                    "manifest_summary": _summarize_agent_task_manifest(source_manifest),
                    "max_loops": max_loops,
                    "timeout_seconds": timeout_seconds,
                    "loop_count": result.loop_count,
                    "finish_reason": result.finish_reason,
                    "timeout_with_existing_output": timeout_with_existing_output,
                },
                "next": (
                    "结果已作为本工具返回；如果 content 被截断，可用 read_file 读取 result_file。"
                ),
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("后台子 Agent 任务失败 task_id=%s", task_id)
            error_path = None
            if task_dir is not None:
                try:
                    task_dir.mkdir(parents=True, exist_ok=True)
                    error_path = task_dir / "error.txt"
                    error_path.write_text(str(e), encoding="utf-8")
                except Exception:
                    error_path = None
            self._agent_task_meta.setdefault(task_id, {"task_id": task_id})
            self._agent_task_meta[task_id].update(
                {
                    "status": "failed",
                    "result_file": _workspace_rel(error_path, self.workspace_dir)
                    if error_path
                    else "",
                    "error": str(e),
                }
            )
            rel_path = _workspace_rel(error_path, self.workspace_dir) if error_path else ""
            content = (
                error_path.read_text(encoding="utf-8", errors="replace")
                if error_path and error_path.exists()
                else str(e)
            )
            return {
                "ok": False,
                "status": "failed",
                "brief": f"后台子 Agent 任务失败：{str(e)[:160]}",
                "task_id": task_id,
                "result_file": rel_path,
                "path": rel_path,
                "content": content,
                "summary": "后台子 Agent 任务失败",
                "error": str(e),
                "data": {
                    "task_id": task_id,
                    "status": "failed",
                    "result_file": rel_path,
                    "summary": "后台子 Agent 任务失败",
                },
                "next": "请根据 error 决定是否重试或改用更小的资料范围。",
            }

    async def _materialize_agent_task_sources(
        self,
        sources: Any,
        task_dir: Path,
    ) -> dict[str, Any]:
        """把多种 source 解析为 workspace 文件，避免大材料经过工具结果通道。"""
        source_items = sources if isinstance(sources, list) else []
        manifest: dict[str, Any] = {"count": 0, "sources": []}
        for idx, raw in enumerate(source_items, start=1):
            if not isinstance(raw, dict):
                continue
            source_type = str(raw.get("type") or "")
            item: dict[str, Any] = {"index": idx, "type": source_type}
            try:
                if source_type in {"workspace_path", "tool_result_file", "image_ref"}:
                    value = str(raw.get("value") or "").strip()
                    if source_type == "image_ref" and value.startswith(("http://", "https://")):
                        raise ValueError("image_ref 暂不支持直接传 URL")
                    path = _resolve_agent_workspace_path(value, self.workspace_dir)
                    item["path"] = _workspace_rel(path, self.workspace_dir)
                    item["exists"] = path.exists()
                elif source_type == "workspace_glob":
                    pattern = str(raw.get("value") or "*").strip() or "*"
                    root = self.workspace_dir.resolve(strict=False)
                    matches = [
                        _workspace_rel(p, self.workspace_dir)
                        for p in root.glob(pattern)
                        if p.is_file() and _is_within(p, root)
                    ][:500]
                    item["paths"] = matches
                    item["count"] = len(matches)
                elif source_type == "directory":
                    path = _resolve_agent_workspace_path(str(raw.get("value") or "."), self.workspace_dir)
                    entries = []
                    if path.is_dir():
                        for child in sorted(path.iterdir())[:500]:
                            entries.append(
                                {
                                    "path": _workspace_rel(child, self.workspace_dir),
                                    "type": "dir" if child.is_dir() else "file",
                                    "size": child.stat().st_size if child.is_file() else None,
                                }
                            )
                    item["path"] = _workspace_rel(path, self.workspace_dir)
                    item["entries"] = entries
                elif source_type == "inline_text":
                    text_path = task_dir / f"source_{idx}.txt"
                    text_path.write_text(str(raw.get("value") or ""), encoding="utf-8")
                    item["path"] = _workspace_rel(text_path, self.workspace_dir)
                elif source_type == "inline_json":
                    json_path = task_dir / f"source_{idx}.json"
                    json_path.write_text(
                        json.dumps(raw.get("data"), ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(json_path, self.workspace_dir)
                elif source_type == "forward_id":
                    if self.adapter is None:
                        raise ValueError("adapter 未就绪，无法读取合并转发")
                    from tools.platform_tools import (
                        build_forward_tree,
                        summarize_forward_tree,
                        write_forward_artifact,
                    )

                    forward_id = str(raw.get("value") or "").strip()
                    tree = await build_forward_tree(
                        self.adapter,
                        forward_id,
                        recursive=True,
                        max_depth=3,
                    )
                    forward_path = write_forward_artifact(
                        self.workspace_dir,
                        tree,
                        output="json",
                        prefix=f"agent_source_{idx}",
                    )
                    summary = summarize_forward_tree(tree)
                    item["path"] = _workspace_rel(forward_path, self.workspace_dir)
                    item["message_count"] = summary["message_count"]
                    item["nested_forward_count"] = summary["nested_forward_count"]
                    item["expired_forward_count"] = summary["expired_forward_count"]
                    item["image_count"] = summary["image_count"]
                elif source_type == "conversation_history":
                    records = await self._agent_task_history_records(raw)
                    history_path = task_dir / f"history_{idx}.json"
                    history_path.write_text(
                        json.dumps(records, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(history_path, self.workspace_dir)
                    item["record_count"] = len(records)
                elif source_type == "message_id":
                    records = await self._agent_task_message_records(str(raw.get("value") or ""))
                    msg_path = task_dir / f"message_{idx}.json"
                    msg_path.write_text(
                        json.dumps(records, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(msg_path, self.workspace_dir)
                    item["record_count"] = len(records)
                elif source_type == "tool_call_id":
                    records = await self._agent_task_tool_records(str(raw.get("value") or ""))
                    tool_path = task_dir / f"tool_call_{idx}.json"
                    tool_path.write_text(
                        json.dumps(records, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(tool_path, self.workspace_dir)
                    item["record_count"] = len(records)
                else:
                    item["error"] = f"不支持的 source type: {source_type}"
            except Exception as e:
                item["error"] = str(e)
            manifest["sources"].append(item)
        manifest["count"] = len(manifest["sources"])
        return manifest

    async def _agent_task_history_records(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        conversation_id = source.get("conversation_id")
        keyword = source.get("keyword")
        time_range = source.get("time_range")
        limit = max(1, min(int(source.get("limit") or 50), 500))
        records: list[dict[str, Any]] = []
        if self.archive is not None:
            records.extend(
                await self.archive.search(
                    conversation_id=conversation_id,
                    keyword=keyword,
                    time_range=time_range,
                    limit=limit,
                )
            )
        if self.history is not None:
            for record in await self.history.records():
                if _agent_record_matches(
                    record,
                    conversation_id=conversation_id,
                    keyword=keyword,
                    time_range=time_range,
                ):
                    records.append(record)
            records = records[-limit:]
        return records

    async def _agent_task_message_records(self, message_id: str) -> list[dict[str, Any]]:
        if not message_id:
            return []
        records = await self._all_history_records()
        return [record for record in records if _record_has_message_id(record, message_id)]

    async def _agent_task_tool_records(self, tool_call_id: str) -> list[dict[str, Any]]:
        if not tool_call_id:
            return []
        records = await self._all_history_records()
        return [
            record
            for record in records
            if str(record.get("tool_call_id") or "") == tool_call_id
        ]

    async def _all_history_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self.archive is not None:
            records.extend(await self.archive.records())
        if self.history is not None:
            records.extend(await self.history.records())
        return records

    async def _send_rate_limit_reply(self, event: IncomingMessage) -> None:
        """非好友超限时发一条限速提示（不入历史）。文案根据当前 rate_limit 配置渲染。"""
        rl = self.behavior_cfg.rate_limit
        text = _RATE_LIMIT_REPLY_TEMPLATE.format(
            window_seconds=rl.window_seconds, max_messages=rl.max_messages
        )
        try:
            msg_id = await self.adapter.send_text(event.source_target, text)
            if msg_id is not None:
                conversation_id = self._conversation_id_from_event(event)
                self._self_id_by_conversation[conversation_id] = str(
                    getattr(event, "self_id", "") or ""
                )
                self._record_successful_outbound(
                    {
                        "target_scope": event.source_target.scope,
                        "target_id": event.source_target.target_id,
                        "content": text,
                        "label": text,
                        "kind": "text",
                    },
                    conversation_id=conversation_id,
                    msg_id=str(msg_id),
                )
        except Exception:
            logger.debug("发送速率超限提示失败（adapter 可能未连接）")

    # ============================================================
    # 历史总结触发（由 SummaryAgent 接管）
    # ============================================================

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
        trigger = self.behavior_cfg.summarize.trigger_at_tokens
        if trigger is None:
            trigger = int(budget.max_context_tokens * 0.75)
        target_after = self.behavior_cfg.summarize.target_after_tokens
        if target_after is None:
            target_after = int(budget.max_context_tokens * 0.50)
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
        if isinstance(new_important_items, list) and new_important_items:
            for item in new_important_items:
                content = (item.get("content") or "").strip() if isinstance(item, dict) else ""
                if content:
                    await self.important.save(content)

        cut_point = len(slice_records)
        await self.history.truncate_head(cut_point)

        important_count = (
            len(new_important_items)
            if isinstance(new_important_items, list)
            else 0
        )
        await self.history.add_system_note(
            f"[滚动摘要] 已归档并移出活跃历史 {cut_point} 条；"
            f"新增重要记忆 {important_count} 条"
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

    # ============================================================
    # 关闭
    # ============================================================

    async def shutdown(self) -> None:
        """优雅停止：取消批处理任务。"""
        tasks = [
            self._batch_task,
            self._requeue_task,
            self._summary_task,
        ]
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning(f"batch_task 取消异常: {e}")
        logger.info("MessagePipeline 已停止")


