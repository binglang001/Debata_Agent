"""控制类工具：no_action / schedule_wakeup。

这两个工具不产生外部副作用（除 wakeup 注册回调），都是 no_feedback。
"""

from __future__ import annotations

import logging

from .base import ToolContext, tool
from .schemas import NoActionArgs, ScheduleWakeupArgs

logger = logging.getLogger(__name__)


@tool(
    name="no_action",
    description="当不需要发送任何消息、不需要执行任何操作时调用。调用此工具即表示本轮不发言。",
    args_model=NoActionArgs,
    category="control",
    no_feedback=True,
)
async def no_action(args: NoActionArgs, ctx: ToolContext) -> dict:
    return {"ok": True, "no_action": True}


@tool(
    name="schedule_wakeup",
    description="设置定时提醒。到时会通知你，由你决定是否操作。",
    args_model=ScheduleWakeupArgs,
    category="control",
    no_feedback=True,
)
async def schedule_wakeup(args: ScheduleWakeupArgs, ctx: ToolContext) -> dict:
    if ctx.wakeup_cb is None:
        return {"ok": False, "error": "未注册唤醒回调"}

    try:
        await ctx.wakeup_cb(args.delay_seconds, args.reminder)
    except Exception as e:
        logger.exception(f"schedule_wakeup 回调失败: {e}")
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "scheduled": True,
        "info": f"已设置 {args.delay_seconds} 秒后提醒",
    }
