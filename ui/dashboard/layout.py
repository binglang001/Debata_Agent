"""仪表盘布局规范 —— 给 GPT 实现 PySide6 组件时的尺寸/位置参考。

布局约束都是建议值；具体实现可以微调，但风格必须遵守 docs/ui_style_guide.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ============================================================
# 整体布局
# ============================================================


@dataclass(frozen=True, slots=True)
class LayoutSpec:
    """主窗口布局规格。"""

    min_width: int = 1080
    """最小宽度：低于此会触发 sidebar 收缩"""

    min_height: int = 680

    default_width: int = 1280
    default_height: int = 800

    sidebar_width: int = 200
    """左侧导航宽度"""

    sidebar_collapsed_width: int = 56
    """收缩态宽度（仅图标）"""

    topbar_height: int = 56
    """顶部状态栏高度"""

    content_padding: int = 32
    """主内容区四边内边距"""

    page_max_width: int = 1100
    """主内容区最大宽度（页面再宽也居中）"""


DEFAULT_LAYOUT = LayoutSpec()


# ============================================================
# 左侧导航项定义
# ============================================================


@dataclass(slots=True)
class NavItem:
    """单个导航项。"""

    key: str
    """对应 COPY[f'nav.{key}'] 取标题"""

    icon: str
    """Phosphor icon 名（无前缀，如 'house' / 'chats' / 'note'）"""

    badge_supplier: str | None = None
    """如果该项有徽章（数字/状态），从 NavBadgeSupplier 取。值是 supplier 的 key"""


NAV_ITEMS: list[NavItem] = [
    NavItem(key="overview", icon="house"),
    NavItem(key="chats", icon="chats-circle", badge_supplier="unread_count"),
    NavItem(key="memory", icon="note-pencil"),
    NavItem(key="logs", icon="terminal-window"),
    NavItem(key="personas", icon="user-circle"),
    NavItem(key="plugins", icon="puzzle-piece"),
    NavItem(key="settings", icon="gear-six"),
]


# ============================================================
# 状态徽章映射 —— 适配器/Provider 健康度对应的视觉状态
# ============================================================


STATUS_BADGE_MAP: dict[str, str] = {
    "connected":     "badge-success",  # 绿色徽章
    "connecting":    "badge-warning",  # 黄色徽章
    "disconnected":  "badge-idle",     # 灰色描边徽章
    "error":         "badge-error",    # 红色徽章
    "ok":            "badge-success",
    "warning":       "badge-warning",
    "fail":          "badge-error",
    "idle":          "badge-idle",
}
"""把语义化状态值映射到 ui.theme 里定义的徽章 role。"""


# ============================================================
# 对话列表项尺寸
# ============================================================


@dataclass(frozen=True, slots=True)
class ChatListItemSpec:
    height: int = 64
    avatar_size: int = 40
    avatar_radius: int = 20  # 圆形头像
    padding_h: int = 16
    padding_v: int = 12


CHAT_LIST_ITEM = ChatListItemSpec()


# ============================================================
# 日志行
# ============================================================


@dataclass(frozen=True, slots=True)
class LogRowSpec:
    height: int = 28
    """单行紧凑高度。日志要密集显示"""

    timestamp_width: int = 140
    level_width: int = 60
    module_width: int = 160


LOG_ROW = LogRowSpec()


# ============================================================
# 空状态布局
# ============================================================


@dataclass(frozen=True, slots=True)
class EmptyStateSpec:
    """空状态页统一规格。
    通常是：上方一个简笔图标 + 一句标题 + 一行说明。
    """

    icon_size: int = 48
    icon_color_key: Literal["text_disabled", "border"] = "border"
    """图标用极弱的颜色，不抢戏。"""

    title_to_subtitle_spacing: int = 8
    icon_to_title_spacing: int = 16
    vertical_offset_ratio: float = 0.35
    """空状态整体在父容器中靠上 35% 位置（不要完全居中——视觉上会显空）。"""


EMPTY_STATE = EmptyStateSpec()
