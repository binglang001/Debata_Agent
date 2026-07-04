"""Agent runner 测试共享 helper。"""

from __future__ import annotations

import json

from agents.base import AgentRunResult
from providers.base import ToolCall


def basic_tool_schema(name: str = "needs_feedback") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def work_call(idx: int, *, name: str = "needs_feedback", args: str = "{}") -> ToolCall:
    return ToolCall(id=f"tc-{idx}", name=name, arguments=args)


def tool_payloads(result: AgentRunResult) -> list[dict]:
    return [
        json.loads(record["content"])
        for record in result.records
        if record.get("role") == "tool"
    ]
