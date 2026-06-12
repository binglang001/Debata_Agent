"""Debata_Agent 工具系统。

模块组成：
    base                —— ITool / ToolContext / ToolRegistry / @tool 装饰器
    schemas             —— 全部 Pydantic args 模型
    message_builder     —— 表情包/CQ 码/打字延迟等消息辅助
    messaging           —— send_private / send_group / recall / upload_file
    memory_tools        —— save / update / delete_important_memory
    platform_tools      —— list_contacts / get_user_info / get_forward_msg /
                           set_*_add_request / summarize_chat_history /
                           summarize_conversation / recall_history
    agent_task_tools    —— start_agent_task 后台子 Agent 资料处理
    control_tools       —— no_action / schedule_wakeup
    feature_tools       —— describe_image / web_search / get_weather

公开 API：
    ToolContext         —— 给 AgentRunner 用的执行上下文
    ToolRegistry        —— 工具集合
    build_default_registry(config)
                        —— 构造稳定工具集合的工厂
    get_default_specs() —— 全部已注册的工具规格
"""

from __future__ import annotations

import logging
from dataclasses import replace

# 导入各模块的副作用：注册装饰器把工具加入全局列表
from . import (  # noqa: F401
    agent_task_tools,
    control_tools,
    feature_tools,
    group_admin_tools,
    memory_tools,
    messaging,
    platform_tools,
    qq_action_tools,
    tool_search_tools,
    workspace_tools,
)
from .base import (
    DEFAULT_NO_FEEDBACK_TOOLS,
    AgentTaskCallback,
    IVisionService,
    IWeatherService,
    IWebSearchService,
    ToolContext,
    ToolRegistry,
    ToolSchemaMode,
    ToolSpec,
    WakeupCallback,
    clear_default_registry,
    get_default_specs,
    tool,
)
from .message_builder import (
    FORBIDDEN_TAGS,
    build_emoji_hint,
    build_message_action,
    contains_forbidden,
    list_emoji_files,
    list_emoji_names,
    resolve_emoji_path,
    resolve_send_image_ref,
    typing_delay,
)

logger = logging.getLogger(__name__)


__all__ = [
    # base
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "ToolSchemaMode",
    "WakeupCallback",
    "IVisionService",
    "IWebSearchService",
    "IWeatherService",
    "DEFAULT_NO_FEEDBACK_TOOLS",
    "AgentTaskCallback",
    "tool",
    "get_default_specs",
    "clear_default_registry",
    # message_builder
    "FORBIDDEN_TAGS",
    "build_emoji_hint",
    "build_message_action",
    "contains_forbidden",
    "list_emoji_files",
    "list_emoji_names",
    "resolve_emoji_path",
    "resolve_send_image_ref",
    "typing_delay",
    # factory
    "build_default_registry",
    "MEMORY_FILE_TOOLS",
    "MEMORY_TOOLS",
    "PLATFORM_OPTIONAL_TOOLS",
    "FEATURE_TOOL_FEATURES",
    "FULL_SCHEMA_TOOLS",
    "STUB_SCHEMA_TOOLS",
]


# ============================================================
# 工厂：按配置决定启用工具集合
# ============================================================


MEMORY_TOOLS: set[str] = {
    "save_important_memory",
    "update_important_memory",
    "delete_important_memory",
}
"""长期重要记忆工具。RAG 只是历史召回，不再替代 important.json。"""

# 兼容旧测试/旧导入名。
MEMORY_FILE_TOOLS = MEMORY_TOOLS


PLATFORM_OPTIONAL_TOOLS: set[str] = {
    "set_friend_add_request",
    "set_group_add_request",
}
"""依赖白名单模式的工具——whitelist.mode == "verify" 时才有意义。
其它模式下保留注册，仅记录警告。"""


FEATURE_TOOL_FEATURES: dict[str, str] = {
    "describe_image": "vision",
    "web_search": "web_search",
    "get_weather": "weather",
    "send_voice_message": "tts",
}
"""feature 工具名 → features 字典里的字段名。"""


FULL_SCHEMA_TOOLS: set[str] = {
    "send_private_messages",
    "send_group_message",
    "commit_send_attempt",
    "no_action",
    "save_important_memory",
    "update_important_memory",
    "delete_important_memory",
    "get_recent_chat_messages",
    "get_forward_msg",
    "recall_message",
    "describe_image",
    "tool_search",
    "schedule_wakeup",
    "list_contacts",
    "get_user_info",
    "get_group_self_role",
    "get_msg",
    "send_poke",
    "set_msg_emoji_like",
    "set_friend_add_request",
    "set_group_add_request",
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "delete_file",
    "run_python",
    "web_search",
    "get_weather",
}
"""常驻完整 schema 工具。"""


STUB_SCHEMA_TOOLS: set[str] = {
    "start_agent_task",
    "summarize_chat_history",
    "summarize_conversation",
    "filter_archive_records",
    "recall_history",
    "upload_file",
    "send_voice_message",
    "set_group_kick",
    "set_group_ban",
    "set_group_whole_ban",
    "set_group_leave",
}
"""低频/高风险/大 schema 工具：常驻名称与简述，调用前通过 tool_search 查询参数摘要。"""


_STUB_SHORT_DESCRIPTIONS: dict[str, str] = {
    "start_agent_task": "低频资料整理工具。先用 tool_search 查询参数摘要；需要完整 schema 时 detail=full。",
    "summarize_chat_history": "低频群历史总结工具。先用 tool_search 查询参数摘要；需要完整 schema 时 detail=full。",
    "summarize_conversation": "低频本地会话总结工具。先用 tool_search 查询参数摘要；需要完整 schema 时 detail=full。",
    "filter_archive_records": "低频归档筛选工具。先用 tool_search 查询参数摘要；需要完整 schema 时 detail=full。",
    "recall_history": "低频归档检索工具。先用 tool_search 查询参数摘要；需要完整 schema 时 detail=full。",
    "upload_file": "文件发送工具。先用 tool_search 查询参数摘要和约束；需要完整 schema 时 detail=full。",
    "send_voice_message": "语音发送工具。先用 tool_search 查询参数摘要；需要完整 schema 时 detail=full。",
    "set_group_kick": "高风险群管理工具。先用 tool_search 查询参数摘要和约束；需要完整 schema 时 detail=full。",
    "set_group_ban": "高风险群管理工具。先用 tool_search 查询参数摘要和约束；需要完整 schema 时 detail=full。",
    "set_group_whole_ban": "高风险群管理工具。先用 tool_search 查询参数摘要和约束；需要完整 schema 时 detail=full。",
    "set_group_leave": "高风险群管理工具。先用 tool_search 查询参数摘要和约束；需要完整 schema 时 detail=full。",
}


_STUB_RISK_LEVELS: dict[str, str] = {
    "upload_file": "medium",
    "send_voice_message": "medium",
    "start_agent_task": "medium",
    "set_group_kick": "high",
    "set_group_ban": "high",
    "set_group_whole_ban": "high",
    "set_group_leave": "high",
}


def build_default_registry(config) -> ToolRegistry:
    """根据 RootConfig 构造稳定工具 Registry。

    规则：
        - messaging 工具（send_*/recall）：始终启用
        - upload_file：始终注册为 stub，执行受 ctx.workspace_dir / adapter 进一步限制
        - memory 工具：始终启用；RAG 是历史召回，不替代 important.json
        - platform 工具：始终启用（按需）
        - control 工具：始终启用
        - feature 工具：始终注册；执行时按 ctx 是否注入 service 返回成功或未启用
        - 低频/高风险/大 schema 工具：始终注册为 stub，通过 tool_search 查询参数摘要

    Args:
        config: RootConfig 实例

    Returns:
        ToolRegistry 实例
    """
    specs = list(get_default_specs())
    memory_mode = config.features.long_term_memory.mode
    features = config.features

    enabled: list[ToolSpec] = []
    for spec in specs:
        configured = spec
        if spec.name in STUB_SCHEMA_TOOLS:
            configured = replace(
                spec,
                schema_mode=ToolSchemaMode.STUB,
                short_description=_STUB_SHORT_DESCRIPTIONS.get(spec.name),
                risk_level=_STUB_RISK_LEVELS.get(spec.name, "low"),
                search_tags=list(spec.search_tags) or [spec.category, spec.name],
            )
        elif spec.name in FULL_SCHEMA_TOOLS:
            configured = replace(spec, schema_mode=ToolSchemaMode.FULL)
        enabled.append(configured)

    registry = ToolRegistry(enabled)
    logger.info(
        f"build_default_registry: 启用 {len(registry)} 个工具 "
        f"(memory_mode={memory_mode}, "
        f"vision={features.vision.enabled}, "
        f"web_search={features.web_search.enabled}, "
        f"weather={features.weather.enabled})"
    )
    return registry
