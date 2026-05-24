"""消息处理 + 主动问候 —— Diana-agent 核心逻辑"""

import asyncio
import base64
import os
import re
import time

from nonebot import get_bot, get_driver, on_message, on_notice, on_request
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    FriendRecallNoticeEvent,
    FriendRequestEvent,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    GroupRequestEvent,
    NoticeEvent,
    PrivateMessageEvent,
)
from nonebot.log import logger

from .config import (
    CHAT_HISTORY_COUNT,
    GREETING_INTERVAL,
    MERGE_WINDOW,
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW,
    RECALL_MERGE_WINDOW,
    TYping_CHARS_PER_SECOND,
    TYping_MAX_DELAY,
)
from .llm_client import (
    FLASH_MODEL,
    check_important_memory_duplicate,
    check_proactive_action,
    flash_client,
    pro_tools,
    run_agent_loop,
    summarize_history,
)
from .memory import (
    SUMMARIZE_AT,
    SUMMARIZE_RANGE,
    add_records,
    add_system_note,
    add_user_message,
    build_messages,
    delete_important_memory,
    get_history_length,
    get_history_slice,
    load_important_memories_text,
    save_important_memory,
    truncate_history,
)
from .web_search import web_search
from .utils import get_time
from .qweather import QWeather
from .vision import describe_image

driver = get_driver()

batch_pending: list[tuple] = []
batch_lock = asyncio.Lock()
reply_lock = asyncio.Lock()

_EMOJI_DIR = os.path.join(os.path.dirname(__file__), "..", "emoji")
_ALLOWED_UPLOAD_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "uploads"))
FORBIDDEN_TAGS = ["[私聊给", "[群聊", "[TO:", "我给 QQ", "我在群"]

# ===== 验证请求暂存（管理员回复时能获取 flag）=====
_pending_requests: dict[str, dict] = {}
_REQUEST_TIMEOUT = 1800  # 30 分钟超时

# ===== 速率限制 =====
_rate_limit: dict[str, list[float]] = {}
_rate_limit_lock = asyncio.Lock()

RATE_LIMIT_MSG = "已超出速率限制（每分钟最多 5 条），请添加机器人为好友后继续使用"


async def _check_rate_limit(bot: Bot, user_id: str) -> bool:
    """非好友用户速率限制。返回 True 表示被限制。"""
    try:
        friends = await bot.get_friend_list()
        friend_ids = {str(f["user_id"]) for f in friends}
        if user_id in friend_ids:
            return False
    except Exception:
        pass

    now = time.time()
    async with _rate_limit_lock:
        timestamps = _rate_limit.get(user_id, [])
        timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
        if len(timestamps) >= RATE_LIMIT_MAX:
            return True
        timestamps.append(now)
        _rate_limit[user_id] = timestamps
        return False


def _get_pending_requests_info() -> str:
    """获取待处理验证请求描述文本，同时清理过期项。"""
    now = time.time()
    expired = [f for f, i in _pending_requests.items() if i["expires_at"] < now]
    for f in expired:
        del _pending_requests[f]
        logger.info(f"验证请求已过期自动清理: flag={f}")
    if not _pending_requests:
        return ""
    lines = ["当前待处理的验证请求："]
    for flag, info in _pending_requests.items():
        if info["type"] == "friend":
            lines.append(
                f"- [好友请求] QQ={info['user_id']} "
                f"昵称={info['nickname']} 附加消息={info.get('comment', '')} "
                f"【操作标识 flag={flag}】"
            )
        else:
            lines.append(
                f"- [群请求/{info.get('sub_type', '')}] QQ={info['user_id']} "
                f"昵称={info['nickname']} 群号={info.get('group_id', '')} 附加消息={info.get('comment', '')} "
                f"【操作标识 flag={flag}】"
            )
    lines.append("管理员可通过回复消息指示同意或拒绝（使用 set_friend_add_request / set_group_add_request 工具）。")
    return "\n".join(lines)


# ===== 撤回事件合并 =====
_recall_pending: list[str] = []
_recall_lock = asyncio.Lock()
_recall_task: asyncio.Task | None = None


def _contains_forbidden(text: str) -> bool:
    return any(tag in text for tag in FORBIDDEN_TAGS)


# ========== 动态 emoji 列表 ==========
def _list_emoji_files() -> list[str]:
    try:
        return sorted(f for f in os.listdir(_EMOJI_DIR)
                      if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif")))
    except Exception:
        return []


def _build_emoji_hint() -> str:
    files = _list_emoji_files()
    return "可用表情包：" + "、".join(files) if files else ""


# ========== 图片 CQ 码 ==========
def _read_image_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _build_image_cq(image_name: str) -> str | None:
    image_path = os.path.join(_EMOJI_DIR, image_name)
    loop = asyncio.get_running_loop()
    try:
        exists = await loop.run_in_executor(None, os.path.exists, image_path)
        if not exists:
            logger.error(f"图片不存在: {image_path}")
            return None
        data = await loop.run_in_executor(None, _read_image_file, image_path)
        b64 = base64.b64encode(data).decode()
        return f"[CQ:image,file=base64://{b64}]"
    except Exception as e:
        logger.error(f"读取图片失败: {e}")
        return None


async def _build_message(content: str | None, image: str | None) -> tuple[str | None, str | None]:
    """返回 (发送用 msg, 记录用 label)"""
    if image:
        cq = await _build_image_cq(image)
        if cq is None:
            return None, None
        return cq, f"[表情包: {image}]"
    if content:
        return content, content
    return None, None


# ========== 单条发送 ==========
async def _do_send(bot: Bot, action: dict) -> int | None:
    msg = action["content"]
    if not msg:
        return None
    try:
        if action["action"] == "private":
            result = await bot.send_private_msg(user_id=action["target"], message=msg)
        elif action["action"] == "group":
            result = await bot.send_group_msg(group_id=action["target"], message=msg)
        else:
            return None
        mid = result.get("message_id") if isinstance(result, dict) else None
        logger.info(f"发送{action['action']} -> {action.get('target')}: "
                    f"msg_id={mid} {action.get('label', msg)[:40]}")
        return mid
    except Exception as e:
        logger.error(f"发送失败: {e}")
        return None


# ========== 打字延迟 ==========
def _typing_delay(text: str) -> float:
    """模拟真人打字延迟：3字/秒，上限见 config.yaml"""
    n = len(text)
    return min(n / TYping_CHARS_PER_SECOND, TYping_MAX_DELAY)


# ========== 工具处理函数 ==========
async def _tool_send_private(bot: Bot, args: dict, collected: list, _cb) -> dict:
    targets = args.get("targets", [])
    valid = 0
    errors = []
    for t in sorted(targets, key=lambda x: x.get("order", 0)):
        qq = t.get("target_qq")
        content = t.get("content", "")
        image = t.get("image")
        msg, label = await _build_message(content or None, image)
        if not qq or not msg:
            if qq:
                errors.append(f"target_qq={qq}: 内容为空或图片不存在")
            continue
        if content and _contains_forbidden(content):
            errors.append(f"target_qq={qq}: 内容含禁止格式")
            continue
        delay = t.get("delay") if t.get("delay") is not None else (_typing_delay(content) if content else 0.5)
        collected.append({"action": "private", "target": qq, "content": msg, "label": label, "delay": delay})
        valid += 1
    return {"ok": True, "count": valid} if not errors else {"ok": True, "count": valid, "errors": errors}


async def _tool_send_group(bot: Bot, args: dict, collected: list, _cb) -> dict:
    gid = args.get("group_id")
    targets = args.get("targets", [])
    valid = 0
    errors = []
    for t in sorted(targets, key=lambda x: x.get("order", 0)):
        content = t.get("content", "")
        image = t.get("image")
        msg, label = await _build_message(content or None, image)
        if not gid or not msg:
            if gid:
                errors.append("内容为空或图片不存在")
            continue
        if content and _contains_forbidden(content):
            errors.append("内容含禁止格式")
            continue
        delay = t.get("delay") if t.get("delay") is not None else (_typing_delay(content) if content else 0.5)
        collected.append({"action": "group", "target": gid, "content": msg, "label": label, "delay": delay})
        valid += 1
    return {"ok": True, "count": valid} if not errors else {"ok": True, "count": valid, "errors": errors}


async def _tool_save_memory(_bot, args: dict, _collected, _cb) -> dict:
    memory_text = args.get("memory_text", "")
    if memory_text:
        result = await save_important_memory(memory_text, check_dup=check_important_memory_duplicate)
        return {"ok": True, "saved": result["saved"], "duplicate": result.get("duplicate", False)}
    return {"ok": False, "error": "empty memory_text"}


async def _tool_list_contacts(bot: Bot, args: dict, _collected, _cb) -> dict:
    scope = args.get("scope", "friends")
    try:
        if scope == "friends":
            data = await bot.get_friend_list()
            items = [{"nickname": f.get("nickname", ""), "user_id": f.get("user_id")} for f in data]
            return {"ok": True, "friends": items, "count": len(items)}
        elif scope == "groups":
            data = await bot.get_group_list()
            items = [{"group_name": g.get("group_name", ""), "group_id": g.get("group_id")} for g in data]
            return {"ok": True, "groups": items, "count": len(items)}
        elif scope == "group_members":
            gid = args.get("group_id")
            if not gid:
                return {"ok": False, "error": "group_members 需要 group_id"}
            data = await bot.get_group_member_list(group_id=gid)
            items = [{"nickname": m.get("nickname", ""), "card": m.get("card", "") or m.get("nickname", ""), "user_id": m.get("user_id")} for m in data]
            return {"ok": True, "members": items, "count": len(items)}
        return {"ok": False, "error": f"未知 scope: {scope}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _tool_schedule_wakeup(_bot, args: dict, _collected, wakeup_cb) -> dict:
    delay_s = args.get("delay_seconds", 0)
    reminder = args.get("reminder", "")
    if delay_s > 0 and wakeup_cb:
        await wakeup_cb(delay_s, reminder)
        return {"ok": True, "scheduled": True, "info": f"已设置 {delay_s} 秒后提醒"}
    return {"ok": False, "error": "无效参数"}


async def _tool_delete_memory(_bot, args: dict, _collected, _cb) -> dict:
    keyword = args.get("keyword", "")
    if keyword:
        deleted = await delete_important_memory(keyword)
        return {"ok": True, "deleted": deleted}
    return {"ok": False, "error": "empty keyword"}


async def _tool_no_action(_bot, _args, _collected, _cb) -> dict:
    return {"ok": True, "no_action": True}


async def _tool_describe_image(_bot, args: dict, _collected, _cb) -> dict:
    image_url = args.get("image_url", "")
    if not image_url:
        return {"ok": False, "error": "缺少 image_url"}
    try:
        description = await describe_image(image_url, args.get("prompt", ""))
        return {"ok": True, "description": description}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _tool_web_search(_bot, args: dict, _collected, _cb) -> dict:
    query = args.get("query", "")
    if not query:
        return {"ok": False, "error": "缺少搜索关键词"}
    result = await web_search(query)
    return {"ok": True, "query": query, "result": result}


async def _tool_get_weather(_bot, args: dict, _collected, _cb) -> dict:
    city = args.get("city", "")
    days = args.get("days", 1)
    if not city:
        return {"ok": False, "error": "缺少城市名称"}
    if days <= 1:
        days_str, limit = "3d", 1
    elif days <= 3:
        days_str, limit = "3d", days
    elif days <= 7:
        days_str, limit = "7d", days
    elif days <= 10:
        days_str, limit = "10d", days
    elif days <= 15:
        days_str, limit = "15d", days
    else:
        days_str, limit = "30d", min(days, 30)
    try:
        def _query():
            client = QWeather()
            if limit == 1:
                resp = client.get_weather_now(city)
                w = resp.now
                return f"{city} 实时天气：{w.text}，温度 {w.temp}°C（体感 {w.feelsLike}°C），{w.windDir} {w.windSpeed}km/h，湿度 {w.humidity}%"
            resp = client.get_weather_daily(city, days_str)
            parts = [f"{d.fxDate}: {d.textDay} {d.tempMin}~{d.tempMax}°C" for d in resp.daily[:limit]]
            return f"{city} {limit}天预报：\n" + "\n".join(parts)
        result = await asyncio.to_thread(_query)
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _tool_upload_file(bot: Bot, args: dict, _collected, _cb) -> dict:
    target_type = args.get("target_type")
    target_id = args.get("target_id")
    file_path = args.get("file_path", "")
    file_name = args.get("file_name", "")
    if not target_id or not file_path:
        return {"ok": False, "error": "缺少必要参数"}
    real_path = os.path.realpath(file_path)
    if not real_path.startswith(_ALLOWED_UPLOAD_DIR):
        return {"ok": False, "error": "文件路径不在允许范围内"}
    kwargs = {"file": file_path}
    if file_name:
        kwargs["name"] = file_name
    if target_type == "private":
        kwargs["user_id"] = target_id
        await bot.call_api("upload_private_file", **kwargs)
    elif target_type == "group":
        kwargs["group_id"] = target_id
        await bot.call_api("upload_group_file", **kwargs)
    else:
        return {"ok": False, "error": f"未知 target_type: {target_type}"}
    return {"ok": True}


async def _tool_set_friend_request(bot: Bot, args: dict, _collected, _cb) -> dict:
    flag = args.get("flag", "")
    approve = args.get("approve", False)
    remark = args.get("remark", "")
    if not flag:
        return {"ok": False, "error": "缺少 flag"}
    await bot.set_friend_add_request(flag=flag, approve=approve, remark=remark)
    return {"ok": True}


async def _tool_set_group_request(bot: Bot, args: dict, _collected, _cb) -> dict:
    flag = args.get("flag", "")
    sub_type = args.get("sub_type", "")
    approve = args.get("approve", False)
    reason = args.get("reason", "")
    logger.info(f"[FLAG追踪] AI调用 set_group_add_request: flag='{flag}', sub_type='{sub_type}', approve={approve}")
    if not flag or not sub_type:
        return {"ok": False, "error": "缺少 flag 或 sub_type"}
    await bot.set_group_add_request(flag=flag, sub_type=sub_type, approve=approve, reason=reason)
    return {"ok": True}


async def _tool_summarize_chat_history(bot: Bot, args: dict, _collected, _cb) -> dict:
    group_id = args.get("group_id")
    custom_prompt = args.get("custom_prompt", "")
    if not group_id:
        return {"ok": False, "error": "缺少 group_id"}
    prompt = custom_prompt or (
        "请总结此群聊的："
        "1) 群成员概况（活跃度、角色等）"
        "2) 群聊基本信息"
        "3) 群内日常讨论内容方向"
        "4) 对各活跃成员的印象和性格分析"
    )
    try:
        history_data = await bot.call_api("get_group_msg_history", group_id=group_id, count=CHAT_HISTORY_COUNT)
    except Exception as e:
        return {"ok": False, "error": f"获取群聊记录失败: {e}"}
    try:
        resp = await flash_client.chat.completions.create(
            model=FLASH_MODEL,
            messages=[{"role": "user", "content": f"{prompt}\n\n聊天记录：\n{history_data}"}],
            temperature=0.3, max_tokens=4096, timeout=60,
            extra_body={"thinking": {"type": "enabled"}},
        )
        summary = resp.choices[0].message.content or ""
        return {"ok": True, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": f"总结失败: {e}"}


async def _tool_recall_message(bot: Bot, args: dict, _collected, _cb) -> dict:
    message_id = args.get("message_id")
    if not message_id:
        return {"ok": False, "error": "缺少 message_id"}
    await bot.delete_msg(message_id=message_id)
    return {"ok": True}


async def _tool_get_forward_msg(bot: Bot, args: dict, _collected, _cb) -> dict:
    forward_id = args.get("forward_id", "")
    if not forward_id:
        return {"ok": False, "error": "缺少 forward_id"}
    result = await bot.get_forward_msg(id=forward_id)
    messages = result.get("messages", [])
    formatted = []
    for m in messages:
        sender = m.get("sender", {}).get("nickname", "未知")
        content = m.get("raw_message", "") or m.get("content", "")
        if isinstance(content, list):
            parts = []
            for seg in content:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        parts.append(seg.get("data", {}).get("text", ""))
                    elif seg.get("type") == "image":
                        parts.append("[图片]")
                    else:
                        parts.append(f"[{seg.get('type', '')}]")
                else:
                    parts.append(str(seg))
            content = "".join(parts)
        formatted.append(f"{sender}: {content}")
    return {"ok": True, "content": "\n".join(formatted)}


async def _tool_get_user_info(bot: Bot, args: dict, _collected, _cb) -> dict:
    user_id = args.get("user_id")
    if not user_id:
        return {"ok": False, "error": "缺少 user_id"}
    info = await bot.get_stranger_info(user_id=user_id)
    return {"ok": True, "info": info}


# ========== 工具分发表 ==========
_TOOL_HANDLERS = {
    "send_private_messages": _tool_send_private,
    "send_group_message": _tool_send_group,
    "save_important_memory": _tool_save_memory,
    "list_contacts": _tool_list_contacts,
    "schedule_wakeup": _tool_schedule_wakeup,
    "delete_important_memory": _tool_delete_memory,
    "no_action": _tool_no_action,
    "describe_image": _tool_describe_image,
    "web_search": _tool_web_search,
    "get_weather": _tool_get_weather,
    "upload_file": _tool_upload_file,
    "set_friend_add_request": _tool_set_friend_request,
    "set_group_add_request": _tool_set_group_request,
    "summarize_chat_history": _tool_summarize_chat_history,
    "recall_message": _tool_recall_message,
    "get_forward_msg": _tool_get_forward_msg,
    "get_user_info": _tool_get_user_info,
}


def make_tool_executor(bot: Bot, wakeup_cb=None) -> tuple:
    collected: list[dict] = []

    async def executor(tool_name: str, args: dict) -> dict:
        handler = _TOOL_HANDLERS.get(tool_name)
        if handler:
            return await handler(bot, args, collected, wakeup_cb)
        return {"ok": False, "error": f"unknown tool: {tool_name}"}

    return executor, collected


# ========== 执行发送动作（逐条发送 + 逐条记录真实时间和 msg_id） ==========
async def execute_collected_actions(bot: Bot, actions: list[dict]) -> int:
    """
    逐条发送，每条发送前检查是否有新消息打断。
    返回实际发送条数；若小于 len(actions) 说明被中断，已记录中断信息到记忆。
    """
    sent = 0
    for i, action in enumerate(actions):
        # 每条发送前检查是否有新消息到达
        async with batch_lock:
            if batch_pending:
                remaining = actions[i:]
                bot_qq = str(bot.self_id)
                interrupt_lines = []
                for ev, uid, nick, loc, fb in batch_pending:
                    msg_text = _reconstruct_message_text(ev, bot_qq)
                    msg_text = await _attach_media_urls(ev, bot, bot_qq, msg_text)
                    interrupt_lines.append(
                        f"{nick}({uid}): {msg_text}"
                    )

                remaining_parts = []
                for a in remaining:
                    label = a.get("label", a.get("content", ""))
                    remaining_parts.append(f"  [未发送] {label[:60]}")
                remaining_desc = "\n".join(remaining_parts)

                interrupt_desc = "\n".join(f"  → {l}" for l in interrupt_lines)

                # 区分已发送和未发送：sent>0 时说明部分成功
                sent_summary = f"已成功发送 {sent} 条。" if sent > 0 else ""
                note = (
                    f"⚠ 发送中断！{sent_summary}\n"
                    f"以下消息【未发出】，被新消息打断：\n{remaining_desc}\n"
                    f"打断的新消息：\n{interrupt_desc}\n"
                    f"请决定：重新发送未发出的内容，还是回应新消息。\n"
                    f"如果对话还没结束，就不要什么都不说"
                )
                await add_system_note(note)
                logger.info(
                    f"发送中断：已发送 {sent} 条，剩余 {len(remaining)} 条丢弃，"
                    f"新消息 {len(interrupt_lines)} 条"
                )
                return sent

        send_time = get_time()
        msg_id = await _do_send(bot, action)

        # 发送结果记录
        label = action.get("label", "")
        if label:
            await add_system_note(f"{send_time} msg_id={msg_id} → {label}")

        sent += 1

        delay = action.get("delay", 0)
        if delay > 0 and i < len(actions) - 1:
            await asyncio.sleep(delay)

    return sent


# ========== 记忆总结检查 ==========
async def maybe_summarize() -> None:
    n = await get_history_length()
    if n < SUMMARIZE_AT:
        return
    logger.info(f"历史 {n} 条触发总结")

    slice_for_summary = await get_history_slice(SUMMARIZE_RANGE[1])
    existing_imp = load_important_memories_text()

    result = await summarize_history(slice_for_summary, existing_imp)
    if result and "cut_point" in result and "new_important" in result:
        await truncate_history(result["cut_point"], result["new_important"])
        logger.info(f"总结完成：截断于 {result['cut_point']}")
    else:
        logger.warning("总结失败，使用简单截断")
        await truncate_history(SUMMARIZE_RANGE[0], [])


# ========== 合并处理一批消息 ==========
async def process_batch(bot: Bot, events_data: list[tuple]) -> bool:
    """处理一批消息。返回 True 表示发送被中断。"""
    if not events_data:
        return False

    emoji_hint = _build_emoji_hint()
    now = get_time()
    bot_qq = str(bot.self_id)
    lines = []
    for ev, uid, nick, loc, _ in events_data:
        msg_text = _reconstruct_message_text(ev, bot_qq)
        msg_text = await _attach_media_urls(ev, bot, bot_qq, msg_text)
        lines.append(
            f"【{now} {loc} {nick}({uid}) msg_id={ev.message_id}】{msg_text}"
        )
    await add_user_message("\n".join(lines))
    logger.info(f"合并处理 {len(events_data)} 条消息")

    wakeup_cb = _make_wakeup_callback(bot)

    pro_context = (f"现在是{now}。{emoji_hint}。"
                   f"引用消息用 [CQ:reply,id=消息ID]；@某人用 [CQ:at,qq=QQ号]。"
                   f"提醒：别人引用（reply）你的消息不一定是在跟你说话——"
                   f"可能是在对别人说话时引用了你的内容作为参考。除非对方明确是在对你说话，否则不需要回复。"
                   f"注意：用户可能将一句话拆成多条连续消息发送，"
                   f"若同一用户连续多条消息语义不完整，请合并理解其完整意图。")
    pending_info = _get_pending_requests_info()
    if pending_info:
        pro_context += f"\n{pending_info}"
    pro_messages = build_messages(pro_context)
    executor, collected = make_tool_executor(bot, wakeup_cb)
    async with reply_lock:
        result = await run_agent_loop(pro_messages, executor, tools=pro_tools)

    return await _handle_agent_result(bot, result, collected)


async def _handle_agent_result(bot: Bot, result: dict, collected: list[dict]) -> bool:
    """处理 agent 结果：记录、发送、检查中断。返回 True 表示被中断。"""
    await add_records(result["records"])
    final_content = result["final"]
    if collected:
        sent = await execute_collected_actions(bot, collected)
        if sent < len(collected):
            await maybe_summarize()
            return True
    elif final_content and final_content != "NO_ACTIONS":
        logger.warning(f"AI 未用工具输出文本，已丢弃: {final_content[:80]}")
        await add_system_note(
            f"注意：上轮未用工具，以下内容丢失：{final_content[:120]}"
        )
    else:
        logger.info("Agent 未发送消息")
    await maybe_summarize()
    return False


def _reconstruct_message_text(event, bot_qq: str) -> str:
    """
    从 raw_message（原始 CQ 码字符串）重建人类可读文本。
    绕过适配器对连续相同 @ 的合并，保留 @ 的原始位置和个数。
    """
    try:
        raw = getattr(event, "raw_message", "") or ""
        if not raw:
            return event.get_plaintext().strip()

        result = _parse_raw_cq(raw, bot_qq)
        return result.strip()
    except Exception as e:
        logger.warning(f"重建消息文本失败: {e}")
        return event.get_plaintext().strip()


def _parse_raw_cq(raw: str, bot_qq: str) -> str:
    """解析原始 CQ 码字符串，保留位置和个数。"""
    result = []
    i = 0
    while i < len(raw):
        if raw[i : i + 4] == "[CQ:":
            end = raw.find("]", i)
            if end == -1:
                result.append(raw[i:])
                break
            cq = raw[i + 4 : end]  # e.g. "at,qq=3976590169" or "reply,id=xxx"
            # 解析 CQ 类型和参数
            if "," in cq:
                cq_type = cq[: cq.index(",")]
                params_str = cq[cq.index(",") + 1 :]
            else:
                cq_type = cq
                params_str = ""
            # 解析键值对
            params = {}
            if params_str:
                # 简单按逗号分割（参数值不含逗号时有效）
                for p in re.split(r",(?=\w+=)", params_str):
                    if "=" in p:
                        k, v = p.split("=", 1)
                        params[k] = v

            if cq_type == "at":
                qq = params.get("qq", "")
                if qq == "all":
                    result.append("@全体成员")
                elif qq == bot_qq:
                    result.append("@我")
                else:
                    result.append(f"@QQ{qq}")
            elif cq_type == "reply":
                msg_id = params.get("id", "")
                result.insert(0, f"[引用msg_id={msg_id}]")
            elif cq_type == "image":
                result.append("[图片]")
            elif cq_type == "record":
                result.append("[语音]")
            elif cq_type == "video":
                result.append("[视频]")
            elif cq_type == "face":
                face_id = params.get("id", "")
                result.append(f"[表情{face_id}]")
            elif cq_type == "forward":
                fid = params.get("id", "")
                result.append(f"[合并转发 id={fid}]")
            elif cq_type == "file":
                result.append("[文件]")
            else:
                result.append(f"[{cq_type}]")
            i = end + 1
        else:
            result.append(raw[i])
            i += 1
    return "".join(result)


# ===== NapCat HTTP API 辅助 =====
async def _fetch_ptt_text(bot, message_id: int) -> str:
    """通过 bot.call_api 调用 fetch_ptt_text Action 获取语音转文字"""
    # 等 1s 让 NapCat 处理完语音
    await asyncio.sleep(1)
    try:
        result = await bot.call_api("fetch_ptt_text", message_id=message_id)
        text = (result or {}).get("text", "")
        if text:
            logger.info(f"语音转文字成功: msg_id={message_id} text={text[:50]}")
        else:
            logger.info(f"语音转文字返回空: msg_id={message_id}")
        return text
    except Exception as e:
        logger.warning(f"语音转文字失败: msg_id={message_id} error={e}")
        return ""


async def _get_file_url_via_bot(bot, file_id: str) -> str | None:
    """通过 bot.call_api 调用 NapCat 专属 Action get_file 获取文件 URL"""
    try:
        result = await bot.call_api("get_file", file_id=file_id, type="url")
        url = result.get("url", "")
        if url:
            logger.info(f"文件 URL 获取成功: file_id={file_id}")
            return url
        logger.warning(f"文件 URL 获取失败: file_id={file_id} result={result}")
        return None
    except Exception as e:
        logger.warning(f"get_file API 调用失败: file_id={file_id} error={e}")
        return None


def _has_media_segments(event) -> bool:
    """检查消息是否包含图片/语音/视频/文件段。"""
    try:
        for seg in event.get_message():
            if seg.type in ("image", "record", "video", "file", "forward"):
                return True
    except Exception:
        pass
    return False


async def _attach_media_urls(event, bot, bot_qq: str, text: str) -> str:
    """从事件段提取图片/语音/文件，替换占位。
    语音通过 NapCat HTTP /fetch_ptt_text 自动转文字。
    文件通过 bot.call_api('get_file') Action 获取下载 URL。"""
    try:
        msg = event.get_message()
        img_urls = []
        voice_count = 0
        file_ids = []
        for seg in msg:
            if seg.type == "image":
                url = seg.data.get("url", "") or seg.data.get("file", "")
                if url:
                    img_urls.append(url)
            elif seg.type == "record":
                voice_count += 1
            elif seg.type == "file":
                fid = seg.data.get("file", "")
                if fid:
                    file_ids.append(fid)

        for url in img_urls:
            text = text.replace("[图片]", f"[图片 url={url}]", 1)

        # 语音消息：通过 NapCat HTTP /fetch_ptt_text
        for _ in range(voice_count):
            voice_text = await _fetch_ptt_text(bot, event.message_id)
            replacement = f"[音频消息: {voice_text}]" if voice_text else "[音频消息: 未识别出文字]"
            text = text.replace("[语音]", replacement, 1)

        # 文件消息：通过 bot.call_api('get_file')
        for file_id in file_ids:
            file_url = await _get_file_url_via_bot(bot, file_id)
            if file_url:
                text = text.replace("[文件]", f"[文件 url={file_url}]", 1)
            else:
                text = text.replace("[文件]", "[文件: 获取URL失败]", 1)
    except Exception:
        pass
    return text


# ========== 合并任务调度（循环处理 + 中断重评估） ==========
_current_ai_task: asyncio.Task | None = None


async def batch_task(bot: Bot) -> None:
    """等待合并窗口后取出积压消息处理；若发送被中断则循环重新评估。"""
    global _current_ai_task

    await asyncio.sleep(MERGE_WINDOW)

    while True:
        async with batch_lock:
            if not batch_pending:
                break
            events_data = batch_pending[:]
            batch_pending.clear()

        try:
            interrupted = await process_batch(bot, events_data)
        except Exception as e:
            logger.error(f"处理批次消息失败: {e}")
            break

        if not interrupted:
            break

        await asyncio.sleep(MERGE_WINDOW)

    # 退出前异步回查：处理期间可能有新消息正好入队
    async def _requeue_check():
        global _current_ai_task
        await asyncio.sleep(0)
        async with batch_lock:
            if batch_pending and (_current_ai_task is None or _current_ai_task.done()):
                _current_ai_task = asyncio.create_task(batch_task(bot))

    asyncio.create_task(_requeue_check())


# ========== 被动消息响应器 ==========
receiving = on_message(priority=5, block=False)


@receiving.handle()
async def handle_receiving(bot: Bot, event: Event) -> None:
    global _current_ai_task
    if not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
        return
    # 纯媒体消息（图片/语音/视频）没有文字，但需要处理
    has_media = _has_media_segments(event)
    msg = event.get_plaintext().strip()
    if not msg and not has_media:
        return

    user_id = event.get_user_id()

    # 非好友速率限制
    if await _check_rate_limit(bot, str(user_id)):
        try:
            await bot.send_private_msg(user_id=user_id, message=RATE_LIMIT_MSG)
        except Exception:
            pass
        return

    if isinstance(event, GroupMessageEvent):
        nickname = event.sender.card or event.sender.nickname or "未知"
        location = f"群聊 {event.group_id}"
        fallback = {"group_id": event.group_id}
    else:
        nickname = event.sender.nickname or "未知"
        location = "私聊"
        fallback = user_id

    async with batch_lock:
        batch_pending.append((event, user_id, nickname, location, fallback))
        # 如果当前没有等待中的 batch_task，创建一个
        if _current_ai_task is None or _current_ai_task.done():
            _current_ai_task = asyncio.create_task(batch_task(bot))
        # 如果已有 batch_task 在等待/运行中，它会在唤醒时拿到新消息


# ========== 定时唤醒回调 ==========
def _make_wakeup_callback(bot: Bot):
    async def cb(delay_s: int, reminder: str):
        asyncio.create_task(_schedule_wakeup_task(delay_s, reminder, bot))
    return cb


async def _schedule_wakeup_task(delay_s: int, reminder: str, bot: Bot) -> None:
    await asyncio.sleep(delay_s)
    logger.info(f"定时唤醒: {reminder}")

    now = get_time()
    context = f"现在是{now}。定时唤醒: {reminder}。根据唤醒内容决定是否发消息或执行操作。"
    messages = build_messages(context)
    executor, collected = make_tool_executor(bot)

    async with reply_lock:
        result = await run_agent_loop(messages, executor)

    # 记录原始工具调用 + 工具结果到历史
    await add_records(result["records"])

    final_content = result["final"]
    if collected:
        await execute_collected_actions(bot, collected)
        logger.info(f"定时唤醒：发送了 {len(collected)} 条消息")


# ========== 主动问候（10 分钟，Flash 路由） ==========
async def greeting_loop() -> None:
    while True:
        await asyncio.sleep(GREETING_INTERVAL)
        now = get_time()
        logger.info(f"主动思考时间: {now}")

        async with batch_lock:
            has_pending = bool(batch_pending)
        if has_pending:
            logger.info("主动思考：有待处理消息，跳过")
            continue

        try:
            context = (
                f"现在是{now}。这是主动思考时间。"
                f"想聊天就回复 TAKE_ACTIONS，不想就回复 NO_ACTIONS。"
            )
            messages = build_messages(context)

            needs_action = await check_proactive_action(messages)
            if not needs_action:
                continue

            bot = get_bot()
            executor, collected = make_tool_executor(bot)

            async with reply_lock:
                result = await run_agent_loop(messages, executor)

            await add_records(result["records"])

            if collected:
                await execute_collected_actions(bot, collected)
                logger.info(f"主动思考：发送了 {len(collected)} 条消息")
        except Exception as e:
            logger.error(f"主动问候出错: {e}")


_background_task = None


@driver.on_startup
async def start_greeting() -> None:
    global _background_task
    _background_task = asyncio.create_task(greeting_loop())


# ========== 消息撤回事件处理 ==========
recall_notice = on_notice(priority=5, block=False)


@recall_notice.handle()
async def handle_recall(bot: Bot, event: NoticeEvent) -> None:
    global _recall_task
    note = None
    if isinstance(event, GroupRecallNoticeEvent):
        note = (f"群 {event.group_id} 中 QQ {event.user_id} "
                f"撤回了消息 msg_id={event.message_id}")
        if event.operator_id != event.user_id:
            note += f"（由 QQ {event.operator_id} 撤回）"
    elif isinstance(event, FriendRecallNoticeEvent):
        note = f"私聊中 QQ {event.user_id} 撤回了消息 msg_id={event.message_id}"
    else:
        return

    async with _recall_lock:
        _recall_pending.append(note)
        if _recall_task is None or _recall_task.done():
            _recall_task = asyncio.create_task(_process_recall_batch(bot))


async def _process_recall_batch(bot: Bot) -> None:
    """合并处理一段时间内的撤回事件，避免高频 API 调用。"""
    await asyncio.sleep(RECALL_MERGE_WINDOW)

    async with _recall_lock:
        notes = _recall_pending[:]
        _recall_pending.clear()

    if not notes:
        return

    combined = "\n".join(notes)
    await add_system_note(combined)
    logger.info(f"合并处理 {len(notes)} 条撤回")

    now = get_time()
    context = f"现在是{now}。最近有 {len(notes)} 条消息被撤回。根据规则，通常不需要回复撤回。"
    messages = build_messages(context)
    executor, collected = make_tool_executor(bot)

    async with reply_lock:
        result = await run_agent_loop(messages, executor)

    await add_records(result["records"])
    if collected:
        await execute_collected_actions(bot, collected)


# ========== 好友 / 群请求验证处理 ==========
friend_req = on_request(priority=5, block=False)
group_req = on_request(priority=5, block=False)


async def _handle_request(bot: Bot, desc: str, extra_info: str) -> None:
    """通用请求验证：跳过 Flash，直接 Pro 处理。"""
    now = get_time()
    context = (
        f"现在是{now}。{desc}\n"
        f"{extra_info}\n"
        f"请按照验证审核规则处理：先通知管理员（发送私聊），等待管理员回复。"
        f"管理员同意则调用对应工具通过，否则不操作。不要自行决定。"
    )
    messages = build_messages(context)
    executor, collected = make_tool_executor(bot)

    async with reply_lock:
        result = await run_agent_loop(messages, executor, tools=pro_tools)

    await add_records(result["records"])
    if collected:
        await execute_collected_actions(bot, collected)


@friend_req.handle()
async def handle_friend_request(bot: Bot, event: FriendRequestEvent) -> None:
    logger.info(f"好友请求: QQ {event.user_id}, flag={event.flag}")
    user_info = {}
    try:
        user_info = await bot.get_stranger_info(user_id=event.user_id)
    except Exception:
        pass
    nickname = user_info.get("nickname", "未知")
    sex = user_info.get("sex", "unknown")
    age = user_info.get("age", "未知")
    comment = event.comment or "无附加消息"

    # 存入待处理列表，供管理员回复时使用
    _pending_requests[event.flag] = {
        "type": "friend",
        "user_id": event.user_id,
        "nickname": nickname,
        "comment": comment,
        "expires_at": time.time() + _REQUEST_TIMEOUT,
    }
    info = (f"请求者信息：QQ {event.user_id}，昵称 {nickname}，性别 {sex}，"
            f"年龄 {age}，附加消息：{comment}。【操作标识 flag={event.flag}】")
    await _handle_request(bot, "收到好友添加请求。", info)


@group_req.handle()
async def handle_group_request(bot: Bot, event: GroupRequestEvent) -> None:
    sub_label = "加群申请" if event.sub_type == "add" else "邀请入群"
    logger.info(f"群请求({sub_label}): QQ {event.user_id}, 群 {event.group_id}, flag={event.flag}")
    user_info = {}
    try:
        user_info = await bot.get_stranger_info(user_id=event.user_id)
    except Exception:
        pass
    nickname = user_info.get("nickname", "未知")
    comment = event.comment or "无附加消息"

    # 存入待处理列表，供管理员回复时使用
    _pending_requests[event.flag] = {
        "type": "group",
        "sub_type": event.sub_type,
        "user_id": event.user_id,
        "group_id": event.group_id,
        "nickname": nickname,
        "comment": comment,
        "expires_at": time.time() + _REQUEST_TIMEOUT,
    }
    info = (f"{sub_label}：请求者 QQ {event.user_id}，昵称 {nickname}，"
            f"群号 {event.group_id}，附加消息：{comment}。"
            f"sub_type={event.sub_type}，【操作标识 flag={event.flag}】")
    await _handle_request(bot, f"收到{sub_label}。", info)


@driver.on_shutdown
async def shutdown_greeting() -> None:
    if _background_task:
        _background_task.cancel()
        try:
            await _background_task
        except asyncio.CancelledError:
            pass
