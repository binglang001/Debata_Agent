"""主动思考路由 Agent —— 用小模型判断是否需要主动发言。

行为（沿用 V1 语义）：
    每隔 behavior.proactive_think_interval_seconds 触发一次。给模型当前历史；
    如果模型返回 TAKE_ACTIONS: 理由，再用主 ChatAgent 跑一次完整循环。

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

from .base import StatusCallback, UsageRecorder

logger = logging.getLogger(__name__)


ROUTER_INSTRUCTION = (
    "你现在处于后台主动思考模式。快速判断：是否需要发消息给任何人？"
    "如果历史里有人明确要求你在“下次主动思考”时发消息、提醒用户或执行明确操作，"
    "本轮就是执行时机，应回复 TAKE_ACTIONS 并给一句理由。"
    "也允许在距上次互动较久、有自然由头、当前时间点适合时主动找人聊天或问候。"
    "必须克制；给不出具体理由就回复 NO_ACTIONS。"
    "只回复 TAKE_ACTIONS: <一句话理由> 或 NO_ACTIONS。不要额外解释。"
)


class ProactiveRouterAgent:
    """主动思考的判定器（小模型路由）。"""

    def __init__(
        self,
        provider: IProvider,
        cfg: AgentConfig,
        *,
        usage_recorder: UsageRecorder | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.usage_recorder = usage_recorder
        self.status_callback = status_callback

    async def should_act(self, messages: list[dict[str, Any]]) -> tuple[bool, str]:
        """判断当前是否值得主动发言。"""
        check_msgs = list(messages)
        check_msgs.append({"role": "system", "content": ROUTER_INSTRUCTION})

        try:
            self._emit_status("thinking", "主动思考路由判断中")
            result = await self.provider.chat_completion(
                check_msgs,
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=self.cfg.max_tokens,
                reasoning=self._to_provider_reasoning(),
                stream=True,
                timeout=self.cfg.first_token_timeout_seconds * 2,
                first_token_timeout=self.cfg.first_token_timeout_seconds,
            )
            await self._record_usage(result.usage, operation="proactive_route")
        except ProviderError as e:
            logger.warning(f"主动路由判断失败: {e}，默认不操作")
            self._emit_status("error", "主动思考路由失败")
            return False, ""
        except Exception as e:
            logger.exception(f"主动路由异常: {e}")
            self._emit_status("error", "主动思考路由异常")
            return False, ""

        text = (result.content or "").strip()
        decision, reason = _parse_action_decision(text)
        self._emit_status(
            "idle",
            "主动思考决定行动" if decision else "主动思考无需行动",
        )
        logger.info(
            "主动路由: text=%r, decision=%s, reason=%r",
            text[:80],
            decision,
            reason[:80],
        )
        return decision, reason

    def _to_provider_reasoning(self) -> ReasoningConfig | None:
        if self.cfg.reasoning is None:
            return None
        return ReasoningConfig(
            enabled=self.cfg.reasoning.enabled,
            budget=self.cfg.reasoning.budget,
            max_tokens=self.cfg.reasoning.max_tokens,
        )

    async def _record_usage(self, usage, **metadata: Any) -> None:
        if self.usage_recorder is None:
            return
        try:
            await self.usage_recorder(
                usage,
                {
                    "provider": self.provider.name,
                    "model": self.cfg.model,
                    "agent": "主动思考",
                    **metadata,
                },
            )
        except Exception:
            logger.debug("记录主动思考用量失败", exc_info=True)

    def _emit_status(self, state: str, text: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(
                {
                    "state": state,
                    "text": text,
                    "model": self.cfg.model,
                    "agent": "主动思考",
                }
            )
        except Exception:
            logger.debug("更新主动思考状态失败", exc_info=True)


def _parse_action_decision(text: str) -> tuple[bool, str]:
    """解析主动路由输出。

    主动路由只能接受干净的 TAKE_ACTIONS / NO_ACTIONS。模型吐 DSML / 工具标记时属于
    无工具调用场景下的畸形输出，不能当作需要行动，否则会周期性空跑主模型。
    """
    raw = (text or "").strip()
    normalized = raw.upper()
    if normalized == "NO_ACTIONS":
        return False, ""
    if normalized == "TAKE_ACTIONS":
        return True, ""
    prefix = "TAKE_ACTIONS:"
    if normalized.startswith(prefix):
        reason = raw[len(prefix) :].strip()
        return True, reason
    return False, ""


def _is_action_decision(text: str) -> bool:
    """兼容旧测试/调用方的布尔解析。"""
    return _parse_action_decision(text)[0]
