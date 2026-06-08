"""Agent 通用类型定义。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from providers.base import Usage

# 工具执行器签名
ToolExecutor = Callable[..., Awaitable[dict[str, Any]]]
UsageRecorder = Callable[[Usage, dict[str, Any]], Awaitable[None]]
StatusCallback = Callable[[dict[str, Any]], None]


FinishReason = Literal[
    "no_action",
    "finish_after_success",
    "tool_stop",
    "no_tool_after_retry",
    "tool_loop_finalized",
    "max_loops",
    "api_error",
    "no_response",
]


@dataclass(slots=True)
class AgentRunResult:
    """Agent 一次完整运行的结果。"""

    final_content: str = ""
    """最后一轮 assistant 的 content（仅在无工具调用结束或工具上限收尾时有意义）"""

    records: list[dict[str, Any]] = field(default_factory=list)
    """完整记录：assistant 消息（含 tool_calls）+ tool 响应 + 中间系统补正"""

    loop_count: int = 0

    finish_reason: FinishReason = "no_response"
    """循环结束的原因，用于上层决策"""

    reasoning_logs: list[str] = field(default_factory=list)
    """每轮 reasoning_content 的拼接（用于 UI 展示思考过程）"""

    prompt_tokens: int = 0
    """本轮所有模型调用累计的 prompt token，用于上下文估算校准。"""

    def has_records(self) -> bool:
        return bool(self.records)
