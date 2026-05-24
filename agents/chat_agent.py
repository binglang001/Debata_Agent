"""主聊天 Agent —— 多轮工具循环，处理被动消息、主动唤醒、撤回、请求等场景。

构造时注入：
    - provider: 已构造的 IProvider
    - agent_cfg: 来自 config.agents.chat
    - tool_provider: 提供 tools schema 和 executor 的接口（Phase 1.7 实现）

调用：
    result = await chat_agent.run(messages, tool_executor=executor, tools=tools_schema)
"""

from __future__ import annotations

import logging
from typing import Any

from app_config.schema import AgentConfig
from providers.base import IProvider

from .base import AgentRunResult, ToolExecutor
from .runner import AgentRunner

logger = logging.getLogger(__name__)


class ChatAgent:
    """主聊天 Agent。"""

    def __init__(
        self,
        provider: IProvider,
        cfg: AgentConfig,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self._runner = AgentRunner(provider, cfg)

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tool_executor: ToolExecutor,
        task_contract: str | None = None,
    ) -> AgentRunResult:
        """执行一次完整对话循环（直到工具调用收尾）。

        Args:
            task_contract: 本轮目标的一句话描述。会按 refocus_interval 周期性
                重注入到 messages 末尾，防止多轮工具循环中的焦点漂移。
        """
        return await self._runner.run(
            messages,
            tools=tools,
            tool_executor=tool_executor,
            task_contract=task_contract,
        )
