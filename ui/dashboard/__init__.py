"""仪表盘 —— Debata 运行时状态可视化。

模块构成：
    copy            —— 所有用户可见文案（中文，集中放）
    layout          —— 整体布局与导航结构（Claude 已定义）
    main_window     —— 主窗口（GPT-TODO PySide6 实现）
    status_panel    —— 状态总览面板（GPT-TODO）
    chat_visualizer —— 实时对话可视化（GPT-TODO）
    log_viewer      —— 结构化日志查看器（GPT-TODO）

Claude 完成：copy.py / layout.py
GPT 接手：各个 PySide6 组件，按 docs/ui_style_guide.md 实现。
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
