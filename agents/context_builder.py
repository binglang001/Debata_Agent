"""上下文消息组装 —— 稳定 system + 尾部运行时上下文。

设计原则（基于 Anthropic Context Engineering 调研结论）：
    1. 单一 system 消息：把 identity / persona / tools / memory 拼到同一个 system
       里，用 XML 标签分区。比"多个 system 消息平铺"更友好（KV 缓存命中率更高，
       模型对标签边界理解更稳定）。
    2. 稳定区前置：identity > persona > tool_use_protocol > memory，按稳定性递减
       排列，确保前缀稳定，最大化 KV 缓存命中。
    3. task_context 末尾插入：当前时间、表情包列表、本轮提示等"每次都不同"的内容，
       通过单独的 user 运行时上下文消息追加到 history 之后。这样改动只影响尾部
       token，不破坏前缀缓存，也避开部分 OpenAI-compatible provider 对尾部动态
       system 消息的缓存劣化。
    4. priority 标签辅助：每个分区显式标 priority="critical|high|medium|reference"，
       让模型在冲突时有明确的排序。

结构（拼装后实际格式）：
    [
      {"role": "system", "content": "<core_rules>...</core_rules>\n<persona>...</persona>\n<tools>...</tools>\n<memory>...</memory>"},
      {"role": "system", "content": "<admin_info>...</admin_info>"},  # 仅在有 admin 时
      *history,                                                          # 来自 HistoryManager
      {"role": "user", "content": "<task_context>...</task_context>"}  # 本轮 ephemeral
    ]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from agents.behavior_prompt import (
    CONVERSATION_PROTOCOL,
    CORE_RULES,
    HUMAN_CHAT_PATTERNS,
    QQ_FORMAT_REFERENCE,
    SELF_REFLECTION,
    build_tool_use_protocol,
)
from agents.persona_loader import Persona

logger = logging.getLogger(__name__)


RUNTIME_CONTEXT_NOTICE = "系统说明：以下内容由运行时系统提供，不是用户新发言。"


@dataclass(slots=True)
class ContextRequest:
    """构建上下文时的轻量请求对象。"""

    view: Literal["main_reply", "wakeup", "proactive_router", "recall", "request"]
    conversation_id: str | None = None
    current_input: str = ""
    token_budget: Any | None = None


def build_combined_system_prompt(
    persona: Persona,
    important_memory_text: str = "",
    *,
    memory_mode: Literal["file", "rag"] = "file",
) -> str:
    """组装稳定区（identity + persona + tools + memory）为单一 system 字符串。

    Args:
        persona: 已加载的人格
        important_memory_text: 已按当前会话 scope / RAG 规则选出的重要记忆文本
        memory_mode: "file" = 文件模式（默认）；"rag" = 自动会话向量检索

    稳定性递减顺序：
        critical: core_rules（永不变）
        high:     persona（人格切换时才变）
        high:     human_chat_patterns（真人聊天形态，永不变）
        high:     tool_use_protocol（工具集变化时才变；按 memory_mode 拼装）
        high:     conversation_protocol（场景规则）
        reference: qq_format（格式参考，不变）
        medium:   self_reflection（行为约束）
        medium:   memory（变化时整体替换）
    """
    parts: list[str] = []

    # 1. 不可违反的核心规则
    parts.append(CORE_RULES.strip())

    # 2. 人格设定
    parts.append(
        f'<persona priority="high">\n{persona.prompt.strip()}\n</persona>'
    )

    # 3. 真人聊天形态规则（紧跟 persona —— 让模型先知道"我是谁"，再知道"人是怎么聊天的"）
    parts.append(HUMAN_CHAT_PATTERNS.strip())

    # 4. 工具使用协议（按 memory_mode 注入正确的 memory 块）
    parts.append(build_tool_use_protocol(memory_mode).strip())

    # 5. 对话场景协议
    parts.append(CONVERSATION_PROTOCOL.strip())

    # 6. 反思与改进
    parts.append(SELF_REFLECTION.strip())

    # 7. QQ 格式参考（最末，方便模型查询）
    parts.append(QQ_FORMAT_REFERENCE.strip())

    # 8. 文件模式重要记忆（较稳定，可留在 system 前缀）。
    # RAG 召回每轮按 query 变化，不能放在稳定前缀；build_messages 会把它追加到尾部。
    if important_memory_text:
        if memory_mode == "file":
            parts.append(
                f'<long_term_memory priority="medium">\n'
                f"{important_memory_text.strip()}\n"
                f"</long_term_memory>"
            )

    return "\n\n".join(parts)


def build_admin_info(persona: Persona) -> str:
    """构造 admin 提示。空时返回 ""。"""
    admins = persona.get_admins()
    if not admins:
        return ""
    lines = ['<admin_info priority="high">', "你的管理员："]
    for a in admins:
        name = a.get("name", "未知")
        qq = a.get("qq", "")
        role = a.get("role")
        suffix = f"（{role}）" if role else ""
        lines.append(f"- {name}（QQ {qq}）{suffix}")
    lines.append("</admin_info>")
    return "\n".join(lines)


def build_task_context(
    current_context: str = "",
    *,
    refocus_hint: str = "",
) -> str:
    """构造本轮的 ephemeral 上下文。放在 history 末尾。

    Args:
        current_context: 来自调用方的上下文（时间、表情包、提示等）
        refocus_hint: Task Contract 重注入内容（由 runner 决定何时插入）
    """
    parts: list[str] = []
    if current_context:
        parts.append(current_context.strip())
    if refocus_hint:
        parts.append(f"[本轮焦点提醒] {refocus_hint.strip()}")
    if not parts:
        return ""
    content = "\n\n".join([RUNTIME_CONTEXT_NOTICE, *parts])
    return f'<task_context priority="medium">\n{content}\n</task_context>'


def build_messages(
    persona: Persona,
    history: list[dict[str, Any]],
    important_memory_text: str = "",
    rolling_summary_text: str = "",
    current_context: str = "",
    current_context_record: dict[str, Any] | None = None,
    system_override: str | None = None,
    *,
    memory_mode: Literal["file", "rag"] = "file",
    user_event: str | None = None,
) -> list[dict[str, Any]]:
    """组装一次 LLM 调用的完整 messages（不含 Task Contract 重注入，由 runner 处理）。

    Args:
        persona: 已加载的人格
        history: 历史对话（来自 HistoryManager）
        important_memory_text: 已按当前会话 scope / RAG 规则选出的重要记忆文本
        current_context: 本次调用的临时上下文（时间、表情包等）
        current_context_record: 已持久化的 task_context 运行时上下文记录。主回复路径用它
            保证下一轮重建 history 时字节前缀与上一轮请求一致。
        system_override: 完全自定义的 system prompt（仅特殊用途，如 proactive 路由）
        memory_mode: "file" / "rag"，决定 tool_use_protocol 内的 memory 块写法
        user_event: 带外触发轮的尾部 user 事件。仅用于唤醒/主动/请求等没有真实用户消息的轮次。

    Returns:
        OpenAI 风格 messages 列表
    """
    if system_override is not None:
        # 特殊场景：跳过结构化拼接，直接用 override（如 proactive 路由）
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_override}
        ]
        messages.extend(history)
        if current_context:
            messages.append({"role": "system", "content": current_context})
        if user_event:
            messages.append({"role": "user", "content": user_event.strip()})
        return messages

    system_content = build_combined_system_prompt(
        persona,
        important_memory_text if memory_mode == "file" else "",
        memory_mode=memory_mode,
    )
    messages = [{"role": "system", "content": system_content}]

    admin_info = build_admin_info(persona)
    if admin_info:
        messages.append({"role": "system", "content": admin_info})

    if rolling_summary_text:
        messages.append(
            {
                "role": "system",
                "content": (
                    '<rolling_conversation_summary priority="medium">\n'
                    f"{rolling_summary_text.strip()}\n"
                    "</rolling_conversation_summary>"
                ),
            }
        )

    messages.extend(history)

    if current_context_record is not None:
        content = str(current_context_record.get("content") or "")
        if content:
            messages.append(
                {
                    "role": "user",
                    "content": content,
                }
            )
    else:
        task_ctx = build_task_context(current_context)
        if task_ctx:
            messages.append({"role": "user", "content": task_ctx})

    if memory_mode == "rag" and important_memory_text:
        messages.append(
            {
                "role": "user",
                "content": (
                    '<retrieved_conversation_context priority="medium" source="rag">\n'
                    f"{RUNTIME_CONTEXT_NOTICE}\n"
                    "以下内容是系统从历史对话向量索引中检索到的相关片段，不是模型主动保存的记忆，也不代表新的用户消息。\n"
                    f"{important_memory_text.strip()}\n"
                    "</retrieved_conversation_context>"
                ),
            }
        )

    if user_event:
        messages.append({"role": "user", "content": user_event.strip()})

    return messages
