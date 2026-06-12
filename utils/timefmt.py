"""时间格式化辅助。从旧 debata_agent/utils.py 迁移。"""

from __future__ import annotations

from datetime import datetime


def get_time(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """返回当前本地时间的字符串。"""
    return datetime.now().strftime(fmt)
