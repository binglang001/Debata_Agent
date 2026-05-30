"""首次配置向导 —— PySide6 实现。

模块构成（按职责）：
    steps       —— 步骤元数据：每步的 id、标题、副标题、所需字段
    copy        —— 所有用户可见的中文文案（按 key 引用）
    flow        —— 步骤流转逻辑：决定下一步去哪、可不可跳过
    persona_creator —— 人格生成器对话流程（独立子模块）
    components  —— 组件（ProviderSelector / ApiKeyInput / 等）
    window      —— 主窗口

各模块按 docs/ui_style_guide.md 的风格约束实现。
"""

from .copy import COPY
from .flow import (
    WIZARD_PATH_CUSTOM,
    WIZARD_PATH_RECOMMENDED,
    is_last_step,
    next_step,
    prev_step,
    progress,
)
from .steps import STEPS, Step, StepId

__all__ = [
    "COPY",
    "STEPS",
    "Step",
    "StepId",
    "WIZARD_PATH_CUSTOM",
    "WIZARD_PATH_RECOMMENDED",
    "next_step",
    "prev_step",
    "is_last_step",
    "progress",
]
