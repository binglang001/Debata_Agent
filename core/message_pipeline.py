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

from .agent_task_helpers import (
    _agent_task_timeout_seconds as _agent_task_timeout_seconds,
)
from .agent_task_helpers import (
    _resolve_agent_workspace_path as _resolve_agent_workspace_path,
)
from .chat_timeline import ChatTimelineStore
from .media_pipeline import MediaPipelineMixin
from .pipeline_agent_tasks import PipelineAgentTasksMixin
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
    _WORKING_HISTORY_SEND_RECEIPT_KEEP as _WORKING_HISTORY_SEND_RECEIPT_KEEP,
)
from .pipeline_history import (
    _assistant_tool_call_names as _assistant_tool_call_names,
)
from .pipeline_history import (
    _no_action_pair_indices as _no_action_pair_indices,
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
from .pipeline_summary import PipelineSummaryMixin
from .pipeline_working_context import PipelineWorkingContextMixin
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





class MessagePipeline(
    PipelineSummaryMixin,
    PipelineAgentTasksMixin,
    PipelineWorkingContextMixin,
    MediaPipelineMixin,
):
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


