"""消息处理管道 —— 整个项目的心脏。

负责：
    1. 接收 IncomingMessage（来自 EventBus）
    2. 速率限制（非好友）
    3. 重建可读文本（CQ 码解析 + 附加媒体 URL）
    4. 关键词强制保存（命中即触发 ImportantMemoryManager.force_save）
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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from adapters.base import IAdapter
from adapters.types import IncomingMessage, MediaType, Target
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
from utils import get_time, parse_raw_cq
from utils.token_budget import TokenBudget, TokenEstimator

from .state import MessageBatch, PendingMessageItem, PendingRequestStore, RateLimiter
from .wakeup import WakeupScheduler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _InboundRef:
    seq: int
    conversation_id: str
    message_id: str
    user_id: str
    nickname: str
    text: str
    received_at: float


@dataclass(slots=True)
class _SendJob:
    send_id: str
    conversation_id: str
    actions: list[dict[str, Any]]
    source_tool: str
    trigger_message_id: str | None
    trigger_inbound_seq: int
    trigger_user_id: str | None
    created_at: float


@dataclass(slots=True)
class _SendConversationState:
    queue: deque[_SendJob] = field(default_factory=deque)
    worker: asyncio.Task | None = None
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_receipts: list[dict[str, Any]] = field(default_factory=list)
    needs_resync: bool = False
    in_flight: bool = False


class _AsyncSendManager:
    """Phase 0 每会话 FIFO 发送队列。

    工具调用只入队并立即返回；后台 worker 逐条真实发送。只有清洁完成以外的
    回执会投递给模型，且所有回执都只追加、不回改旧历史。
    """

    def __init__(self, pipeline: "MessagePipeline") -> None:
        self.pipeline = pipeline
        self._states: dict[str, _SendConversationState] = {}
        self._recent_inbound: dict[str, list[_InboundRef]] = {}
        self._send_counter = 0
        self._active_model_conversation: str | None = None

    def begin_model_turn(self, conversation_id: str | None) -> None:
        self._active_model_conversation = conversation_id

    def end_model_turn(self, conversation_id: str | None) -> None:
        if self._active_model_conversation == conversation_id:
            self._active_model_conversation = None

    def is_model_active(self, conversation_id: str) -> bool:
        return self._active_model_conversation == conversation_id

    def has_in_flight(self, conversation_id: str) -> bool:
        state = self._states.get(conversation_id)
        return bool(state and (state.in_flight or state.queue))

    def should_defer_batch(self, conversation_id: str) -> bool:
        state = self._states.get(conversation_id)
        return bool(state and state.needs_resync)

    def notify_inbound(self, item: PendingMessageItem) -> None:
        ref = _InboundRef(
            seq=item.inbound_seq,
            conversation_id=item.conversation_id,
            message_id=item.message_id,
            user_id=item.user_id,
            nickname=item.nickname,
            text=item.text,
            received_at=item.received_at,
        )
        recent = self._recent_inbound.setdefault(item.conversation_id, [])
        recent.append(ref)
        if len(recent) > 200:
            del recent[:-200]

        msg = self._inbound_to_receipt_message(ref)
        state = self._state(item.conversation_id)
        if state.in_flight or state.queue:
            state.needs_resync = True
            state.interrupt_messages.append(msg)
            state.interrupt_event.set()
            return

        if not self.is_model_active(item.conversation_id):
            return

        state.needs_resync = True
        state.interrupt_messages.append(msg)

        # LLM 正在思考但还没有发送在途：也要把新消息作为回执边界带回模型。
        receipt = self._find_or_create_model_interrupt_receipt(
            state,
            item.conversation_id,
        )
        receipt["new_messages"].append(msg)

    async def submit(
        self,
        actions: list[dict[str, Any]],
        source_tool: str,
        *,
        trigger_message_id: str | None = None,
        trigger_inbound_seq: int = 0,
        trigger_user_id: str | None = None,
    ) -> dict[str, Any]:
        send_id = self._next_send_id()
        normalized = [self._normalize_action(a) for a in actions]
        if not normalized:
            return {"ok": True, "status": "sent", "send_id": send_id, "count": 0, "sent": []}

        groups: dict[str, list[dict[str, Any]]] = {}
        for action in normalized:
            groups.setdefault(self._conversation_id(action), []).append(action)

        stale_convs = [cid for cid in groups if self._state(cid).needs_resync]
        if stale_convs:
            return {
                "ok": False,
                "status": "stale",
                "send_id": send_id,
                "note": "该会话刚来新消息，请先看新消息再决定发不发",
                "stale_conversations": stale_convs,
            }

        can_sync = all(self._can_sync_send(cid, acts) for cid, acts in groups.items())
        if can_sync:
            return await self._send_sync(
                send_id,
                groups,
                source_tool,
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
            )

        for conversation_id, group_actions in groups.items():
            state = self._state(conversation_id)
            job = _SendJob(
                send_id=send_id,
                conversation_id=conversation_id,
                actions=group_actions,
                source_tool=source_tool,
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
                created_at=time.monotonic(),
            )
            state.queue.append(job)
            if state.worker is None or state.worker.done():
                state.worker = asyncio.create_task(self._worker(conversation_id, state))

        return {
            "ok": True,
            "status": "queued",
            "send_id": send_id,
            "note": "已进入发送队列；正常发完只静默记历史，被打断或失败才会追加 send_receipt。",
        }

    def pop_pending_receipts(self, conversation_id: str) -> list[dict[str, Any]]:
        state = self._state(conversation_id)
        receipts = state.pending_receipts[:]
        state.pending_receipts.clear()
        return receipts

    def mark_receipts_delivered(self, conversation_id: str) -> None:
        state = self._state(conversation_id)
        state.needs_resync = False
        state.interrupt_messages.clear()
        state.interrupt_event.clear()

    def _state(self, conversation_id: str) -> _SendConversationState:
        state = self._states.get(conversation_id)
        if state is None:
            state = _SendConversationState()
            self._states[conversation_id] = state
        return state

    def _next_send_id(self) -> str:
        self._send_counter += 1
        return f"send-{int(time.time() * 1000)}-{self._send_counter}"

    @staticmethod
    def _conversation_id(action: dict[str, Any]) -> str:
        return f"{action['target_scope']}:{action['target_id']}"

    @staticmethod
    def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": action.get("kind", "text"),
            "order": int(action.get("order", 0)),
            "target_scope": str(action.get("target_scope")),
            "target_id": str(action.get("target_id")),
            "content": str(action.get("content") or ""),
            "label": str(action.get("label") or action.get("content") or ""),
            "delay": float(action.get("delay") or 0.0),
            "audio_path": str(action.get("audio_path") or ""),
        }

    def _can_sync_send(self, conversation_id: str, actions: list[dict[str, Any]]) -> bool:
        state = self._state(conversation_id)
        if state.in_flight or state.queue or state.needs_resync:
            return False
        if len(actions) == 1:
            return True
        return all(float(a.get("delay") or 0.0) <= 0 for a in actions)

    async def _send_sync(
        self,
        send_id: str,
        groups: dict[str, list[dict[str, Any]]],
        source_tool: str,
        *,
        trigger_message_id: str | None,
        trigger_inbound_seq: int,
        trigger_user_id: str | None,
    ) -> dict[str, Any]:
        sent: list[dict[str, Any]] = []
        errors: list[str] = []
        for conversation_id, actions in groups.items():
            for index, action in enumerate(actions):
                try:
                    msg_id = await self._send_one(
                        action,
                        source_tool,
                        conversation_id,
                        trigger_message_id=trigger_message_id,
                        trigger_inbound_seq=trigger_inbound_seq,
                        trigger_user_id=trigger_user_id,
                    )
                    sent.append(self._sent_item(action, msg_id))
                except Exception as e:  # noqa: BLE001
                    logger.exception("同步发送失败 conversation_id=%s: %s", conversation_id, e)
                    errors.append(f"{conversation_id}: {e}")

        result: dict[str, Any] = {
            "ok": bool(sent) or not errors,
            "status": "sent",
            "send_id": send_id,
            "count": len(sent),
            "sent": sent,
        }
        if errors:
            result["errors"] = errors
        return result

    async def _worker(self, conversation_id: str, state: _SendConversationState) -> None:
        try:
            while state.queue:
                job = state.queue.popleft()
                state.in_flight = True
                sent: list[dict[str, Any]] = []
                errors: list[str] = []
                interrupted = False
                unsent: list[dict[str, Any]] = []

                for index, action in enumerate(job.actions):
                    if state.interrupt_event.is_set():
                        interrupted = True
                        unsent.extend(self._unsent_items(job.actions[index:], job.send_id))
                        break

                    try:
                        msg_id = await self._send_one(
                            action,
                            job.source_tool,
                            conversation_id,
                            trigger_message_id=job.trigger_message_id,
                            trigger_inbound_seq=job.trigger_inbound_seq,
                            trigger_user_id=job.trigger_user_id,
                        )
                        sent.append(self._sent_item(action, msg_id))
                    except Exception as e:  # noqa: BLE001
                        logger.exception("异步发送失败 conversation_id=%s: %s", conversation_id, e)
                        errors.append(f"order={action.get('order')}: {e}")
                        continue

                    delay = float(action.get("delay") or 0.0)
                    if delay > 0 and index < len(job.actions) - 1:
                        try:
                            await asyncio.wait_for(state.interrupt_event.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                        if state.interrupt_event.is_set():
                            interrupted = True
                            unsent.extend(self._unsent_items(job.actions[index + 1 :], job.send_id))
                            break

                if interrupted:
                    unsent.extend(self._flush_queued_unsent(state))

                receipt = {
                    "type": "send_receipt",
                    "send_id": job.send_id,
                    "conversation_id": conversation_id,
                    "sent": sent,
                    "unsent": unsent,
                    "interrupted": interrupted,
                    "new_messages": list(state.interrupt_messages),
                }
                if errors:
                    receipt["errors"] = errors
                clean = not interrupted and not errors
                await self._handle_receipt(conversation_id, receipt, clean=clean)

                if interrupted:
                    state.interrupt_event.clear()
                    state.interrupt_messages.clear()
                    break
        finally:
            state.in_flight = False
            state.worker = None
            if state.queue:
                state.worker = asyncio.create_task(self._worker(conversation_id, state))

    async def _send_one(
        self,
        action: dict[str, Any],
        source_tool: str,
        conversation_id: str,
        *,
        trigger_message_id: str | None,
        trigger_inbound_seq: int,
        trigger_user_id: str | None,
    ) -> str | None:
        target = Target(
            adapter=self.pipeline.adapter.name,
            scope=action["target_scope"],  # type: ignore[arg-type]
            target_id=action["target_id"],
        )
        kind = action.get("kind", "text")
        if kind == "voice":
            send_voice = getattr(self.pipeline.adapter, "send_voice", None)
            if send_voice is None:
                raise RuntimeError("当前适配器不支持发送语音")
            msg_id = await send_voice(target, Path(action.get("audio_path") or ""))
        else:
            content = action.get("content") or ""
            msg_id = await self.pipeline.adapter.send_text(target, content)

        self.pipeline.mark_activity()
        logger.debug(
            "出站气泡 sent_at_ms=%s msg_id=%s source=%s conversation_id=%s "
            "trigger_msg_id=%s order=%s kind=%s",
            int(time.time() * 1000),
            msg_id,
            source_tool,
            conversation_id,
            trigger_message_id,
            action.get("order"),
            kind,
        )
        return msg_id

    @staticmethod
    def _sent_item(action: dict[str, Any], msg_id: str | None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "order": int(action.get("order", 0)),
            "target_type": action.get("target_scope"),
            "target_id": action.get("target_id"),
            "msg_id": str(msg_id) if msg_id is not None else None,
        }
        if action.get("target_scope") == "private":
            item["target_qq"] = action.get("target_id")
        if action.get("target_scope") == "group":
            item["group_id"] = action.get("target_id")
        return item

    @staticmethod
    def _unsent_items(actions: list[dict[str, Any]], send_id: str) -> list[dict[str, Any]]:
        return [
            {
                "send_id": send_id,
                "order": int(action.get("order", 0)),
                "target_type": action.get("target_scope"),
                "target_id": action.get("target_id"),
                "content": action.get("label") or action.get("content") or "",
            }
            for action in actions
        ]

    def _flush_queued_unsent(self, state: _SendConversationState) -> list[dict[str, Any]]:
        unsent: list[dict[str, Any]] = []
        while state.queue:
            queued = state.queue.popleft()
            unsent.extend(self._unsent_items(queued.actions, queued.send_id))
        return unsent

    async def _handle_receipt(
        self,
        conversation_id: str,
        receipt: dict[str, Any],
        *,
        clean: bool,
    ) -> None:
        if clean:
            await self.pipeline._record_clean_send_receipt(receipt)
            return
        state = self._state(conversation_id)
        state.pending_receipts.append(receipt)
        if not self.is_model_active(conversation_id):
            self.pipeline._schedule_send_receipt_turn(conversation_id)

    def _find_or_create_model_interrupt_receipt(
        self,
        state: _SendConversationState,
        conversation_id: str,
    ) -> dict[str, Any]:
        for receipt in state.pending_receipts:
            if receipt.get("send_id") is None and receipt.get("interrupted"):
                return receipt
        receipt = {
            "type": "send_receipt",
            "send_id": None,
            "conversation_id": conversation_id,
            "sent": [],
            "unsent": [],
            "interrupted": True,
            "new_messages": [],
            "note": "模型思考期间当前会话来了新消息",
        }
        state.pending_receipts.append(receipt)
        return receipt

    @staticmethod
    def _inbound_to_receipt_message(ref: _InboundRef) -> dict[str, Any]:
        return {
            "nickname": ref.nickname,
            "user_id": ref.user_id,
            "text": ref.text,
            "msg_id": ref.message_id,
        }


# 速率超限时的提示模板（占位符运行时替换）
_RATE_LIMIT_REPLY_TEMPLATE = "已超出速率限制（{window_seconds} 秒内最多 {max_messages} 条），请添加机器人为好友后继续使用"
_PREFIX_ESTIMATE_TOKENS = 12_000
_CURRENT_CONVERSATION_MIN_RECORDS = 8
_PROACTIVE_ROUTER_HISTORY_BUDGET = 16_384


def _recommended_context_budget(model: str, context_length: int | None = None) -> int:
    """按模型名推导工作上下文预算，不等于模型硬上限。"""
    name = (model or "").lower()
    if "deepseek-v4-pro" in name:
        return 350_000
    if "deepseek-v4" in name:
        return 300_000
    if context_length and context_length > 0:
        if context_length >= 1_000_000:
            return 300_000
        if context_length >= 200_000:
            return 150_000
        if context_length >= 128_000:
            return 96_000
        return max(4096, int(context_length * 0.75))
    if "claude" in name:
        return 150_000
    return 96_000


def _record_timestamp(record: dict[str, Any]) -> Any:
    meta = record.get("metadata")
    if isinstance(meta, dict):
        if meta.get("timestamp") is not None:
            return meta.get("timestamp")
        messages = meta.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                return last.get("timestamp")
    return None


def _record_conversation_id(record: dict[str, Any]) -> str | None:
    """从记录里读取会话标签；兼容未迁移的旧 metadata，不回写历史。"""
    if record.get("conversation_id"):
        return str(record.get("conversation_id"))
    meta = record.get("metadata")
    if not isinstance(meta, dict):
        return None

    messages = meta.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            scope = last.get("scope")
            target_id = last.get("target_id")
            group_id = last.get("group_id")
            user_id = last.get("user_id")
            if scope == "group" and (group_id or target_id):
                return f"group:{group_id or target_id}"
            if scope == "private" and (target_id or user_id):
                return f"private:{target_id or user_id}"
            if group_id:
                return f"group:{group_id}"
            if user_id:
                return f"private:{user_id}"

    scope = meta.get("scope")
    target_id = meta.get("target_id")
    group_id = meta.get("group_id")
    user_id = meta.get("user_id")
    if scope == "group" and (group_id or target_id):
        return f"group:{group_id or target_id}"
    if scope == "private" and (target_id or user_id):
        return f"private:{target_id or user_id}"
    if group_id:
        return f"group:{group_id}"
    if user_id:
        return f"private:{user_id}"
    return None


class MessagePipeline:
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

        # 速率限制
        if self.rate_limiter and await self.rate_limiter.check_and_log(event.user_id):
            await self._send_rate_limit_reply(event)
            return

        # 关键词强制保存（命中即写入重要记忆）
        keyword_saved = False
        if event.text and self.features_cfg.long_term_memory.mode != "rag":
            keyword_result = await try_save_from_user(
                event.text,
                self.important,
                enabled=self.features_cfg.long_term_memory.keyword_trigger_save,
            )
            keyword_saved = bool(keyword_result and keyword_result.get("saved"))

        # 重建可读文本（CQ 码 + 媒体）
        text = await self._build_readable_text(event)
        self._inbound_seq += 1
        inbound_seq = self._inbound_seq
        received_at = time.monotonic()
        conversation_id = self._conversation_id_from_event(event)
        logger.debug(
            "入站消息 received_at_ms=%s conversation_id=%s msg_id=%s user_id=%s",
            int(time.time() * 1000),
            conversation_id,
            event.message_id,
            event.user_id,
        )

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

        await self.batch.append(item)
        self._send_manager.notify_inbound(item)
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
        now = get_time()
        user_record = self._build_user_record(items, now)
        conversation_id = user_record.get("conversation_id") or "legacy:unknown"
        await self.history.add_records([user_record], conversation_id=conversation_id)
        logger.info(f"合并处理 {len(items)} 条消息")

        # 构造给 LLM 的 messages（emoji_hint / pending_requests 已在 _build_task_context 内拼装）
        task_context = self._build_task_context(now, conversation_id)

        # RAG 模式下用最新一条 user 消息作 query 召回 top-k；否则注入全部
        if (
            self.features_cfg.long_term_memory.mode == "rag"
            and self.important.rag_enabled
            and items
        ):
            important_text = await self.important.retrieve_for_query(items[-1].text)
        else:
            important_text = self.important.text()

        history_window = await self._select_working_history(conversation_id)
        estimator = self._token_estimator()

        messages = build_messages(
            persona=self.persona,
            history=history_window,
            important_memory_text=important_text,
            rolling_summary_text=self._rolling_summary_text(estimator),
            current_context=task_context,
            memory_mode=self.features_cfg.long_term_memory.mode,
        )

        # 构造 ToolContext
        default_target = items[-1].raw_event.source_target if items else None
        latest_user_text = "\n".join(item.text for item in items)
        ctx = self._build_tool_context(
            default_target=default_target,
            latest_user_text=latest_user_text,
            conversation_id=conversation_id,
            trigger_message_id=items[-1].message_id if items else None,
            trigger_inbound_seq=items[-1].inbound_seq if items else 0,
            trigger_user_id=items[-1].user_id if items else None,
        )
        executor = self.tool_registry.get_executor(ctx)
        tools_schema = self.tool_registry.get_schemas()
        estimated_prompt_tokens = estimator.estimate_messages(messages)
        if tools_schema:
            estimated_prompt_tokens += estimator.estimate_text(str(tools_schema))

        # 只串行模型轮；Phase 0 后台发送不占 reply_lock。
        async with self.reply_lock:
            self._send_manager.begin_model_turn(conversation_id)
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

            # 写 records
            if result.records:
                await self.history.add_records(
                    result.records,
                    conversation_id=conversation_id,
                )

            # Phase 0 后发送类工具已在工具调用内即时发送；这里仅保留兼容兜底。
            await self._execute_collected(ctx.collected)
            self._calibrate_tokens(estimated_prompt_tokens, result.prompt_tokens)
            self.mark_activity()

        self._schedule_summarize()

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
                    "nickname": item.nickname,
                    "user_id": item.user_id,
                    "text": item.text,
                    "msg_id": item.message_id,
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
                    "role": "system",
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
        await self.history.add_system_note(
            f"{get_time()} 发送完成（全部消息已发出）"
            f" send_id={receipt.get('send_id')} msg_ids=[{msg_ids}]",
            conversation_id=conversation_id or None,
        )

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
                if r.get("role") == "system" and r.get("content")
            )
            task_context = (
                "<send_receipt_task priority=\"high\">\n"
                "下面是本轮需要处理的发送状态回执，"
                "请以 sent / unsent / interrupted / new_messages 字段为准：\n"
                f"{receipt_block}\n"
                "已发出的消息保持已发送；"
                "未发出的消息不要自动补发，先结合新消息判断是否需要回应。\n"
                "</send_receipt_task>"
            )
            target = self._target_from_conversation_id(conversation_id)
            messages = build_messages(
                persona=self.persona,
                history=await self._select_working_history(conversation_id),
                important_memory_text=self.important.text(),
                rolling_summary_text=self._rolling_summary_text(),
                current_context=task_context,
                memory_mode=self.features_cfg.long_term_memory.mode,
            )
            ctx = self._build_tool_context(
                default_target=target,
                conversation_id=conversation_id,
            )
            executor = self.tool_registry.get_executor(ctx)
            self._send_manager.begin_model_turn(conversation_id)
            try:
                result = await self.chat_agent.run(
                    messages,
                    tools=self.tool_registry.get_schemas(),
                    tool_executor=executor,
                    task_contract="处理发送回执和新消息",
                    pending_context_provider=lambda: self._consume_send_receipts(
                        conversation_id
                    ),
                )
            finally:
                self._send_manager.end_model_turn(conversation_id)
            if result.records:
                await self.history.add_records(result.records, conversation_id=conversation_id)
            await self._execute_collected(ctx.collected)
            self.mark_activity()

    def _format_send_receipt(self, receipt: dict[str, Any]) -> str:
        return (
            "<send_receipt>\n"
            "发送回执：这是当前会话的发送状态记录，请以 sent / unsent / interrupted / new_messages 字段判断结果。\n"
            "sent 表示已经发出；unsent 表示未发出；interrupted 表示发送是否被新消息中断；new_messages 是中断期间的新消息。\n"
            "未发出的消息不要自动补发，先结合新消息判断是否需要回应。\n"
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
        used = 0

        def add_index(index: int, *, force: bool = False) -> bool:
            nonlocal used
            if index in selected_indices:
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
        return selected

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
                return msg_id
            msg_id = await self.adapter.send_text(target, content)
            self.mark_activity()
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
        await self.history.add_system_note(f"{now} 定时唤醒：{reminder}")
        logger.info(f"定时任务执行 mode={mode}: {reminder!r}")

        if mode == "send_message":
            if target and message_text:
                await self._send_scheduled_message(target, message_text)
            else:
                logger.warning(
                    "mode=send_message 缺少 target 或 message_text，跳过执行"
                )
            return

        target_hint = ""
        conversation_id: str | None = None
        if target:
            target_type = target.get("target_type")
            target_id = target.get("target_id")
            if target_type and target_id is not None:
                conversation_id = f"{target_type}:{target_id}"
                target_hint = (
                    f"\n本次唤醒来自一个明确的提醒目标：{target_type}:{target_id}。"
                    "如果 reminder 要求通知这个目标，请调用发送消息工具；如果任务无需通知，可以 no_action。"
                )
        task_context = (
            "<wakeup_task priority=\"critical\">\n"
            f"现在是{now}。这是定时唤醒，不是新用户消息。\n"
            f"提醒任务：{reminder}\n"
            "提醒任务应已包含设置时的用户原话、提醒目标和具体动作；优先按提醒任务执行。\n"
            "只处理这条提醒任务；不要把历史中已经完成、无关或仅作为背景的请求当作当前任务重复执行。\n"
            "固定消息发送应在设置阶段使用 schedule_wakeup 的 mode=send_message；本模式只处理需要查询、整理、判断或调用工具的复杂任务。\n"
            "只有纯内部继续任务且确实无需通知时才 no_action。"
            f"{target_hint}"
            "\n</wakeup_task>"
        )

        messages = build_messages(
            persona=self.persona,
            history=await self._select_working_history(conversation_id),
            important_memory_text=self.important.text(),
            rolling_summary_text=self._rolling_summary_text(),
            current_context=task_context,
            memory_mode=self.features_cfg.long_term_memory.mode,
        )

        ctx = self._build_tool_context(
            default_target=self._target_from_conversation_id(conversation_id)
            if conversation_id
            else None,
            conversation_id=conversation_id,
        )
        executor = self.tool_registry.get_executor(ctx)
        tools_schema = self.tool_registry.get_schemas()

        async with self.reply_lock:
            self._send_manager.begin_model_turn(conversation_id)
            try:
                result = await self.chat_agent.run(
                    messages,
                    tools=tools_schema,
                    tool_executor=executor,
                    task_contract=f"定时唤醒任务：{reminder}",
                    pending_context_provider=(
                        (lambda: self._consume_send_receipts(conversation_id))
                        if conversation_id
                        else None
                    ),
                )
            finally:
                self._send_manager.end_model_turn(conversation_id)

            if result.records:
                await self.history.add_records(result.records)
            if ctx.collected:
                await self._execute_collected(ctx.collected)
        self.mark_activity()

    # ============================================================
    # 通用 Agent 调用：供 recall_handler / request_handler / proactive_loop 复用
    # ============================================================

    async def run_one_turn(
        self,
        task_context: str,
        *,
        as_system_note: str | None = None,
        lock_already_held: bool = False,
        default_target: Target | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """通用单轮 Agent 入口：注入 task_context，跑一轮，处理 collected。

        Args:
            task_context: 本轮 ephemeral context（时间、事件描述、提醒等）
            as_system_note: 若给，会在调 Agent 前写入 history 作为事件记录
                （如撤回通知、请求通知）
        """
        self.mark_activity()
        if as_system_note:
            await self.history.add_system_note(as_system_note)

        messages = build_messages(
            persona=self.persona,
            history=await self._select_working_history(None),
            important_memory_text=self.important.text(),
            rolling_summary_text=self._rolling_summary_text(),
            current_context=task_context,
            memory_mode=self.features_cfg.long_term_memory.mode,
        )

        ctx = self._build_tool_context(
            default_target=default_target,
            conversation_id=conversation_id,
        )
        executor = self.tool_registry.get_executor(ctx)
        tools_schema = self.tool_registry.get_schemas()

        async def _run_locked() -> None:
            self._send_manager.begin_model_turn(conversation_id)
            try:
                result = await self.chat_agent.run(
                    messages,
                    tools=tools_schema,
                    tool_executor=executor,
                    pending_context_provider=(
                        (lambda: self._consume_send_receipts(conversation_id))
                        if conversation_id
                        else None
                    ),
                )
            finally:
                self._send_manager.end_model_turn(conversation_id)

            if result.records:
                await self.history.add_records(result.records)
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
    ) -> ToolContext:
        """每次 Agent 调用前构造新的 ToolContext。

        collected 是 per-call 的（绝不复用），所以每次新建实例。
        """
        # SummaryAgent 启用时把它的 provider/model 注入给 summarize_chat_history 工具用
        if self.summary_agent is not None:
            summary_provider = self.summary_agent.provider
            summary_model = self.summary_agent.cfg.model
        else:
            summary_provider = None
            summary_model = ""

        extras: dict[str, Any] = {}
        if default_target is not None:
            raw_target_id = str(default_target.target_id)
            extras["default_reply_target"] = {
                "target_type": default_target.scope,
                "target_id": int(raw_target_id) if raw_target_id.isdigit() else raw_target_id,
            }
        if latest_user_text:
            extras["latest_user_message"] = latest_user_text

        async def _send_actions(
            actions: list[dict[str, Any]],
            source_tool: str,
        ) -> dict[str, Any]:
            return await self._send_manager.submit(
                actions,
                source_tool,
                trigger_message_id=trigger_message_id,
                trigger_inbound_seq=trigger_inbound_seq,
                trigger_user_id=trigger_user_id,
            )

        return ToolContext(
            adapter=self.adapter,
            important=self.important,
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
            tool_result_soft_limit_tokens=self.behavior_cfg.context.tool_result_soft_limit_tokens,
            tool_result_hard_cap_tokens=self.behavior_cfg.context.tool_result_hard_cap_tokens,
            tool_result_soft_overrides=dict(self.behavior_cfg.context.tool_result_soft_overrides),
            activity_cb=self.mark_activity,
            send_actions_cb=_send_actions,
            default_history_fetch_count=self.behavior_cfg.default_history_fetch_count,
            summary_provider=summary_provider,
            summary_model=summary_model,
            collected=[],
            extras=extras,
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

        pending_info = self.pending_requests.to_prompt_text()
        if pending_info:
            parts.append(pending_info)

        return "\n".join(parts)

    async def _build_readable_text(self, event: IncomingMessage) -> str:
        """把 IncomingMessage 重建为人类可读文本（CQ 解析 + 媒体 URL/转录附加）。

        events.parse_napcat_event 已经把 raw_message 走过 parse_raw_cq，
        结果存在 event.text 里。这里只在 event.text 为空（异常路径）时回退重算。
        然后扫描 event.media，把占位升级为含 URL / 转录 / workspace 路径的版本：
            - 图片：[图片] → [图片 url=... workspace=<相对路径>]
            - 语音：[语音] → [音频消息: 转录文本 workspace=<相对路径>]
            - 文件：[文件] → [文件 url=... workspace=<相对路径>]
        媒体文件会下载落地到 data/workspace/incoming/，让 AI 可以用 read_file 直接读。
        """
        text = event.text
        if not text and event.raw_message:
            bot_qq = str(getattr(event, "self_id", ""))
            text = parse_raw_cq(event.raw_message, bot_qq)
        text = text or ""

        # 升级媒体占位为含 URL / 转录的版本
        for seg in event.media:
            try:
                if seg.type == MediaType.IMAGE and seg.url:
                    ws_path = await self._save_media_to_workspace(
                        seg.url, suggested_name=f"img_{event.message_id}.jpg"
                    )
                    suffix = f" workspace={ws_path}" if ws_path else ""
                    text = text.replace("[图片]", f"[图片 url={seg.url}{suffix}]", 1)
                elif seg.type in (MediaType.VOICE, MediaType.RECORD):
                    # 先把语音文件落到 workspace，供本地/API ASR 使用；失败不阻塞后续 fallback。
                    ws_path = None
                    if seg.url:
                        ws_path = await self._save_media_to_workspace(
                            seg.url, suggested_name=f"voice_{event.message_id}.amr"
                        )
                    voice_text = await self._transcribe_voice_with_asr(event, ws_path)
                    if not voice_text:
                        voice_text = await self._fetch_voice_text_from_adapter(event)
                    suffix_parts = []
                    if seg.url:
                        suffix_parts.append(f"url={seg.url}")
                    if ws_path:
                        suffix_parts.append(f"workspace={ws_path}")
                    suffix = f" {' '.join(suffix_parts)}" if suffix_parts else ""
                    placeholder = (
                        f"[音频消息: {voice_text}{suffix}]"
                        if voice_text
                        else f"[音频消息: 未识别{suffix}]"
                    )
                    if "[语音]" in text:
                        text = text.replace("[语音]", placeholder, 1)
                    else:
                        text = f"{text} {placeholder}".strip()
                elif seg.type == MediaType.FILE:
                    url: str | None = seg.url
                    if not url and seg.file_id:
                        try:
                            url = await self.adapter.get_file_url(seg.file_id)
                        except NotImplementedError:
                            url = None
                        except Exception as e:
                            logger.warning(f"获取文件 URL 失败 file_id={seg.file_id}: {e}")
                    source = url or seg.file_id
                    ws_path = None
                    if source:
                        ws_path = await self._save_media_to_workspace(
                            source,
                            suggested_name=seg.name
                            or f"file_{event.message_id}",
                        )
                    suffix_parts = []
                    if source:
                        suffix_parts.append(f"url={source}")
                    if ws_path:
                        suffix_parts.append(f"workspace={ws_path}")
                    suffix = f" {' '.join(suffix_parts)}" if suffix_parts else ""
                    replacement = (
                        f"[文件{suffix}]"
                        if source
                        else "[文件: 获取URL失败]"
                    )
                    text = text.replace("[文件]", replacement, 1)
            except Exception as e:
                # 单段媒体抽取失败不应阻塞主链路
                logger.exception(f"媒体段处理失败 type={seg.type}: {e}")

        return text

    async def _transcribe_voice_with_asr(
        self, event: IncomingMessage, ws_path: str | None
    ) -> str:
        """优先使用已注入的 ASR 服务识别 workspace 中的语音文件。"""
        if self.asr is None:
            return ""
        if not ws_path or self.workspace_dir is None:
            logger.warning(
                f"ASR 已启用但语音文件不可用，回退适配器转写 msg_id={event.message_id}"
            )
            return ""
        audio_path = self.workspace_dir / ws_path
        try:
            text = await self.asr.transcribe(audio_path)
            return (text or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"ASR 识别失败 msg_id={event.message_id}，回退适配器转写: {e}"
            )
            return ""

    async def _fetch_voice_text_from_adapter(self, event: IncomingMessage) -> str:
        """适配器自带语音转文字 fallback。"""
        try:
            text = await self.adapter.fetch_voice_text(event.message_id)
            return (text or "").strip()
        except NotImplementedError:
            return ""
        except Exception as e:
            logger.warning(f"适配器语音转文字失败 msg_id={event.message_id}: {e}")
            return ""

    async def _save_media_to_workspace(
        self, url: str, suggested_name: str
    ) -> str | None:
        """下载/复制媒体到 data/workspace/incoming/，返回相对 workspace 的路径。

        失败仅 warn，返回 None；不阻塞主链路。
        NapCat 文件消息可能给 http(s) URL，也可能给本机 temp 路径；两种都保存到 workspace。
        """
        if not self.workspace_dir:
            return None
        try:
            import re
            import shutil
            from urllib.parse import unquote, urlparse

            import httpx

            incoming = self.workspace_dir / "incoming"
            incoming.mkdir(parents=True, exist_ok=True)
            parsed = urlparse(url)
            source_path: Path | None = None
            if re.match(r"^[A-Za-z]:[\\/]", url):
                source_path = Path(url)
            elif parsed.scheme == "file":
                file_path = unquote(parsed.path)
                if re.match(r"^/[A-Za-z]:/", file_path):
                    file_path = file_path[1:]
                source_path = Path(file_path)
            elif not parsed.scheme and not url.startswith(("http://", "https://")):
                source_path = Path(url)

            if source_path is not None and source_path.exists():
                if not Path(suggested_name).suffix and source_path.suffix:
                    suggested_name = f"{suggested_name}{source_path.suffix}"

            # 清理文件名：去掉路径分隔符与不安全字符
            safe_name = re.sub(r"[^\w.\-]", "_", suggested_name)[:80] or "file.bin"
            dest = incoming / safe_name
            # 同名加 _1 _2 后缀
            counter = 0
            while dest.exists():
                counter += 1
                stem = dest.stem
                suffix = dest.suffix
                # 去掉旧 counter 后缀
                stem = re.sub(r"_\d+$", "", stem)
                dest = incoming / f"{stem}_{counter}{suffix}"

            if source_path is not None:
                if not source_path.exists() or not source_path.is_file():
                    return None
                await asyncio.to_thread(shutil.copy2, source_path, dest)
            else:
                if not url.startswith(("http://", "https://")):
                    return None
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
            return f"incoming/{dest.name}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"下载媒体到 workspace 失败 url={url[:60]}: {e}")
            return None

    async def _send_rate_limit_reply(self, event: IncomingMessage) -> None:
        """非好友超限时发一条限速提示（不入历史）。文案根据当前 rate_limit 配置渲染。"""
        rl = self.behavior_cfg.rate_limit
        text = _RATE_LIMIT_REPLY_TEMPLATE.format(
            window_seconds=rl.window_seconds, max_messages=rl.max_messages
        )
        try:
            await self.adapter.send_text(event.source_target, text)
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

        result = await self.summary_agent.summarize_rolling(
            slice_records,
            self.rolling_summary.text(),
            self.important.text(),
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

        await self.history.add_system_note(
            f"[滚动摘要] 已归档并移出活跃历史 {cut_point} 条；"
            f"新增重要记忆 {len(new_important_items) if isinstance(new_important_items, list) else 0} 条"
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
        tasks = [self._batch_task, self._requeue_task, self._summary_task]
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
