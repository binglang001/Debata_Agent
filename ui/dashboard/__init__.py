"""仪表盘 —— Debata 运行时状态可视化。

模块构成：
    copy            —— 所有用户可见文案（中文，集中放）
    layout          —— 整体布局与导航结构
    main_window     —— 主窗口
    overview_page   —— 状态总览页
    chats_page      —— 实时对话页
    logs_page       —— 结构化日志页

copy.py / layout.py 提供文案与布局规格，各页面模块实现具体 PySide6 组件。
"""

from .copy import DASHBOARD_COPY
from .layout import (
    CHAT_LIST_ITEM,
    DEFAULT_LAYOUT,
    EMPTY_STATE,
    LOG_ROW,
    NAV_ITEMS,
    STATUS_BADGE_MAP,
    ChatListItemSpec,
    EmptyStateSpec,
    LayoutSpec,
    LogRowSpec,
    NavItem,
)

__all__ = [
    "DASHBOARD_COPY",
    "NAV_ITEMS",
    "STATUS_BADGE_MAP",
    "LayoutSpec",
    "NavItem",
    "DEFAULT_LAYOUT",
    "CHAT_LIST_ITEM",
    "LOG_ROW",
    "EMPTY_STATE",
    "ChatListItemSpec",
    "LogRowSpec",
    "EmptyStateSpec",
]
