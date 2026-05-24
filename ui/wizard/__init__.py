"""首次配置向导 —— PySide6 实现。

模块构成（按职责）：
    steps       —— 步骤元数据：每步的 id、标题、副标题、所需字段
    copy        —— 所有用户可见的中文文案（按 key 引用）
    flow        —— 步骤流转逻辑：决定下一步去哪、可不可跳过
    persona_creator —— 人格生成器对话流程（独立子模块）
    components  —— 组件（GPT-TODO 实现：ProviderSelector / ApiKeyInput / 等）
    wizard_window —— 主窗口（GPT-TODO 实现）

Claude 已完成：steps.py / copy.py / flow.py 的文案和流程定义。
GPT 接手：components.py / wizard_window.py 的 PySide6 实现，按 docs/ui_style_guide.md 实现样式。
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
