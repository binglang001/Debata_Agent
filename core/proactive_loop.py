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
                await asyncio.sleep(self.behavior_cfg.proactive_think_interval_seconds)
            except asyncio.CancelledError:
                break

            if self._stopping:
                break

            try:
                await self._maybe_act()
            except Exception as e:
                logger.error(f"主动思考异常: {e}")

    async def _maybe_act(self) -> None:
        """单次判定 + 行动。

        先用小模型 ProactiveRouterAgent.should_act 判定，True 才跑主 ChatAgent。
        这是主动思考省 token 的关键路径：大部分时段都应该被路由器拦下。
        """
        # 有待处理消息时跳过（让被动响应优先）
        if not self.pipeline.batch.is_empty_unsafe():
            logger.info("主动思考：有待处理消息，跳过")
            return

        now = get_time()

        # 小模型路由判定
        if self.proactive_agent is not None:
            try:
                # 给路由器看相同的上下文（不带 tools schema，节省 token）
                router_messages = build_messages(
                    persona=self.pipeline.persona,
                    history=await self.pipeline.history.records(),
                    important_memory_text=self.pipeline.important.text(),
                    current_context=f"现在是{now}。后台主动思考触发。",
                    memory_mode=self.pipeline.features_cfg.long_term_memory.mode,
                )
                needs_action = await self.proactive_agent.should_act(router_messages)
            except Exception as e:
                logger.exception(f"主动路由判断失败: {e}，本次不主动发言")
                return
            if not needs_action:
                logger.info("主动路由：本次跳过")
                return

        # 通过路由 → 跑一轮主 Agent
        task_context = (
            f"现在是{now}。这是主动思考时间——你刚才判断要主动说点什么。"
            f"想发就调 send_*，不想就 no_action 收尾。"
        )
        await self.pipeline.run_one_turn(task_context)

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("ProactiveLoop 已停止")
