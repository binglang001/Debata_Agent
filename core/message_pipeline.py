"""消息处理管道 —— 整个项目的心脏。

负责：
    1. 接收 IncomingMessage（来自 EventBus）
    2. 速率限制（非好友）
    3. 重建可读文本（CQ 码解析 + 附加媒体 URL）
    4. 关键词强制保存（命中即触发 ImportantMemoryManager.force_save）
    5. 合并窗口暂存
    6. 批处理触发：合并窗口到时，组装 messages → 调 ChatAgent → 收集 collected
    7. 真实发送：逐条发，每条前检查批处理队列是否有新消息打断
    8. 历史持久化 + 总结触发

这是 P1.8 的核心模块，已装实完整链路：消息合并 / 中断检测 / 媒体抽取 /
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
    upload_allowed_dir Path | None      文件上传白名单
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
from pathlib import Path
from typing import Awaitable, Callable

from adapters.base import IAdapter
from adapters.types import IncomingMessage, MediaType, Target
from agents import ChatAgent, Persona, SummaryAgent, build_messages
from app_config.schema import BehaviorConfig, FeaturesConfig, WhitelistConfig
from memory import HistoryManager, ImportantMemoryManager
from tools import (
    IVisionService,
    IWeatherService,
    IWebSearchService,
    ToolContext,
    ToolRegistry,
    try_save_from_user,
)
from utils import get_time, parse_raw_cq

from .state import MessageBatch, PendingMessageItem, PendingRequestStore, RateLimiter
from .wakeup import WakeupScheduler

logger = logging.getLogger(__name__)


# 速率超限时的提示模板（占位符运行时替换）
_RATE_LIMIT_REPLY_TEMPLATE = "已超出速率限制（{window_seconds} 秒内最多 {max_messages} 条），请添加机器人为好友后继续使用"


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
        tool_registry: ToolRegistry,
        wakeup_scheduler: WakeupScheduler,
        pending_requests: PendingRequestStore,
        behavior_cfg: BehaviorConfig,
        features_cfg: FeaturesConfig,
        whitelist: WhitelistConfig | None = None,
        emoji_dir: Path | None = None,
        upload_allowed_dir: Path | None = None,
        rate_limiter: RateLimiter | None = None,
        summary_agent: SummaryAgent | None = None,
        vision: IVisionService | None = None,
        web_search: IWebSearchService | None = None,
        weather: IWeatherService | None = None,
    ) -> None:
        self.adapter = adapter
        self.chat_agent = chat_agent
        self.persona = persona
        self.history = history
        self.important = important
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
        self.upload_allowed_dir = upload_allowed_dir
        self.rate_limiter = rate_limiter
        self.summary_agent = summary_agent
        self.vision = vision
        self.web_search = web_search
        self.weather = weather

        self.batch = MessageBatch()
        self.reply_lock = asyncio.Lock()
        self._batch_task: asyncio.Task | None = None
        # 退出 _batch_loop 后的兜底 task；保留引用避免 GC
        self._requeue_task: asyncio.Task | None = None

    # ============================================================
    # 入口：EventBus 调用此方法
    # ============================================================

    async def enqueue(self, event: IncomingMessage) -> None:
        """接收一条入站消息，做前置检查后加入批处理队列。"""
        if not event.text and not event.media:
            # 空消息（无文本无媒体）忽略
            return

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
        if event.text:
            await try_save_from_user(
                event.text,
                self.important,
                enabled=self.features_cfg.long_term_memory.keyword_trigger_save,
            )

        # 重建可读文本（CQ 码 + 媒体）
        text = await self._build_readable_text(event)

        item = PendingMessageItem(
            message_id=event.message_id,
            user_id=event.user_id,
            nickname=event.nickname,
            location=f"群聊 {event.group_id}" if event.is_group() else "私聊",
            text=text,
            raw_event=event,
        )

        await self.batch.append(item)
        # 启动批处理任务（如未运行）
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_loop(event.source_target))

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

            try:
                interrupted = await self._process_batch(items)
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

    async def _process_batch(self, items: list[PendingMessageItem]) -> bool:
        """处理一批消息。返回 True 表示发送被中断（需要重循环）。

        步骤：
            1. 合并文本写入 history（user 消息）
            2. 构造 ToolContext + 调 ChatAgent
            3. 写 records 到 history
            4. 执行 collected 真实发送（带中断检测）
            5. 触发可选总结
        """
        # 合并为一条 user 消息写入历史
        now = get_time()
        lines = [
            f"【{now} {item.location} {item.nickname}({item.user_id}) "
            f"msg_id={item.message_id}】{item.text}"
            for item in items
        ]
        await self.history.add_user_message("\n".join(lines))
        logger.info(f"合并处理 {len(items)} 条消息")

        # 构造给 LLM 的 messages（emoji_hint / pending_requests 已在 _build_task_context 内拼装）
        task_context = self._build_task_context(now)

        messages = build_messages(
            persona=self.persona,
            history=await self.history.records(),
            important_memory_text=self.important.text(),
            current_context=task_context,
            memory_mode=self.features_cfg.long_term_memory.mode,
        )

        # 构造 ToolContext
        ctx = self._build_tool_context()
        executor = self.tool_registry.get_executor(ctx)
        tools_schema = self.tool_registry.get_schemas()

        # 跑 Agent（reply_lock 防并发）
        async with self.reply_lock:
            result = await self.chat_agent.run(
                messages,
                tools=tools_schema,
                tool_executor=executor,
                task_contract=None,  # 暂不强制 task contract，由 prompt 末尾的提示词承担焦点引导
            )

        # 写 records
        if result.records:
            await self.history.add_records(result.records)

        # 处理 collected
        interrupted = await self._execute_collected(ctx.collected, items)

        # 触发可选总结
        if self.summary_agent is not None:
            try:
                await self._maybe_summarize()
            except Exception as e:
                logger.warning(f"总结触发失败（忽略）: {e}")

        return interrupted

    # ============================================================
    # 执行收集到的发送动作（含中断检测）
    # ============================================================

    async def _execute_collected(
        self,
        collected: list[dict],
        original_items: list[PendingMessageItem],
    ) -> bool:
        """逐条发送，每条前检查是否有新消息打断。

        返回：True = 中途被打断；False = 全部成功或没有动作。

        旧版语义保留点（重要）：
            - 中断时未发送的动作被 ABANDON（不重试），但要把"中断信息"作为 system_note
              写入历史，让下次 Agent 知道有些话没说出去
            - 中断时已发送的部分要保留 msg_id 记录

        ⚠ 关于 TOCTOU（time-of-check vs time-of-use）：
            本函数在每条发送前 acquire batch.lock，检查 pending，释放锁，再调
            adapter.send_text。从锁释放到 send_text 返回这段时间内，新消息仍
            可能进入 batch —— 这是已知的"乐观中断"语义。设计上接受这种短窗口，
            因为：(1) send_text 通常很快（< 200ms）;(2) 即便错过这一条，下一条
            发送前还会再 check 一次；(3) 紧凑加锁会引入复杂度且对体验改善有限。
            中断 system_note 中已经写明"新消息可能在发送过程中又到了"，让 Agent
            自己消化语义。
        """
        if not collected:
            return False

        sent = 0
        for i, action in enumerate(collected):
            # 中断检测：发送前检查队列
            async with self.batch.lock:
                pending = await self.batch.peek_locked()
                if pending:
                    remaining = collected[i:]
                    note = self._build_interrupt_note(sent, remaining, pending)
                    await self.history.add_system_note(note)
                    logger.info(
                        f"发送中断：已发送 {sent} 条，剩余 {len(remaining)} 条丢弃，"
                        f"新消息 {len(pending)} 条"
                    )
                    return True

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

    async def _do_send(self, action: dict) -> str | None:
        """把单个 collected action 真实发送出去。

        失败时写一条 system_note 到历史，让 Agent 下次能看到"哪条没发出去"，
        避免 AI 误以为消息已成功传达。
        """
        scope = action.get("action", "")
        target_id = action.get("target", "")
        content = action.get("content", "")
        label = action.get("label", "")
        if not content:
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
            return await self.adapter.send_text(target, content)
        except Exception as e:
            logger.error(f"发送失败 {target}: {e}")
            await self.history.add_system_note(
                f"⚠ 发送失败 → {label or target_id}（{type(e).__name__}: {e}）"
            )
            return None

    # ============================================================
    # 唤醒入口（WakeupScheduler 触发时调用）
    # ============================================================

    async def run_wakeup_turn(self, reminder: str) -> None:
        """schedule_wakeup 到时调用，跑一轮 Agent。

        不走批处理（独立任务）。整体路径与 _process_batch 相似但 context 不同。
        """
        now = get_time()
        await self.history.add_system_note(f"{now} 定时唤醒：{reminder}")
        logger.info(f"定时唤醒执行: {reminder!r}")

        task_context = f"现在是{now}。定时唤醒: {reminder}。根据唤醒内容决定是否发消息或执行操作。"

        messages = build_messages(
            persona=self.persona,
            history=await self.history.records(),
            important_memory_text=self.important.text(),
            current_context=task_context,
            memory_mode=self.features_cfg.long_term_memory.mode,
        )

        ctx = self._build_tool_context()
        executor = self.tool_registry.get_executor(ctx)
        tools_schema = self.tool_registry.get_schemas()

        async with self.reply_lock:
            result = await self.chat_agent.run(
                messages,
                tools=tools_schema,
                tool_executor=executor,
            )

        if result.records:
            await self.history.add_records(result.records)
        if ctx.collected:
            await self._execute_collected(ctx.collected, [])

    # ============================================================
    # 通用 Agent 调用：供 recall_handler / request_handler / proactive_loop 复用
    # ============================================================

    async def run_one_turn(
        self,
        task_context: str,
        *,
        as_system_note: str | None = None,
    ) -> None:
        """通用单轮 Agent 入口：注入 task_context，跑一轮，处理 collected。

        Args:
            task_context: 本轮 ephemeral context（时间、事件描述、提醒等）
            as_system_note: 若给，会在调 Agent 前写入 history 作为事件记录
                （如撤回通知、请求通知）
        """
        if as_system_note:
            await self.history.add_system_note(as_system_note)

        messages = build_messages(
            persona=self.persona,
            history=await self.history.records(),
            important_memory_text=self.important.text(),
            current_context=task_context,
            memory_mode=self.features_cfg.long_term_memory.mode,
        )

        ctx = self._build_tool_context()
        executor = self.tool_registry.get_executor(ctx)
        tools_schema = self.tool_registry.get_schemas()

        async with self.reply_lock:
            result = await self.chat_agent.run(
                messages,
                tools=tools_schema,
                tool_executor=executor,
            )

        if result.records:
            await self.history.add_records(result.records)
        if ctx.collected:
            await self._execute_collected(ctx.collected, [])

    # ============================================================
    # 构造 ToolContext —— 集中点
    # ============================================================

    def _build_tool_context(self) -> ToolContext:
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

        return ToolContext(
            adapter=self.adapter,
            important=self.important,
            history=self.history,
            wakeup_cb=self.wakeup_scheduler.schedule,
            vision=self.vision,
            web_search=self.web_search,
            weather=self.weather,
            upload_allowed_dir=self.upload_allowed_dir,
            emoji_dir=self.emoji_dir,
            typing_chars_per_second=self.behavior_cfg.typing.chars_per_second,
            typing_max_delay_seconds=self.behavior_cfg.typing.max_delay_seconds,
            default_history_fetch_count=self.behavior_cfg.default_history_fetch_count,
            summary_provider=summary_provider,
            summary_model=summary_model,
            collected=[],
        )

    # ============================================================
    # 私有辅助 —— 待 GPT 填实的部分
    # ============================================================

    def _build_task_context(self, now: str) -> str:
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

        parts.append(
            "引用消息用 [CQ:reply,id=消息ID]；@某人用 [CQ:at,qq=QQ号]。"
            "提醒：别人引用（reply）你的消息不一定是在跟你说话——"
            "可能是在对别人说话时引用了你的内容作为参考。除非对方明确是在对你说话，否则不需要回复。"
        )
        parts.append(
            "注意：用户可能将一句话拆成多条连续消息发送，"
            "若同一用户连续多条消息语义不完整，请合并理解其完整意图。"
        )

        pending_info = self.pending_requests.to_prompt_text()
        if pending_info:
            parts.append(pending_info)

        return "\n".join(parts)

    async def _build_readable_text(self, event: IncomingMessage) -> str:
        """把 IncomingMessage 重建为人类可读文本（CQ 解析 + 媒体 URL/转录附加）。

        events.parse_napcat_event 已经把 raw_message 走过 parse_raw_cq，
        结果存在 event.text 里。这里只在 event.text 为空（异常路径）时回退重算。
        然后扫描 event.media，把占位升级为含 URL / 转录的版本：
            - 图片：[图片] → [图片 url=...]
            - 语音：[语音] → [音频消息: 转录文本]（通过 adapter.fetch_voice_text）
            - 文件：[文件] → [文件 url=...]（通过 adapter.get_file_url）
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
                    text = text.replace("[图片]", f"[图片 url={seg.url}]", 1)
                elif seg.type in (MediaType.VOICE, MediaType.RECORD):
                    voice_text = ""
                    try:
                        voice_text = await self.adapter.fetch_voice_text(event.message_id)
                    except NotImplementedError:
                        voice_text = ""
                    except Exception as e:
                        logger.warning(f"语音转文字失败 msg_id={event.message_id}: {e}")
                    placeholder = (
                        f"[音频消息: {voice_text}]" if voice_text else "[音频消息: 未识别]"
                    )
                    text = text.replace("[语音]", placeholder, 1)
                elif seg.type == MediaType.FILE and seg.file_id:
                    url: str | None = None
                    try:
                        url = await self.adapter.get_file_url(seg.file_id)
                    except NotImplementedError:
                        url = None
                    except Exception as e:
                        logger.warning(f"获取文件 URL 失败 file_id={seg.file_id}: {e}")
                    replacement = (
                        f"[文件 url={url}]" if url else "[文件: 获取URL失败]"
                    )
                    text = text.replace("[文件]", replacement, 1)
            except Exception as e:
                # 单段媒体抽取失败不应阻塞主链路
                logger.exception(f"媒体段处理失败 type={seg.type}: {e}")

        return text

    def _build_interrupt_note(
        self,
        sent: int,
        remaining: list[dict],
        pending: list[PendingMessageItem],
    ) -> str:
        """构造中断说明文本（写入 history.add_system_note）。"""
        sent_summary = f"已成功发送 {sent} 条。" if sent > 0 else ""
        remaining_desc = "\n".join(
            f"  [未发送] {a.get('label', a.get('content', ''))[:60]}"
            for a in remaining
        )
        interrupt_desc = "\n".join(
            f"  → {p.nickname}({p.user_id}): {p.text[:60]}" for p in pending
        )
        return (
            f"⚠ 发送中断！{sent_summary}\n"
            f"以下消息【未发出】，被新消息打断：\n{remaining_desc}\n"
            f"打断的新消息：\n{interrupt_desc}\n"
            f"注意：在发送过程中可能又有新消息到达——下一轮你能看到的会更全。\n"
            f"请决定：重新发送未发出的内容，还是回应新消息。\n"
            f"如果对话还没结束，就不要什么都不说"
        )

    async def _send_rate_limit_reply(self, event: IncomingMessage) -> None:
        """非好友超限时发一条限速提示（不入历史）。文案根据当前 rate_limit 配置渲染。"""
        rl = self.behavior_cfg.rate_limit
        text = _RATE_LIMIT_REPLY_TEMPLATE.format(
            window_seconds=rl.window_seconds, max_messages=rl.max_messages
        )
        try:
            await self.adapter.send_text(event.source_target, text)
        except Exception:
            pass

    # ============================================================
    # 历史总结触发（占位实现，由 SummaryAgent 接管）
    # ============================================================

    async def _maybe_summarize(self) -> None:
        """达到阈值时让 SummaryAgent 总结历史。

        流程：
            1. history 长度 ≥ trigger_at 才触发
            2. 取前 range_end 条作为切片（让 SummaryAgent 在 [range_start, range_end] 内择点）
            3. 调 summary_agent.summarize(slice, important.text()) →
               {"cut_point": int, "new_important": [{"timestamp", "content"}, ...]}
            4. 用返回的 new_important 整体替换重要记忆（SummaryAgent prompt 已要求返回"合并后的完整版"）
            5. truncate_head(cut_point) 删除被总结的对话
            6. 写一条 system_note 标记总结事件
        """
        if self.summary_agent is None:
            return

        length = await self.history.length()
        if length < self.behavior_cfg.summarize.trigger_at_messages:
            return

        logger.info(
            f"对话历史达 {length} 条 ≥ {self.behavior_cfg.summarize.trigger_at_messages}，触发总结"
        )

        slice_records = await self.history.get_slice(
            0, self.behavior_cfg.summarize.range_end_messages
        )

        result = await self.summary_agent.summarize(slice_records, self.important.text())
        if not result:
            logger.warning("总结 Agent 返回 None，跳过本次截断")
            return

        cut_point = result.get("cut_point")
        new_important_items = result.get("new_important", [])

        # 先替换重要记忆（即便 truncate 失败，新的记忆已写入）
        if isinstance(new_important_items, list) and new_important_items:
            await self.important.replace_all(new_important_items)

        if isinstance(cut_point, int) and cut_point > 0:
            await self.history.truncate_head(cut_point)

        await self.history.add_system_note(
            f"[记忆总结] 已截断历史 {cut_point} 条，"
            f"重要记忆更新为 {len(new_important_items)} 条"
        )

    # ============================================================
    # 关闭
    # ============================================================

    async def shutdown(self) -> None:
        """优雅停止：取消批处理任务。"""
        for task in (self._batch_task, self._requeue_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("MessagePipeline 已停止")
