"""Debata_Agent 工具系统。

模块组成：
    base                —— ITool / ToolContext / ToolRegistry / @tool 装饰器
    schemas             —— 全部 Pydantic args 模型
    message_builder     —— 表情包/CQ 码/打字延迟等消息辅助
    messaging           —— send_private / send_group / recall / upload_file
    memory_tools        —— save / delete_important_memory（仅 file 模式）
    platform_tools      —— list_contacts / get_user_info / get_forward_msg /
                           set_*_add_request / summarize_chat_history /
                           summarize_conversation / recall_history
    agent_task_tools    —— start_agent_task 后台子 Agent 资料处理
    control_tools       —— no_action / schedule_wakeup
    feature_tools       —— describe_image / web_search / get_weather
    keyword_save        —— 关键词强制保存联动

公开 API：
    ToolContext         —— 给 AgentRunner 用的执行上下文
    ToolRegistry        —— 工具集合
    build_default_registry(config)
                        —— 按 RootConfig 决定启用哪些工具的工厂
    get_default_specs() —— 全部已注册的工具规格
    try_save_from_user  —— 关键词强制保存
"""

from __future__ import annotations

import logging

# 导入各模块的副作用：注册装饰器把工具加入全局列表
from . import (  # noqa: F401
    agent_task_tools,
    control_tools,
    feature_tools,
    memory_tools,
    messaging,
    platform_tools,
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
    ToolSpec,
    WakeupCallback,
    clear_default_registry,
    get_default_specs,
    tool,
)
from .keyword_save import try_save_from_user
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
    # keyword_save
    "try_save_from_user",
    # factory
    "build_default_registry",
    "MEMORY_FILE_TOOLS",
    "MEMORY_TOOLS",
    "PLATFORM_OPTIONAL_TOOLS",
    "FEATURE_TOOL_FEATURES",
]


# ============================================================
# 工厂：按配置决定启用工具集合
# ============================================================


MEMORY_TOOLS: set[str] = {
    "save_important_memory",
    "delete_important_memory",
}
"""文件长期记忆工具。RAG 模式改用会话向量检索，不暴露这些工具。"""

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


def build_default_registry(
    config,
    *,
    include_upload_file: bool = True,
) -> ToolRegistry:
    """根据 RootConfig 构造启用工具的 Registry。

    规则：
        - messaging 工具（send_*/recall）：始终启用
        - upload_file：默认启用（受 ctx.workspace_dir 进一步限制）
        - memory 工具：仅 file 模式启用；RAG 模式不再使用 important.json
        - platform 工具：始终启用（按需）
        - control 工具：始终启用
        - feature 工具：按 features.{vision,web_search,weather}.enabled

    Args:
        config: RootConfig 实例
        include_upload_file: 是否包含 upload_file（如不需要可关掉）

    Returns:
        ToolRegistry 实例
    """
    specs = list(get_default_specs())
    memory_mode = config.features.long_term_memory.mode
    features = config.features

    enabled: list[ToolSpec] = []
    for spec in specs:
        # 1. feature 工具按 enabled 开关
        if spec.name in FEATURE_TOOL_FEATURES:
            feat_name = FEATURE_TOOL_FEATURES[spec.name]
            feat_cfg = getattr(features, feat_name, None)
            if feat_cfg is None or not feat_cfg.enabled:
                continue

        # 2. upload_file 按调用方意愿
        if spec.name == "upload_file" and not include_upload_file:
            continue

        # 3. RAG 模式使用自动会话向量检索，不暴露重要记忆写入/删除工具
        if memory_mode == "rag" and spec.name in MEMORY_TOOLS:
            continue

        enabled.append(spec)

    registry = ToolRegistry(enabled)
    logger.info(
        f"build_default_registry: 启用 {len(registry)} 个工具 "
        f"(memory_mode={memory_mode}, "
        f"vision={features.vision.enabled}, "
        f"web_search={features.web_search.enabled}, "
        f"weather={features.weather.enabled})"
    )
    return registry
