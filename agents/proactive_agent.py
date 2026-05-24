"""主动思考路由 Agent —— 用小模型判断是否需要主动发言。

行为（沿用 V1 语义）：
    每隔 greeting_interval 触发一次。给模型当前历史 + "想聊天就回 TAKE_ACTIONS"。
    如果模型说 TAKE_ACTIONS，再用主 ChatAgent 跑一次完整循环。

为了节省 token，proactive 用单次（非工具循环）调用，不带 tools。
"""

from __future__ import annotations

import logging
from typing import Any

from app_config.schema import AgentConfig
from providers.base import (
    IProvider,
    ProviderError,
    ReasoningConfig,
)

logger = logging.getLogger(__name__)


ROUTER_INSTRUCTION = (
    "你现在处于后台主动思考模式。快速判断：是否需要发消息给任何人？"
    "只回复 TAKE_ACTIONS（需要）或 NO_ACTIONS（不需要）。不要解释。"
)


class ProactiveRouterAgent:
    """主动思考的判定器（小模型路由）。"""

    def __init__(
        self,
        provider: IProvider,
        cfg: AgentConfig,
    ) -> None:
        self.provider = provider
        self.cfg = cfg

    async def should_act(self, messages: list[dict[str, Any]]) -> bool:
        """判断当前是否值得主动发言。"""
        check_msgs = list(messages)
        check_msgs.append({"role": "system", "content": ROUTER_INSTRUCTION})

        try:
            result = await self.provider.chat_completion(
                check_msgs,
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=self.cfg.max_tokens,
                reasoning=self._to_provider_reasoning(),
                stream=False,
                timeout=self.cfg.first_token_timeout * 2,
                first_token_timeout=self.cfg.first_token_timeout,
            )
        except ProviderError as e:
            logger.warning(f"主动路由判断失败: {e}，默认不操作")
            return False
        except Exception as e:
            logger.exception(f"主动路由异常: {e}")
            return False

        text = (result.content or "").strip().upper()
        decision = "TAKE_ACTIONS" in text
        logger.info(f"主动路由: text={text[:40]!r}, decision={decision}")
        return decision

    def _to_provider_reasoning(self) -> ReasoningConfig | None:
        if self.cfg.reasoning is None:
            return None
        return ReasoningConfig(
            enabled=self.cfg.reasoning.enabled,
            budget=self.cfg.reasoning.budget,
            max_tokens=self.cfg.reasoning.max_tokens,
        )
