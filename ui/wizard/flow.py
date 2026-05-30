"""向导步骤流转逻辑 —— 给定当前步骤和上下文，决定下一步去哪。

把流转规则从 UI 代码里剥离，便于测试和调整。

两条主路径：
    推荐路径：WELCOME → MAIN_MODEL_QUICK → FEATURES → EMBEDDING → ADAPTER → PERSONA → SUMMARY
    自定义路径：WELCOME → MAIN_MODEL_CUSTOM → OTHER_AGENTS → FEATURES → EMBEDDING → ADAPTER → PERSONA → SUMMARY

分支点：
    PERSONA → 若选了 create，则插入 PERSONA_CREATE 子流程
"""

from __future__ import annotations

from typing import Any

from .steps import StepId


# ============================================================
# 路径常量
# ============================================================


WIZARD_PATH_RECOMMENDED = "recommended"
WIZARD_PATH_CUSTOM = "custom"


# ============================================================
# 步骤序列（不考虑分支）
# ============================================================


_RECOMMENDED_SEQUENCE = [
    StepId.WELCOME,
    StepId.MAIN_MODEL_QUICK,
    StepId.FEATURES,
    StepId.EMBEDDING,
    StepId.ADAPTER,
    StepId.PERSONA,
    StepId.SUMMARY,
]


_CUSTOM_SEQUENCE = [
    StepId.WELCOME,
    StepId.MAIN_MODEL_CUSTOM,
    StepId.OTHER_AGENTS,
    StepId.FEATURES,
    StepId.EMBEDDING,
    StepId.ADAPTER,
    StepId.PERSONA,
    StepId.SUMMARY,
]


# ============================================================
# 流转函数
# ============================================================


def next_step(
    current: StepId,
    path: str,
    context: dict[str, Any],
) -> StepId | None:
    """根据当前步骤、路径、已填写的上下文，决定下一步。

    返回 None 表示已到末尾（向导完成）。

    Args:
        current: 当前步骤 id
        path: WIZARD_PATH_RECOMMENDED / WIZARD_PATH_CUSTOM
        context: 已填写字段的字典。关键判定字段：
            - long_term_memory_mode: "file" / "rag" → 决定是否插入 EMBEDDING
            - persona_source: "builtin" / "create" / "import" → 决定是否插入 PERSONA_CREATE
    """
    # 分支：PERSONA → 若选 create 则插入 PERSONA_CREATE
    if current == StepId.PERSONA:
        source = context.get("persona_source")
        if source == "create":
            return StepId.PERSONA_CREATE

    # 分支：PERSONA_CREATE 完成后去 SUMMARY
    if current == StepId.PERSONA_CREATE:
        return StepId.SUMMARY

    # 主流：按路径序列推进
    sequence = (
        _RECOMMENDED_SEQUENCE
        if path == WIZARD_PATH_RECOMMENDED
        else _CUSTOM_SEQUENCE
    )

    try:
        idx = sequence.index(current)
    except ValueError:
        # 当前步骤不在该路径序列中（如 PERSONA_CREATE 已上面处理过，不该到这）
        return None

    if idx + 1 >= len(sequence):
        return None

    return sequence[idx + 1]


def prev_step(
    current: StepId,
    path: str,
    context: dict[str, Any],
) -> StepId | None:
    """返回上一步。Welcome 没有上一步。"""
    if current == StepId.WELCOME:
        return None

    if current == StepId.ADAPTER:
        return StepId.EMBEDDING

    if current == StepId.PERSONA_CREATE:
        return StepId.PERSONA

    sequence = (
        _RECOMMENDED_SEQUENCE
        if path == WIZARD_PATH_RECOMMENDED
        else _CUSTOM_SEQUENCE
    )

    try:
        idx = sequence.index(current)
    except ValueError:
        return None

    if idx == 0:
        return None
    return sequence[idx - 1]


def is_last_step(current: StepId) -> bool:
    return current == StepId.SUMMARY


def progress(current: StepId, path: str, context: dict[str, Any]) -> tuple[int, int]:
    """返回 (当前序号, 总数)。用于进度条显示。

    分支步骤（PERSONA_CREATE）参与计数。
    """
    sequence = (
        _RECOMMENDED_SEQUENCE
        if path == WIZARD_PATH_RECOMMENDED
        else _CUSTOM_SEQUENCE
    )
    sequence = list(sequence)

    source = context.get("persona_source")
    if source == "create":
        try:
            persona_idx = sequence.index(StepId.PERSONA)
            sequence.insert(persona_idx + 1, StepId.PERSONA_CREATE)
        except ValueError:
            pass

    total = len(sequence)
    try:
        idx = sequence.index(current) + 1
    except ValueError:
        idx = 0
    return idx, total
