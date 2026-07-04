from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from .base import ToolContext, tool
from .schemas import EatArgs, SleepArgs

logger = logging.getLogger(__name__)


@tool(
    name="eat",
    description=(
        "记录人格开始进食。只转发给人格管理 Agent，工具本身不直接修改数据库或等待耗时。"
    ),
    args_model=EatArgs,
    category="persona",
    search_tags=["persona", "physiology", "satiety", "eat"],
)
async def eat(args: EatArgs, ctx: ToolContext) -> dict:
    handler = _persona_handler(ctx, "on_eat_start")
    if handler is None:
        return _unavailable("eat")

    result = await handler(args.meal_type, args.duration_minutes, args.description)
    return _persona_result(result, default_status="done", default_brief="已记录开始进食。")


@tool(
    name="sleep",
    description=(
        "记录人格开始睡眠或休息。只转发给人格管理 Agent，工具本身不直接修改数据库或等待耗时。"
    ),
    args_model=SleepArgs,
    category="persona",
    search_tags=["persona", "physiology", "energy", "sleep"],
)
async def sleep(args: SleepArgs, ctx: ToolContext) -> dict:
    handler = _persona_handler(ctx, "on_sleep_start")
    if handler is None:
        return _unavailable("sleep")

    result = await _call_sleep_start(handler, args.duration_minutes, args.reason)
    wrapped = _persona_result(result, default_status="done", default_brief="已记录开始睡眠。")
    wrapped["reason"] = args.reason
    return wrapped


def _persona_handler(ctx: ToolContext, name: str) -> Callable[..., Any] | None:
    persona_agent = ctx.persona_agent
    if persona_agent is None:
        return None
    handler = getattr(persona_agent, name, None)
    if not callable(handler):
        return None
    return handler


def _unavailable(tool_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "unavailable",
        "brief": f"{tool_name} 工具不可用：未注入人格管理 Agent。",
        "error": "persona_agent unavailable",
    }


def _persona_result(
    result: Any,
    *,
    default_status: str,
    default_brief: str,
) -> dict[str, Any]:
    status = default_status
    if isinstance(result, dict) and isinstance(result.get("status"), str):
        status = result["status"]
    return {
        "ok": True,
        "status": status,
        "brief": default_brief,
        "result": result,
    }


async def _call_sleep_start(
    handler: Callable[..., Any],
    duration_minutes: int,
    reason: str,
) -> Any:
    if _accepts_reason(handler):
        return await _maybe_await(handler(duration_minutes, reason=reason))
    return await _maybe_await(handler(duration_minutes))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _accepts_reason(handler: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        logger.debug("无法读取 on_sleep_start 签名，按仅时长调用", exc_info=True)
        return False

    parameters = list(signature.parameters.values())
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters):
        return True
    if "reason" in signature.parameters:
        return True
    if any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in parameters):
        return True

    positional = [
        param
        for param in parameters
        if param.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2
