"""向导步骤定义 —— 元数据（id / 标题 / 副标题 / 字段）。

不含 UI 实现。GPT 实现 wizard_window 时按 STEPS 顺序渲染步骤。

每步分两个分支：
    - WIZARD_PATH_RECOMMENDED：推荐路径（5 步，5-10 分钟）
    - WIZARD_PATH_CUSTOM：自定义路径（更多步骤）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StepId(str, Enum):
    """所有可能的步骤 id（推荐 + 自定义两条路径并集）。"""

    WELCOME = "welcome"
    """欢迎页 + 选路径"""

    MAIN_MODEL_QUICK = "main_model_quick"
    """推荐路径：DeepSeek 一键配置"""

    MAIN_MODEL_CUSTOM = "main_model_custom"
    """自定义路径：选提供商 + 模型 + 参数"""

    OTHER_AGENTS = "other_agents"
    """自定义路径：proactive / summary 等子 Agent 配置"""

    FEATURES = "features"
    """可选高级功能（多模态 / 天气 / 联网 / 长期记忆模式）"""

    EMBEDDING = "embedding"
    """RAG 模式下的 embedding 配置（features 选了 rag 时才出现）"""

    ADAPTER = "adapter"
    """渠道配置（NapCat）"""

    PERSONA = "persona"
    """人格选择（内置示范 / 自定义生成 / 导入）"""

    PERSONA_CREATE = "persona_create"
    """人格自定义生成对话框（独立子流程）"""

    SUMMARY = "summary"
    """完成总览 + 测试连接"""


@dataclass(slots=True)
class Step:
    """单步元数据。"""

    id: StepId
    title: str
    """卡片标题（T2 思源宋体，简短）"""

    subtitle: str
    """副标题（说明文字，可一两行）"""

    fields: list[str] = field(default_factory=list)
    """该步骤涉及的配置字段（仅用于参考，UI 实现时按需渲染）"""

    can_skip: bool = False
    """是否允许跳过（如 OTHER_AGENTS 在推荐路径下可跳）"""


# ============================================================
# 步骤注册表
# ============================================================


STEPS: dict[StepId, Step] = {
    StepId.WELCOME: Step(
        id=StepId.WELCOME,
        title="开始之前",
        subtitle=(
            "Diana_Agent 是一个让虚拟角色活过来的通用框架。\n"
            "下面用几分钟配置好——你可以选择「推荐」最快上手，也可以自定义每个细节。"
        ),
    ),
    StepId.MAIN_MODEL_QUICK: Step(
        id=StepId.MAIN_MODEL_QUICK,
        title="选个主模型",
        subtitle=(
            "推荐使用 DeepSeek——中文对话表现好，价格亲民。\n"
            "你需要从官网获取一个 API 密钥（按钮下方有教程）。"
        ),
        fields=["api_key"],
    ),
    StepId.MAIN_MODEL_CUSTOM: Step(
        id=StepId.MAIN_MODEL_CUSTOM,
        title="选个主模型",
        subtitle="你可以选择内置预设，或填入完全自定义的提供商。",
        fields=["provider", "model", "temperature", "top_p", "max_tokens", "reasoning"],
    ),
    StepId.OTHER_AGENTS: Step(
        id=StepId.OTHER_AGENTS,
        title="其它模型",
        subtitle=(
            "Diana_Agent 内部用了三类模型：\n"
            "  · 主聊天（你刚才配的）\n"
            "  · 主动思考（小模型，决定是否主动开口）\n"
            "  · 历史总结（中型模型，定期整理对话）\n"
            "可以分别配置，也可以都用主模型。"
        ),
        fields=["proactive_agent", "summary_agent"],
        can_skip=True,
    ),
    StepId.FEATURES: Step(
        id=StepId.FEATURES,
        title="选些可选的本领",
        subtitle=(
            "这些功能默认关闭。打开哪个，Diana 就拥有哪个能力。\n"
            "现在不开也没关系，之后随时能在设置里打开。"
        ),
        fields=[
            "vision",
            "asr",
            "tts",
            "weather",
            "web_search",
            "long_term_memory_mode",
        ],
    ),
    StepId.EMBEDDING: Step(
        id=StepId.EMBEDDING,
        title="向量记忆",
        subtitle=(
            "你选择了 RAG 模式的长期记忆。这需要一个 embedding 服务来给对话建索引。\n"
            "可以用 API（火山引擎 / GLM），也可以本地跑开源模型。"
        ),
        fields=["embedding_type", "embedding_provider", "embedding_model"],
    ),
    StepId.ADAPTER: Step(
        id=StepId.ADAPTER,
        title="把 NapCat 接上",
        subtitle=(
            "NapCat 是连接 QQ 的中间件。你需要先把它跑起来（参考教程），\n"
            "然后告诉 Diana 怎么找到它。"
        ),
        fields=[
            "adapter_mode",
            "ws_url",
            "access_token",
            "whitelist_mode",
            "whitelist_qq",
            "whitelist_groups",
        ],
    ),
    StepId.PERSONA: Step(
        id=StepId.PERSONA,
        title="赋予一个角色",
        subtitle=(
            "Diana 不是单一角色——你给它什么人格，它就活成谁。\n"
            "可以用内置的示范角色快速开始，或者花十分钟创造一个属于你的。"
        ),
        fields=["persona_source", "active_persona"],
    ),
    StepId.PERSONA_CREATE: Step(
        id=StepId.PERSONA_CREATE,
        title="塑造你的角色",
        subtitle=(
            "回答几个问题，Diana 会和你一起把这个角色具象化。\n"
            "可以多轮调整，直到你满意为止。"
        ),
        fields=[
            "name",
            "personality",
            "background",
            "voice",
            "boundaries",
            "never_say",
            "relation_matrix",
            "sensitive_topics",
            "relation",
        ],
    ),
    StepId.SUMMARY: Step(
        id=StepId.SUMMARY,
        title="就快好了",
        subtitle=(
            "看一眼配置摘要，确认无误后就可以启动了。\n"
            "之后随时能在设置面板里调整。"
        ),
    ),
}
