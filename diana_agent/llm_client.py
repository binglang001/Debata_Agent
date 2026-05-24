"""DeepSeek API 客户端 —— 流式输出 + 小模型路由 + 记忆去重/总结"""

import asyncio
import json
import os
from typing import Any, Callable

from openai import AsyncOpenAI
from nonebot.log import logger

from .config import (
    CHAT_HISTORY_COUNT,
    FIRST_TOKEN_TIMEOUT,
    FLASH_MODEL,
    MAX_LOOPS,
    MAX_TOKENS,
    PRO_MODEL,
    TEMPERATURE,
)
from .memory import SUMMARIZE_RANGE

# ========== 工具定义 ==========
pro_tools = [
    {
        "type": "function",
        "function": {
            "name": "send_private_messages",
            "description": "向 QQ 用户发送私聊消息。可混合文字/图片，按 order 排序，delay 控制间隔。"
                           "可在 content 开头加 [CQ:reply,id=消息ID] 引用回复。"
                           "send_only=true 则正常发送后直接结束。",
            "parameters": {
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target_qq": {"type": "integer", "description": "接收者 QQ 号"},
                                "content": {"type": "string", "description": "消息正文；可在开头加 [CQ:reply,id=msg_id] 引用"},
                                "image": {"type": "string", "description": "表情包文件名（二选一）"},
                                "order": {"type": "integer", "description": "发送顺序"},
                                "delay": {"type": "number", "description": "本条后等待秒数"},
                            },
                            "required": ["target_qq", "order"],
                        },
                    },
                    "send_only": {"type": "boolean", "default": False},
                },
                "required": ["targets"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_group_message",
            "description": "向 QQ 群发送消息。可混合文字/图片，按 order 排序，delay 控制间隔。"
                           "可在 content 开头加 [CQ:reply,id=msg_id] 引用；@人用 [CQ:at,qq=QQ号]。"
                           "send_only=true 则正常发送后直接结束。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer"},
                    "targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "消息正文；@人用 [CQ:at,qq=QQ号]"},
                                "image": {"type": "string", "description": "表情包文件名（二选一）"},
                                "order": {"type": "integer", "description": "发送顺序"},
                                "delay": {"type": "number", "description": "本条后等待秒数"},
                            },
                            "required": ["order"],
                        },
                    },
                    "send_only": {"type": "boolean", "default": False},
                },
                "required": ["group_id", "targets"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_important_memory",
            "description": "永久保存重要记忆（人物、约定、秘密等）",
            "parameters": {
                "type": "object",
                "properties": {"memory_text": {"type": "string"}},
                "required": ["memory_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "查询好友/群/群成员",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["friends", "groups", "group_members"]},
                    "group_id": {"type": "integer"},
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_wakeup",
            "description": "设置定时提醒。到时会通知你，由你决定是否操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "delay_seconds": {"type": "integer", "description": "多少秒后唤醒"},
                    "reminder": {"type": "string", "description": "唤醒时收到的提醒内容"},
                },
                "required": ["delay_seconds", "reminder"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_important_memory",
            "description": "删除一条重要记忆。当记忆过时、重复、或不再需要时使用。传入要删除的记忆关键词进行模糊匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要删除的记忆关键词，会匹配包含此关键词的记忆"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "no_action",
            "description": "当不需要发送任何消息、不需要执行任何操作时调用。调用此工具即表示本轮不发言。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息。当需要查找当前新闻、事实核查、最新资讯时使用。返回相关结果的摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气。支持实时天气和多日预报。参数 city 为城市名称，days 为预报天数（1-7，默认1）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称，如 宁德、北京"},
                    "days": {"type": "integer", "description": "预报天数 1-7，默认 1"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_file",
            "description": "向私聊或群聊发送本地文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_type": {"type": "string", "enum": ["private", "group"], "description": "目标类型"},
                    "target_id": {"type": "integer", "description": "QQ号或群号"},
                    "file_path": {"type": "string", "description": "本地文件路径"},
                    "file_name": {"type": "string", "description": "显示文件名（可选）"},
                },
                "required": ["target_type", "target_id", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_friend_add_request",
            "description": "处理好友添加请求。必须先经管理员同意才能调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "flag": {"type": "string", "description": "请求的 flag，从系统通知中获取"},
                    "approve": {"type": "boolean", "description": "是否同意"},
                    "remark": {"type": "string", "description": "好友备注（通过时可选）"},
                },
                "required": ["flag", "approve"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_group_add_request",
            "description": "处理加群申请或群邀请。必须先经管理员同意才能调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "flag": {"type": "string", "description": "请求的 flag，从系统通知中获取"},
                    "sub_type": {"type": "string", "enum": ["add", "invite"], "description": "add=加群申请，invite=邀请入群"},
                    "approve": {"type": "boolean", "description": "是否同意"},
                    "reason": {"type": "string", "description": "拒绝理由（拒绝时可选）"},
                },
                "required": ["flag", "sub_type", "approve"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_chat_history",
            "description": "获取群聊聊天记录并总结。了解群基本情况、成员、氛围、对各成员的印象。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号"},
                    "custom_prompt": {"type": "string", "description": "自定义总结提示词，不填则使用默认"},
                },
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_message",
            "description": "撤回已发送的消息。仅可撤回 2 分钟内发出的消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer", "description": "要撤回的消息 ID"},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forward_msg",
            "description": "提取合并转发消息的内容。当用户发送合并转发消息时，使用此工具获取其中每条消息的具体内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "forward_id": {"type": "string", "description": "合并转发消息的 ID，从消息中的 [合并转发 id=xxx] 标记获取"},
                },
                "required": ["forward_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": "获取指定 QQ 用户的公开信息（昵称、性别、年龄等）。用于了解陌生人或新成员。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "目标用户 QQ 号"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_image",
            "description": "理解图片内容。传入图片 URL 和可选的提示词，返回图片的文字描述。"
                           "当用户发送图片时，先调用此工具获取图片内容再回复。"
                           "可根据场景自定义 prompt，如识别文字、分析表情、判断场景等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_url": {"type": "string", "description": "图片 URL，从消息中的 [图片 url=xxx] 标记获取"},
                    "prompt": {"type": "string", "description": "可选，自定义理解提示。不填则默认描述图片内容"},
                },
                "required": ["image_url"],
            },
        },
    },
]

# ========== 客户端 ==========
pro_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY", ""),
    base_url="https://api.deepseek.com",
)
flash_client = AsyncOpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY", ""),
    base_url="https://api.deepseek.com",
)

ToolExecutor = Callable[[str, dict], Any]


def _is_send_tool(name: str) -> bool:
    return name in ("send_private_messages", "send_group_message")


# ========== 流式 API 调用（带首 token 超时） ==========
async def _stream_api_call(msgs: list[dict], client=None, model=PRO_MODEL,
                          reasoning=True, tools=None) -> Any:
    """
    流式调用 API，FIRST_TOKEN_TIMEOUT 秒内无首 token 则抛出 TimeoutError。
    reasoning: 是否启用深度思考（flash 应关闭）。
    tools: 工具列表，默认使用 pro_tools。
    """
    if client is None:
        client = pro_client
    if tools is None:
        tools = pro_tools
    extra = {"thinking": {"type": "enabled" if reasoning else "disabled"}}
    stream = await client.chat.completions.create(
        model=model,
        messages=msgs,
        tools=tools,
        tool_choice="auto",
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        stream=True,
        stream_options={"include_usage": True},
        extra_body=extra,
    )

    chunks = []
    got_first = False

    async def _get_first():
        nonlocal got_first
        async for chunk in stream:
            if not got_first:
                got_first = True
            chunks.append(chunk)

    try:
        await asyncio.wait_for(_get_first(), timeout=FIRST_TOKEN_TIMEOUT)
    except asyncio.TimeoutError:
        if not got_first:
            raise asyncio.TimeoutError(f"首 token 超时 ({FIRST_TOKEN_TIMEOUT}s)")
        # 已收到首 token 但后续卡住 → 用已有 chunks
        logger.warning("流式输出中断（首 token 已收到）")

    # 拼接 chunks
    if not chunks:
        raise Exception("流式输出无数据")

    # 构造完整的 message
    content = ""
    tool_calls_data: dict[int, dict] = {}
    reasoning_content = ""

    for c in chunks:
        if not c.choices:
            continue
        delta = c.choices[0].delta
        if delta.content:
            content += delta.content
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            reasoning_content += delta.reasoning_content
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_data:
                    tool_calls_data[idx] = {
                        "id": tc.id or "",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.id:
                    tool_calls_data[idx]["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        tool_calls_data[idx]["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        tool_calls_data[idx]["function"]["arguments"] += tc.function.arguments

    tool_calls = [
        {"id": v["id"], "type": "function", "function": v["function"]}
        for _, v in sorted(tool_calls_data.items())
    ] if tool_calls_data else None

    # 构造 simple namespace-like object
    class Msg:
        pass
    msg = Msg()
    msg.content = content.strip() if content else ""
    msg.tool_calls = tool_calls
    msg.reasoning_content = reasoning_content if reasoning_content else None
    return msg


# ========== 多轮工具调用循环 ==========
async def run_agent_loop(messages: list[dict], tool_executor: ToolExecutor,
                          client=None, model=PRO_MODEL,
                          reasoning=True, tools=None) -> dict:
    """
    多轮工具调用循环。
    返回 {"final": str, "records": [...]}。
    reasoning: 是否启用深度思考。
    tools: 工具列表，默认 pro_tools。
    """
    if tools is None:
        tools = pro_tools
    tool_names = [t["function"]["name"] for t in tools] if tools else []
    logger.info(f"[{model}] 可用工具 ({len(tool_names)}): {tool_names}")
    msgs = list(messages)
    all_records: list[dict] = []
    final_content = ""
    loop_count = 0
    max_loops = MAX_LOOPS

    while loop_count < max_loops:
        loop_count += 1
        logger.info(f"AI 调用轮次 {loop_count}，消息数 {len(msgs)}")

        try:
            assistant = await _stream_api_call(msgs, client, model, reasoning, tools)
        except asyncio.TimeoutError:
            logger.error(f"API 超时 ({FIRST_TOKEN_TIMEOUT}s 无首 token)")
            return {"final": "", "records": all_records}
        except Exception as e:
            logger.error(f"API 调用失败 (轮次 {loop_count}): {e}")
            return {"final": "", "records": all_records}

        logger.info(f"AI 响应 (轮次 {loop_count}): content={assistant.content}, "
                    f"tool_calls={len(assistant.tool_calls or [])}")

        # 构建 assistant 消息记录（保留 tool_calls 原始格式）
        record: dict = {"role": "assistant", "content": assistant.content or ""}
        if hasattr(assistant, "reasoning_content") and assistant.reasoning_content:
            record["reasoning_content"] = assistant.reasoning_content
        if assistant.tool_calls:
            record["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                for tc in assistant.tool_calls
            ]
        msgs.append(record)
        all_records.append(record)

        # 无工具调用 → 重试（引导模型使用工具）
        if not assistant.tool_calls:
            content = (assistant.content or "").strip()
            logger.warning(f"AI 未使用工具: {content[:60]}")
            if loop_count < max_loops - 1:
                err_record = {
                    "role": "system",
                    "content": "错误：未调用工具。必须调用 send_* 发消息或 no_action 不操作。纯文本无效。"
                }
                msgs.append(err_record)
                all_records.append(err_record)
                continue
            final_content = content
            break

        # 执行工具调用
        tc_results = []
        tool_records = []
        for tc in assistant.tool_calls:
            func_name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError as e:
                result = {"ok": False, "error": f"JSON parse error: {e}"}
                args = {}
            else:
                try:
                    result = await tool_executor(func_name, args)
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
            tc_results.append({"name": func_name, "args": args, "result": result})
            tool_records.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

        msgs.extend(tool_records)
        all_records.extend(tool_records)

        # no_action → 退出
        if any(r["name"] == "no_action" for r in tc_results):
            final_content = "NO_ACTIONS"
            break

        # 全部工具无需反馈且成功 → 直接退出
        NO_FEEDBACK_TOOLS = {"save_important_memory", "delete_important_memory",
                             "no_action",
                             "set_friend_add_request", "set_group_add_request"}
        all_no_feedback = True
        for r in tc_results:
            name = r["name"]
            ok = r["result"].get("ok", True)
            if _is_send_tool(name):
                if not r["args"].get("send_only", False) or not ok:
                    all_no_feedback = False
            elif name in NO_FEEDBACK_TOOLS:
                if not ok:
                    all_no_feedback = False
            else:
                all_no_feedback = False

        if all_no_feedback:
            final_content = (assistant.content or "").strip()
            break

    if loop_count >= max_loops:
        logger.warning(f"达到最大循环次数 {max_loops}，强制退出")

    return {"final": final_content, "records": all_records}


# ========== 小模型路由 ==========
async def check_proactive_action(messages: list[dict]) -> bool:
    """用 flash 模型判断是否需要主动操作。返回 True 表示需要。"""
    try:
        check_msgs = list(messages)
        check_msgs.append({
            "role": "system",
            "content": "你现在处于后台主动思考模式。快速判断：是否需要发消息给任何人？"
                       "只回复 TAKE_ACTIONS（需要）或 NO_ACTIONS（不需要）。不要解释。"
        })
        response = await flash_client.chat.completions.create(
            model=FLASH_MODEL,
            messages=check_msgs,
            temperature=0.3,
            max_tokens=16,
            timeout=15,
            extra_body={"thinking": {"type": "enabled"}},
        )
        text = (response.choices[0].message.content or "").strip().upper()
        logger.info(f"Flash 路由判断: {text}")
        return "TAKE_ACTIONS" in text
    except Exception as e:
        logger.error(f"Flash 路由失败: {e}，默认不操作")
        return False


# ========== 重要记忆去重（flash 模型） ==========
async def check_important_memory_duplicate(existing: list[dict], new_text: str) -> bool:
    """用 flash 模型检查新记忆是否与现存的重复。返回 True 表示重复。"""
    try:
        existing_list = "\n".join(f"- {m['content']}" for m in existing)
        prompt = (
            f"现有重要记忆：\n{existing_list}\n\n"
            f"新记忆：{new_text}\n\n"
            f"新记忆的信息是否已被现有记忆覆盖？回复 JSON："
            f'{{"duplicate": true/false}}'
        )
        response = await flash_client.chat.completions.create(
            model=FLASH_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=64,
            timeout=15,
            extra_body={"thinking": {"type": "enabled"}},
        )
        text = (response.choices[0].message.content or "").strip()
        return '"duplicate": true' in text.lower()
    except Exception as e:
        logger.warning(f"去重检查失败: {e}，默认不跳过")
        return False


# ========== 记忆总结 ==========
async def summarize_history(
    history_slice: list[dict],
    existing_important: str,
) -> dict | None:
    """用 flash 模型总结前段历史，返回 {"cut_point": int, "new_important": [dict]}。"""
    try:
        history_text = "\n".join(
            f"[{m['role']}] {m.get('content', '')}" for m in history_slice
        )
        prompt = f"""你是当前角色的记忆管理系统。以下是该角色的前 {len(history_slice)} 条对话历史。

<现存重要记忆>
{existing_important}
</现存重要记忆>

<对话历史>
{history_text}
</对话历史>

<任务>
1. 从对话历史中提取新增的重要信息（人名、关系、约定、秘密、偏好等），不要存日常闲聊。
2. 检查历史中第 {SUMMARIZE_RANGE[0]}~{SUMMARIZE_RANGE[1]} 条之间，找一个语义完整的位置（话说完、话题转换处）作为截断点。
3. 合并现存重要记忆和新提取的记忆，去重，形成新一份重要记忆。

返回 JSON：
```json
{{"cut_point": 数字, "new_important": [{{"timestamp": "时间", "content": "一句话描述"}}, ...]}}
```

注意：cut_point 必须在 {SUMMARIZE_RANGE[0]}~{SUMMARIZE_RANGE[1]} 之间。
"""
        response = await flash_client.chat.completions.create(
            model=FLASH_MODEL,
            messages=[
                {"role": "system", "content": "你是当前角色的记忆管理系统。从对话历史中提取重要信息并确定截断点。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=8192,
            timeout=60,
            extra_body={"thinking": {"type": "enabled"}},
        )
        text = response.choices[0].message.content or ""
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        logger.error(f"总结返回无法解析: {text[:200]}")
        return None
    except Exception as e:
        logger.error(f"记忆总结失败: {e}")
        return None
