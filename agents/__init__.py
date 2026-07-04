"""Debata_Agent Agent 层。

模块组成：
    persona_loader      —— 加载 data/personas/{name}/persona_prompt.py
    behavior_prompt     —— 行为提示词（按 XML 标签分层 + priority）
    context_builder     —— 组装单一 system + XML 标签化的 messages
    base                —— AgentRunResult 等数据类型
    runner              —— 通用工具循环 + Task Contract 重注入
    chat_agent          —— 主聊天 Agent
    proactive_agent     —— 主动思考路由（小模型）
    summary_agent       —— 历史总结
"""

from .base import AgentRunResult, FinishReason, ToolExecutor
from .behavior_prompt import (
    CONVERSATION_PROTOCOL,
    CORE_RULES,
    HUMAN_CHAT_PATTERNS,
    QQ_FORMAT_REFERENCE,
    SELF_REFLECTION,
    build_tool_use_protocol,
)
from .chat_agent import ChatAgent
from .context_builder import (
    ContextRequest,
    build_admin_info,
    build_combined_system_prompt,
    build_messages,
    build_task_context,
)
from .persona_agent import PersonaAgent
from .persona_gen_agent import PersonaBrief, PersonaGenAgent
from .persona_loader import (
    Persona,
    find_persona_dir,
    list_available_personas,
    load_persona,
    validate_persona_name,
)
from .proactive_agent import ProactiveRouterAgent
from .runner import DEFAULT_NO_FEEDBACK_TOOLS, SEND_TOOL_NAMES, AgentRunner
from .social_agent import SocialAgent
from .subconscious_agent import SubconsciousAgent
from .summary_agent import SummaryAgent

__all__ = [
    # base
    "AgentRunResult",
    "FinishReason",
    "ToolExecutor",
    # persona
    "Persona",
    "load_persona",
    "find_persona_dir",
    "list_available_personas",
    "validate_persona_name",
    # prompts
    "CORE_RULES",
    "CONVERSATION_PROTOCOL",
    "HUMAN_CHAT_PATTERNS",
    "SELF_REFLECTION",
    "QQ_FORMAT_REFERENCE",
    "build_tool_use_protocol",
    # context
    "build_messages",
    "ContextRequest",
    "build_combined_system_prompt",
    "build_admin_info",
    "build_task_context",
    # agents
    "AgentRunner",
    "ChatAgent",
    "PersonaAgent",
    "ProactiveRouterAgent",
    "SocialAgent",
    "SubconsciousAgent",
    "SummaryAgent",
    "PersonaGenAgent",
    "PersonaBrief",
    "DEFAULT_NO_FEEDBACK_TOOLS",
    "SEND_TOOL_NAMES",
]
