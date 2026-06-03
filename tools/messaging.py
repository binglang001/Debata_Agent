"""消息类工具：发送私聊/群消息、撤回、上传文件。

发送类工具保留 order / delay / typing_delay 的节奏。运行在 MessagePipeline
内时交给 Phase 0 异步发送队列；没有队列回调的独立调用退回同步直发兜底。
"""

from __future__ import annotations

import asyncio
import logging

from adapters.types import Target
from utils import get_time

from .base import ToolContext, tool
from .message_builder import build_message, contains_forbidden, typing_delay
from .schemas import (
    RecallMessageArgs,
    SendGroupArgs,
    SendPrivateArgs,
    UploadFileArgs,
)

logger = logging.getLogger(__name__)


def _mark_activity(ctx: ToolContext) -> None:
    if ctx.activity_cb is not None:
        ctx.activity_cb()


def _send_result(
    *,
    sent: list[dict],
    sent_messages: list[dict],
    errors: list[str],
    action_count: int,
) -> dict:
    result: dict = {
        "ok": bool(sent) or not action_count,
        "status": "sent",
        "brief": (
            f"已发送 {len(sent)} 条消息，QQ 可见。"
            if sent
            else "发送尝试完成，但没有消息发出。"
        ),
        "qq_visible": bool(sent),
        "count": len(sent),
        "sent": sent,
        "sent_messages": sent_messages,
    }
    if errors:
        result["errors"] = errors
    return result


def _sent_message_item(
    *,
    target_type: str,
    target_id: str,
    order: int,
    content: str,
    delay: float,
    msg_id: object,
) -> dict:
    item = {
        "conversation_id": f"{target_type}:{target_id}",
        "target_type": target_type,
        "target_id": target_id,
        "order": int(order),
        "content": content,
        "delay": float(delay),
        "msg_id": str(msg_id) if msg_id is not None else None,
        "time": get_time(),
        "qq_visible": True,
    }
    if target_type == "private":
        item["target_qq"] = target_id
    if target_type == "group":
        item["group_id"] = target_id
    return item


# ============================================================
# send_private_messages
# ============================================================


@tool(
    name="send_private_messages",
    description=(
        "向 QQ 用户发送私聊消息。可混合文字/图片，按 order 排序，delay 控制间隔。"
        "可在 content 开头加 [CQ:reply,id=消息ID] 引用回复。"
        "send_only=true 则正常发送后直接结束。"
    ),
    args_model=SendPrivateArgs,
    category="messaging",
)
async def send_private_messages(args: SendPrivateArgs, ctx: ToolContext) -> dict:
    """按 order 即时发送私聊消息，返回每条真实 msg_id。"""
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}

    actions: list[dict] = []
    errors: list[str] = []

    # 按 order 升序排序，保证逐条发送顺序与 LLM 意图一致
    sorted_targets = sorted(args.targets, key=lambda t: t.order)

    for t in sorted_targets:
        msg, label = await build_message(t.content, t.image, ctx.emoji_dir)
        if msg is None:
            errors.append(
                f"target_qq={t.target_qq}: 内容为空或表情包不存在"
            )
            continue
        if t.content and contains_forbidden(t.content):
            errors.append(
                f"target_qq={t.target_qq}: 内容含禁止标签"
            )
            continue

        delay = t.delay
        if delay is None:
            # 文本按长度估算延迟；纯图片走 0.5 秒
            delay = typing_delay(
                t.content or "",
                chars_per_second=ctx.typing_chars_per_second,
                max_delay=ctx.typing_max_delay_seconds,
            ) if t.content else 0.5

        actions.append(
            {
                "order": t.order,
                "target_scope": "private",
                "target_id": str(t.target_qq),
                "content": msg,
                "label": label or "",
                "delay": delay,
                "kind": "text",
            }
        )

    if ctx.send_actions_cb is not None:
        result = await ctx.send_actions_cb(actions, "send_private_messages")
        if errors:
            result["errors"] = [*result.get("errors", []), *errors]
        return result

    sent: list[dict[str, str | int | None]] = []
    sent_messages: list[dict] = []
    for i, action in enumerate(actions):
        target = Target(
            adapter=ctx.adapter.name,
            scope="private",
            target_id=str(action["target_id"]),
        )
        try:
            msg_id = await ctx.adapter.send_text(target, str(action["content"]))
            _mark_activity(ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"send_private_messages 发送失败 target={target.target_id}: {e}")
            errors.append(f"target_qq={target.target_id}: 发送失败：{e}")
            continue

        sent.append(
            {
                "order": int(action["order"]),
                "target_qq": target.target_id,
                "msg_id": str(msg_id) if msg_id is not None else None,
            }
        )
        sent_messages.append(
            _sent_message_item(
                target_type="private",
                target_id=target.target_id,
                order=int(action["order"]),
                content=str(action.get("label") or action.get("content") or ""),
                delay=float(action.get("delay") or 0.0),
                msg_id=msg_id,
            )
        )

        delay = float(action.get("delay") or 0.0)
        if delay > 0 and i < len(actions) - 1:
            await asyncio.sleep(delay)

    return _send_result(
        sent=sent,
        sent_messages=sent_messages,
        errors=errors,
        action_count=len(actions),
    )


# ============================================================
# send_group_message
# ============================================================


@tool(
    name="send_group_message",
    description=(
        "向 QQ 群发送消息。可混合文字/图片，按 order 排序，delay 控制间隔。"
        "可在 content 开头加 [CQ:reply,id=msg_id] 引用；@人用 [CQ:at,qq=QQ号]。"
        "send_only=true 则正常发送后直接结束。"
    ),
    args_model=SendGroupArgs,
    category="messaging",
)
async def send_group_message(args: SendGroupArgs, ctx: ToolContext) -> dict:
    """按 order 即时发送群消息，返回每条真实 msg_id。"""
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}

    actions: list[dict] = []
    errors: list[str] = []

    sorted_targets = sorted(args.targets, key=lambda t: t.order)

    for t in sorted_targets:
        msg, label = await build_message(t.content, t.image, ctx.emoji_dir)
        if msg is None:
            errors.append("内容为空或表情包不存在")
            continue
        if t.content and contains_forbidden(t.content):
            errors.append("内容含禁止标签")
            continue

        delay = t.delay
        if delay is None:
            delay = typing_delay(
                t.content or "",
                chars_per_second=ctx.typing_chars_per_second,
                max_delay=ctx.typing_max_delay_seconds,
            ) if t.content else 0.5

        actions.append(
            {
                "order": t.order,
                "target_scope": "group",
                "target_id": str(args.group_id),
                "content": msg,
                "label": label or "",
                "delay": delay,
                "kind": "text",
            }
        )

    if ctx.send_actions_cb is not None:
        result = await ctx.send_actions_cb(actions, "send_group_message")
        if errors:
            result["errors"] = [*result.get("errors", []), *errors]
        return result

    sent: list[dict[str, str | int | None]] = []
    sent_messages: list[dict] = []
    for i, action in enumerate(actions):
        target = Target(
            adapter=ctx.adapter.name,
            scope="group",
            target_id=str(action["target_id"]),
        )
        try:
            msg_id = await ctx.adapter.send_text(target, str(action["content"]))
            _mark_activity(ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"send_group_message 发送失败 group={target.target_id}: {e}")
            errors.append(f"group_id={target.target_id}: 发送失败：{e}")
            continue

        sent.append(
            {
                "order": int(action["order"]),
                "group_id": target.target_id,
                "msg_id": str(msg_id) if msg_id is not None else None,
            }
        )
        sent_messages.append(
            _sent_message_item(
                target_type="group",
                target_id=target.target_id,
                order=int(action["order"]),
                content=str(action.get("label") or action.get("content") or ""),
                delay=float(action.get("delay") or 0.0),
                msg_id=msg_id,
            )
        )

        delay = float(action.get("delay") or 0.0)
        if delay > 0 and i < len(actions) - 1:
            await asyncio.sleep(delay)

    return _send_result(
        sent=sent,
        sent_messages=sent_messages,
        errors=errors,
        action_count=len(actions),
    )


# ============================================================
# recall_message
# ============================================================


@tool(
    name="recall_message",
    description="撤回已发送的消息。仅可撤回 2 分钟内发出的消息。",
    args_model=RecallMessageArgs,
    category="messaging",
)
async def recall_message(args: RecallMessageArgs, ctx: ToolContext) -> dict:
    """直接调用 adapter.recall。"""
    if ctx.adapter is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "撤回失败：未连接适配器。",
            "error": "未连接适配器",
        }

    ok = await ctx.adapter.recall(str(args.message_id))
    if not ok:
        return {
            "ok": False,
            "status": "failed",
            "brief": "撤回失败：可能已超时或消息不存在。",
            "error": "撤回失败（可能已超时或消息不存在）",
            "data": {"message_id": str(args.message_id)},
        }
    _mark_activity(ctx)
    return {
        "ok": True,
        "status": "done",
        "brief": f"已撤回消息 {args.message_id}。",
        "data": {"message_id": str(args.message_id)},
    }


# ============================================================
# upload_file
# ============================================================


@tool(
    name="upload_file",
    description=(
        "向私聊或群聊发送本地文件。file_path 可以是相对 workspace 的路径"
        "（如 'report.pdf'）或绝对路径——但**必须**在 workspace 目录下，否则拒绝。"
        "file_name 是发到 QQ 后显示的文件名，可不填，系统会自动使用源文件名。"
    ),
    args_model=UploadFileArgs,
    category="messaging",
)
async def upload_file(args: UploadFileArgs, ctx: ToolContext) -> dict:
    """通过 adapter.upload_file 上传。安全检查：file_path 必须在 workspace 下。"""
    if ctx.adapter is None:
        return {
            "ok": False,
            "status": "failed",
            "brief": "上传文件失败：未连接适配器。",
            "error": "未连接适配器",
        }

    from .workspace import WorkspaceError, resolve_in_workspace

    try:
        file_path = resolve_in_workspace(args.file_path, ctx.workspace_dir)
    except WorkspaceError as e:
        return {
            "ok": False,
            "status": "failed",
            "brief": f"上传文件失败：{e}",
            "error": str(e),
        }

    if not file_path.exists() or not file_path.is_file():
        return {
            "ok": False,
            "status": "failed",
            "brief": "上传文件失败：文件不存在。",
            "error": "文件不存在",
            "data": {"path": str(args.file_path)},
        }

    from adapters.types import Target  # 延迟导入避免循环

    scope = "private" if args.target_type == "private" else "group"
    target = Target(
        adapter=ctx.adapter.name,
        scope=scope,
        target_id=str(args.target_id),
    )

    try:
        await ctx.adapter.upload_file(
            target,
            file_path,
            display_name=args.file_name or file_path.name,
        )
        _mark_activity(ctx)
    except NotImplementedError:
        return {
            "ok": False,
            "status": "failed",
            "brief": "上传文件失败：当前适配器不支持上传文件。",
            "error": "当前适配器不支持上传文件",
        }
    except Exception as e:
        logger.exception(f"upload_file 失败: {e}")
        return {
            "ok": False,
            "status": "failed",
            "brief": f"上传文件失败：{e}",
            "error": str(e),
        }

    display_name = args.file_name or file_path.name
    return {
        "ok": True,
        "status": "done",
        "brief": f"已上传文件 {display_name}。",
        "data": {
            "target_type": scope,
            "target_id": str(args.target_id),
            "path": str(file_path),
            "file_name": display_name,
        },
    }
