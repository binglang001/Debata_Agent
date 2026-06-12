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
import logging
import time
from pathlib import Path
from typing import Any

from adapters.base import IAdapter
from adapters.types import Target as Target
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
    ToolRegistry,
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
from .pipeline_inbound import (
    _RATE_LIMIT_REPLY_TEMPLATE as _RATE_LIMIT_REPLY_TEMPLATE,
)
from .pipeline_inbound import (
    PipelineInboundMixin,
)
from .pipeline_sending import PipelineSendingMixin
from .pipeline_summary import PipelineSummaryMixin
from .pipeline_task_context import PipelineTaskContextMixin
from .pipeline_tool_context import PipelineToolContextMixin
from .pipeline_turns import (
    _OUT_OF_BAND_DENIED_TOOLS as _OUT_OF_BAND_DENIED_TOOLS,
)
from .pipeline_turns import (
    PipelineTurnsMixin,
)
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





_SLOW_BATCH_STAGE_SECONDS = 1.0
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
    PipelineInboundMixin,
    PipelineSendingMixin,
    PipelineSummaryMixin,
    PipelineTaskContextMixin,
    PipelineTurnsMixin,
    PipelineToolContextMixin,
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


