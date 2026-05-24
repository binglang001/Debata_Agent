"""通用辅助工具。

不与业务逻辑耦合的纯函数集合：
    cq_parser  —— OneBot CQ 码解析（从 raw_message 恢复人类可读文本）
    typing_sim —— 打字延迟模拟（旧 _typing_delay 的迁移；message_builder 也用到）
    timefmt    —— 时间格式化辅助
"""

from .cq_parser import parse_raw_cq
from .timefmt import get_time

__all__ = [
    "parse_raw_cq",
    "get_time",
]
