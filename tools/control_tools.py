"""控制类工具：no_action / schedule_wakeup。"""

from __future__ import annotations

import logging

from .base import ToolContext, tool
from .schemas import NoActionArgs, ScheduleWakeupArgs

logger = logging.getLogger(__name__)


@tool(
    name="no_action",
    description="当本轮不需要发送消息、不需要调用其它工具、不需要执行任何操作时调用。调用后本轮安静结束。",
    args_model=NoActionArgs,
    category="control",
    no_feedback=True,
)
async def no_action(args: NoActionArgs, ctx: ToolContext) -> dict:
    return {
        "ok": True,
        "status": "done",
        "no_action": True,
    }


@tool(
    name="schedule_wakeup",
    description=(
        "设置延迟任务。delay_seconds 是从现在开始等待的秒数。"
        "mode=send_message 表示到点直接向目标发送 message_text，适合普通提醒、叫人、定时发送消息；"
        "mode=wakeup 表示到点重新唤醒同一个 Agent，并把 reminder 作为唯一任务上下文交回给你，"
        "适合查询、整理、判断后再行动等复杂任务。wakeup 模式不会重新附带完整旧聊天历史，"
        "所以 reminder 必须自包含。target_type/target_id 未填时系统会尽量使用当前会话作为默认目标。"
    ),
    args_model=ScheduleWakeupArgs,
    category="control",
    no_feedback=True,
)
async def schedule_wakeup(args: ScheduleWakeupArgs, ctx: ToolContext) -> dict:
    if ctx.wakeup_cb is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "设置定时任务失败：未注册唤醒回调。",
            "error": "未注册唤醒回调",
        }

    target = None
    if args.target_type and args.target_id is not None:
        target = {"target_type": args.target_type, "target_id": args.target_id}
    elif isinstance(ctx.extras.get("default_reply_target"), dict):
        target = ctx.extras["default_reply_target"]

    if args.mode == "send_message" and target is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "设置定时发送失败：缺少发送目标。",
            "error": "mode=send_message 需要 target_type/target_id，或当前会话默认回复目标",
        }

    if args.mode == "send_message":
        reminder = _build_send_message_note(args.message_text or "", target, ctx)
    else:
        reminder = _build_wakeup_reminder(args.reminder or "", target, ctx)

    try:
        await ctx.wakeup_cb(
            args.delay_seconds,
            reminder,
            target,
            args.mode,
            args.message_text,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"schedule_wakeup 回调失败: {e}")
        return {
            "ok": False,
            "status": "failed",
            "brief": f"设置定时任务失败：{e}",
            "error": str(e),
        }

    return {
        "ok": True,
        "status": "done",
        "brief": f"已设置 {args.delay_seconds} 秒后定时任务。",
        "scheduled": True,
        "info": f"已设置 {args.delay_seconds} 秒后定时任务",
        "data": {
            "delay_seconds": args.delay_seconds,
            "mode": args.mode,
            "target": target,
            "message_text": args.message_text if args.mode == "send_message" else None,
        },
    }


def _build_send_message_note(
    message_text: str,
    target: dict | None,
    ctx: ToolContext,
) -> str:
    """构造定时发送消息的记录文本。不会交给模型执行。"""
    latest = str(ctx.extras.get("latest_user_message") or "").strip()
    lines = ["[定时发送消息]", f"消息内容：{message_text.strip()}"]
    if target:
        lines.append(
            f"发送目标：{target.get('target_type')}:{target.get('target_id')}"
        )
    if latest:
        lines.append(f"设置时用户原话：{latest}")
    return "\n".join(lines)


def _build_wakeup_reminder(
    reminder: str,
    target: dict | None,
    ctx: ToolContext,
) -> str:
    """把模型给的 reminder 补成唤醒轮可独立理解的任务说明。"""
    text = (reminder or "").strip()
    latest = str(ctx.extras.get("latest_user_message") or "").strip()
    if not target and not latest:
        return text

    lines = ["[定时任务]", f"任务说明：{text}"]
    if target:
        lines.append(
            f"提醒目标：{target.get('target_type')}:{target.get('target_id')}"
        )
    if latest:
        lines.append(f"设置时用户原话：{latest}")
    lines.append(
        "到点后只执行这条定时任务；不要把历史中已经完成、无关或仅作为背景的请求当作当前任务重复执行。"
        "如果任务需要发送消息，请按 reminder 的任务说明调用发送工具；如果只是内部继续任务且无需通知，可以 no_action。"
    )
    return "\n".join(lines)
