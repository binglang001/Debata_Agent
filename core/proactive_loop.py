"""主动思考定时循环 —— 让 AI 在空闲时主动发消息。

迁移自旧 handler.greeting_loop。流程：
    1. 每 GREETING_INTERVAL 秒触发一次
    2. 若当前有待处理消息（batch 非空），跳过本次
    3. 调 ProactiveRouterAgent 判断是否需要主动行动
    4. 若需要，调主 ChatAgent 跑一轮（通过 pipeline.run_one_turn）

设计：
    - ProactiveRouterAgent 用小模型判断，避免每次都跑 Pro 模型
    - 主动思考期间用 reply_lock 防止与被动消息处理打架
"""

from __future__ import annotations

import asyncio
import logging

from agents import ProactiveRouterAgent, build_messages
from app_config.schema import BehaviorConfig
from utils import get_time

from .message_pipeline import MessagePipeline

logger = logging.getLogger(__name__)


class ProactiveLoop:
    """主动思考循环。"""

    def __init__(
        self,
        *,
        pipeline: MessagePipeline,
        proactive_agent: ProactiveRouterAgent | None,
        behavior_cfg: BehaviorConfig,
    ) -> None:
        self.pipeline = pipeline
        self.proactive_agent = proactive_agent
        self.behavior_cfg = behavior_cfg
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        """启动循环（创建 background task）。"""
        if self.proactive_agent is None:
            logger.info("ProactiveLoop 未启用（proactive agent 未配置）")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"ProactiveLoop 已启动，间隔 {self.behavior_cfg.proactive_think_interval_seconds}s"
        )

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                idle = self.pipeline.idle_seconds()
                interval = self.behavior_cfg.proactive_think_interval_seconds
                if idle < interval:
                    await asyncio.sleep(max(0.5, min(interval - idle, 5.0)))
                    continue

                await self._maybe_act()
                await asyncio.sleep(min(interval, 5.0))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"主动思考异常: {e}")

            if self._stopping:
                break

    async def _maybe_act(self) -> None:
        """单次判定 + 行动。

        先用小模型 ProactiveRouterAgent.should_act 判定，True 才跑主 ChatAgent。
        这是主动思考省 token 的关键路径：大部分时段都应该被路由器拦下。
        """
        interval = self.behavior_cfg.proactive_think_interval_seconds
        idle = self.pipeline.idle_seconds()
        if idle < interval:
            logger.debug("主动思考：idle %.1fs < %.1fs，跳过", idle, interval)
            return

        if not self.pipeline.batch.is_empty_unsafe():
            logger.info("主动思考：有待处理消息，跳过")
            return
        if self.pipeline.reply_lock.locked():
            logger.info("主动思考：回复锁忙，跳过")
            return

        await self.pipeline.reply_lock.acquire()
        try:
            # 拿到锁后复检，避免“判断为空→等待锁期间新消息入队”的漂移。
            idle = self.pipeline.idle_seconds()
            if idle < interval:
                logger.debug("主动思考：锁内 idle %.1fs < %.1fs，跳过", idle, interval)
                return
            if not self.pipeline.batch.is_empty_unsafe():
                logger.info("主动思考：锁内发现待处理消息，跳过")
                return

            now = get_time()

            if self.proactive_agent is not None:
                try:
                    router_messages = build_messages(
                        persona=self.pipeline.persona,
                        history=await self.pipeline._select_proactive_router_history(),
                        important_memory_text=self.pipeline.important.text(),
                        rolling_summary_text=self.pipeline._rolling_summary_text(),
                        current_context=(
                            f"现在是{now}。后台主动思考触发。"
                            "如果最近用户明确要求你在“下次主动思考”时发消息、提醒用户或执行明确操作，"
                            "本轮就是那个时机。"
                        ),
                        memory_mode=self.pipeline.features_cfg.long_term_memory.mode,
                    )
                    needs_action = await self.proactive_agent.should_act(router_messages)
                except Exception as e:
                    logger.exception(f"主动路由判断失败: {e}，本次不主动发言")
                    self.pipeline.mark_activity()
                    return
                if not needs_action:
                    logger.info("主动路由：本次跳过")
                    self.pipeline.mark_activity()
                    return

            task_context = (
                f"现在是{now}。这是主动思考时间——你刚才判断要主动说点什么。"
                "如果最近用户明确要求你在下次主动思考时发消息、提醒用户或执行明确操作，优先完成那个请求。"
                f"想发就调 send_*，不想就 no_action 收尾。"
            )
            await self.pipeline.run_one_turn(task_context, lock_already_held=True)
        finally:
            self.pipeline.reply_lock.release()

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"ProactiveLoop 停止异常: {e}")
        logger.info("ProactiveLoop 已停止")
