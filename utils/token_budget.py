"""Token 预算估算工具。

热路径只做本地估算，不调用模型。优先使用 tiktoken；不可用时退回粗略字符估算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(slots=True)
class TokenBudget:
    max_context_tokens: int
    reserve_output_tokens: int
    memory_token_budget: int
    summary_token_budget: int

    @property
    def total_input_budget(self) -> int:
        return max(1024, self.max_context_tokens - self.reserve_output_tokens)


class TokenEstimator:
    """带简单校准系数的 token 估算器。"""

    def __init__(self, model: str = "", calib_ratio: float = 1.0) -> None:
        self.model = model
        self.calib_ratio = calib_ratio

    def estimate_text(self, text: str) -> int:
        raw = _estimate_text_raw(text, self.model)
        return max(1, int(raw * self.calib_ratio))

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for message in _normalize_messages_for_estimate(messages):
            total += 4
            for value in message.values():
                total += self.estimate_text(_field_text_for_estimate(value))
        return max(1, total)

    def update_calibration(self, estimated: int, actual_prompt_tokens: int) -> None:
        if estimated <= 0 or actual_prompt_tokens <= 0:
            return
        ratio = actual_prompt_tokens / estimated
        self.calib_ratio = self.calib_ratio * 0.8 + ratio * 0.2


def _estimate_text_raw(text: str, model: str = "") -> int:
    if not text:
        return 1
    try:
        enc = _encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        # 中文约 1.5 字/token，英文约 4 chars/token；用保守混合估算。
        return max(1, len(text) // 2)


def warm_token_estimator(model: str = "") -> None:
    """预热并缓存 tokenizer，避免首条真实消息触发同步加载。"""
    _encoding_for_model(model)


@lru_cache(maxsize=32)
def _encoding_for_model(model: str = ""):
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("o200k_base")
    except Exception:
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception:
            return tiktoken.get_encoding("cl100k_base")


def _normalize_messages_for_estimate(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 provider 默认发送口径规整消息，避免把本地存储字段计入预算。"""
    from providers.base import normalize_messages

    return normalize_messages(messages)


def _field_text_for_estimate(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)
