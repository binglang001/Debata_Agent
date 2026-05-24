"""消息类工具：发送私聊/群消息、撤回、上传文件。

发送类工具特殊性：
    - 不立即发送，而是把构造好的动作攒到 `ctx.collected` 列表。
    - 调用方（message_pipeline）在工具循环结束后读取 collected，
      逐条真实发送（保留旧版"中断检测 + typing delay"语义）。
    - 这样设计让 AgentRunner 与发送时序解耦，便于 P1.8 实现"发送中断"。

撤回 / 上传文件直接调用 adapter，无延迟逻辑。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import ToolContext, tool
from .message_builder import build_message, contains_forbidden, typing_delay
from .schemas import (
    RecallMessageArgs,
    SendGroupArgs,
    SendPrivateArgs,
    UploadFileArgs,
)

logger = logging.getLogger(__name__)


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
    """收集私聊发送动作到 ctx.collected。"""
    valid = 0
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
                max_delay=ctx.typing_max_delay,
            ) if t.content else 0.5

        ctx.collected.append(
            {
                "action": "private",
                "target": str(t.target_qq),
                "content": msg,
                "label": label or "",
                "delay": delay,
                "send_only": args.send_only,
            }
        )
        valid += 1

    result: dict = {"ok": True, "count": valid}
    if errors:
        result["errors"] = errors
    return result


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
    """收集群聊发送动作到 ctx.collected。"""
    valid = 0
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
                max_delay=ctx.typing_max_delay,
            ) if t.content else 0.5

        ctx.collected.append(
            {
                "action": "group",
                "target": str(args.group_id),
                "content": msg,
                "label": label or "",
                "delay": delay,
                "send_only": args.send_only,
            }
        )
        valid += 1

    result: dict = {"ok": True, "count": valid}
    if errors:
        result["errors"] = errors
    return result


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
        return {"ok": False, "error": "未连接适配器"}

    ok = await ctx.adapter.recall(str(args.message_id))
    if not ok:
        return {"ok": False, "error": "撤回失败（可能已超时或消息不存在）"}
    return {"ok": True}


# ============================================================
# upload_file
# ============================================================


@tool(
    name="upload_file",
    description="向私聊或群聊发送本地文件。文件路径必须在白名单目录下。",
    args_model=UploadFileArgs,
    category="messaging",
)
async def upload_file(args: UploadFileArgs, ctx: ToolContext) -> dict:
    """通过 adapter.upload_file 上传。

    安全检查：file_path 必须 realpath 后位于 ctx.upload_allowed_dir 之下。
    """
    if ctx.adapter is None:
        return {"ok": False, "error": "未连接适配器"}
    if ctx.upload_allowed_dir is None:
        return {"ok": False, "error": "未配置上传白名单目录，已禁用"}

    try:
        file_path = Path(args.file_path).resolve(strict=False)
    except OSError as e:
        return {"ok": False, "error": f"路径无效: {e}"}

    if not file_path.exists() or not file_path.is_file():
        return {"ok": False, "error": "文件不存在"}

    # 白名单根目录
    try:
        allowed_root = ctx.upload_allowed_dir.resolve(strict=False)
    except OSError as e:
        return {"ok": False, "error": f"白名单目录无效: {e}"}

    try:
        # Path.is_relative_to 是 3.9+ 的 API
        if not file_path.is_relative_to(allowed_root):
            return {"ok": False, "error": "文件路径不在允许范围内"}
    except ValueError:
        return {"ok": False, "error": "文件路径不在允许范围内"}

    # 构造 Target
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
            display_name=args.file_name,
        )
    except NotImplementedError:
        return {"ok": False, "error": "当前适配器不支持上传文件"}
    except Exception as e:
        logger.exception(f"upload_file 失败: {e}")
        return {"ok": False, "error": str(e)}

    return {"ok": True}
