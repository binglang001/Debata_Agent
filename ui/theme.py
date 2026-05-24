"""主题定义 —— 颜色 / 字体 / 间距常量 + QSS 样式表生成。

所有 PySide6 组件统一从这里取色和样式。新增颜色前先查本文件，避免色值漂移。

GPT-TODO：组件实现时引用 LIGHT / DARK 字典取色，QSS 用 build_qss(theme) 生成。
完整设计原则见 docs/ui_style_guide.md。
"""

from __future__ import annotations

from dataclasses import dataclass


# ============================================================
# 色板
# ============================================================


@dataclass(frozen=True, slots=True)
class Palette:
    """单个主题的完整色板。"""

    # 背景层级
    bg_primary: str       # 背景主色
    bg_card: str          # 卡片 / 面板
    bg_hover: str         # 悬停浅填充
    border: str           # 分隔线 / 极轻边框

    # 文字层级
    text_primary: str     # 主要文字
    text_secondary: str   # 次要文字
    text_disabled: str    # 占位 / 禁用

    # 点缀色（两套主题共用，但下面也单独列出供调用）
    accent_primary: str   # 青瓷青
    accent_blue: str      # 汝窑蓝
    error: str            # 朱砂红
    warning: str          # 栀子黄
    success: str          # 苔色绿


LIGHT = Palette(
    bg_primary="#F8F5EE",
    bg_card="#FCFAF4",
    bg_hover="#F0E8D6",
    border="#E8E1CE",
    text_primary="#2B2622",
    text_secondary="#6B635A",
    text_disabled="#B5AC9F",
    accent_primary="#6FA39A",
    accent_blue="#5A7A99",
    error="#C0584F",
    warning="#D9A85F",
    success="#7A9B6E",
)


DARK = Palette(
    bg_primary="#1A1817",
    bg_card="#232120",
    bg_hover="#2E2B28",
    border="#3A3633",
    text_primary="#E8E2D4",
    text_secondary="#9B9286",
    text_disabled="#5C544A",
    accent_primary="#6FA39A",
    accent_blue="#5A7A99",
    error="#C0584F",
    warning="#D9A85F",
    success="#7A9B6E",
)


# ============================================================
# 间距 / 圆角 / 字号
# ============================================================


class Spacing:
    XS = 4
    SM = 8
    MD = 16
    LG = 24
    XL = 40
    XXL = 64


class Radius:
    SMALL = 4
    DEFAULT = 8
    LARGE = 12
    PILL = 999


class FontSize:
    T1 = 28
    T2 = 20
    T3 = 16
    BODY = 14
    SECONDARY = 13
    SMALL = 12
    CODE = 13


# ============================================================
# 字体族（按优先级降序）
# ============================================================


SERIF_FAMILIES = (
    "Source Han Serif CN",
    "Noto Serif CJK SC",
    "STSong",
    "宋体",
    "serif",
)
"""标题 / 装饰字体（思源宋体优先）。"""


SANS_FAMILIES = (
    "Source Han Sans CN",
    "Noto Sans CJK SC",
    "PingFang SC",
    "Microsoft YaHei UI",
    "微软雅黑",
    "Inter",
    "sans-serif",
)
"""正文 / UI 字体（思源黑体优先）。"""


MONO_FAMILIES = (
    "Sarasa Mono SC",
    "JetBrains Mono",
    "Consolas",
    "Courier New",
    "monospace",
)


def font_family(family: tuple[str, ...]) -> str:
    """构造 QSS 用的字体族字符串。"""
    return ", ".join(f'"{f}"' if " " in f else f for f in family)


# ============================================================
# QSS 生成器
# ============================================================


def build_qss(palette: Palette) -> str:
    """根据色板生成完整应用样式表。

    GPT-TODO：组件实现时给 QApplication.setStyleSheet(build_qss(LIGHT)) 即可。
    切换主题：再次调用并 setStyleSheet 即可。
    """
    p = palette
    sans = font_family(SANS_FAMILIES)

    return f"""
/* ===== 全局 ===== */
QWidget {{
    background-color: {p.bg_primary};
    color: {p.text_primary};
    font-family: {sans};
    font-size: {FontSize.BODY}px;
    letter-spacing: 0.3px;
}}

/* ===== 卡片 / 面板 ===== */
QFrame#Card {{
    background-color: {p.bg_card};
    border-radius: {Radius.DEFAULT}px;
    padding: {Spacing.LG}px;
}}

/* ===== 主按钮 ===== */
QPushButton[role="primary"] {{
    background-color: {p.accent_primary};
    color: white;
    border: none;
    border-radius: {Radius.DEFAULT}px;
    padding: 8px 20px;
    font-size: {FontSize.BODY}px;
    font-weight: 500;
    min-height: 36px;
}}
QPushButton[role="primary"]:hover {{
    background-color: {_darken(p.accent_primary, 0.08)};
}}
QPushButton[role="primary"]:pressed {{
    background-color: {_darken(p.accent_primary, 0.12)};
}}
QPushButton[role="primary"]:disabled {{
    background-color: {p.text_disabled};
    color: {p.bg_primary};
}}

/* ===== 次按钮 ===== */
QPushButton[role="secondary"] {{
    background-color: transparent;
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.DEFAULT}px;
    padding: 8px 20px;
    min-height: 36px;
}}
QPushButton[role="secondary"]:hover {{
    background-color: {p.bg_hover};
}}

/* ===== 文字按钮 ===== */
QPushButton[role="text"] {{
    background-color: transparent;
    color: {p.accent_primary};
    border: none;
    padding: 4px 8px;
}}
QPushButton[role="text"]:hover {{
    text-decoration: underline;
}}

/* ===== 危险按钮 ===== */
QPushButton[role="danger"] {{
    background-color: {p.error};
    color: white;
    border: none;
    border-radius: {Radius.DEFAULT}px;
    padding: 8px 20px;
    min-height: 36px;
}}
QPushButton[role="danger"]:hover {{
    background-color: {_darken(p.error, 0.08)};
}}

/* ===== 输入框 ===== */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {p.bg_card};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 0 12px;
    min-height: 36px;
    selection-background-color: {p.accent_primary};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {p.accent_primary};
}}
QLineEdit[state="error"], QTextEdit[state="error"] {{
    border-color: {p.error};
}}

/* ===== 标签 ===== */
QLabel[role="title-1"] {{
    font-family: {font_family(SERIF_FAMILIES)};
    font-size: {FontSize.T1}px;
    font-weight: 500;
    color: {p.text_primary};
}}
QLabel[role="title-2"] {{
    font-family: {font_family(SERIF_FAMILIES)};
    font-size: {FontSize.T2}px;
    font-weight: 500;
    color: {p.text_primary};
}}
QLabel[role="title-3"] {{
    font-size: {FontSize.T3}px;
    font-weight: 600;
    color: {p.text_primary};
}}
QLabel[role="secondary"] {{
    font-size: {FontSize.SECONDARY}px;
    color: {p.text_secondary};
}}
QLabel[role="caption"] {{
    font-size: {FontSize.SMALL}px;
    color: {p.text_disabled};
}}

/* ===== 列表 ===== */
QListWidget {{
    background-color: {p.bg_primary};
    border: none;
    outline: none;
}}
QListWidget::item {{
    padding: 12px 16px;
    border-radius: {Radius.SMALL}px;
}}
QListWidget::item:hover {{
    background-color: {p.bg_hover};
}}
QListWidget::item:selected {{
    background-color: {p.bg_hover};
    color: {p.text_primary};
    border-left: 3px solid {p.accent_primary};
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.text_disabled};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

/* ===== 状态徽章 ===== */
QLabel[role="badge-success"] {{
    background-color: {p.success};
    color: white;
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
}}
QLabel[role="badge-warning"] {{
    background-color: {p.warning};
    color: white;
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
}}
QLabel[role="badge-error"] {{
    background-color: {p.error};
    color: white;
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
}}
QLabel[role="badge-idle"] {{
    background-color: transparent;
    color: {p.text_secondary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
}}

/* ===== 分隔线 ===== */
QFrame[role="separator"] {{
    background-color: {p.border};
    max-height: 1px;
    min-height: 1px;
}}

/* ===== 弹窗 ===== */
QDialog {{
    background-color: {p.bg_card};
    border-radius: {Radius.LARGE}px;
}}

/* ===== Tab ===== */
QTabBar::tab {{
    background: transparent;
    color: {p.text_secondary};
    padding: 8px 16px;
    border: none;
}}
QTabBar::tab:selected {{
    color: {p.text_primary};
    border-bottom: 2px solid {p.accent_primary};
}}

/* ===== Tooltip ===== */
QToolTip {{
    background-color: {p.text_primary};
    color: {p.bg_primary};
    border: none;
    border-radius: {Radius.SMALL}px;
    padding: 6px 10px;
}}
"""


# ============================================================
# 颜色微调辅助
# ============================================================


def _darken(hex_color: str, amount: float) -> str:
    """把 #RRGGBB 颜色变暗 amount（0~1）。"""
    color = hex_color.lstrip("#")
    if len(color) != 6:
        return hex_color
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    factor = 1.0 - amount
    r, g, b = (max(0, min(255, int(c * factor))) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def _lighten(hex_color: str, amount: float) -> str:
    """把 #RRGGBB 颜色变亮 amount（0~1）。"""
    color = hex_color.lstrip("#")
    if len(color) != 6:
        return hex_color
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    r, g, b = (
        max(0, min(255, int(c + (255 - c) * amount))) for c in (r, g, b)
    )
    return f"#{r:02X}{g:02X}{b:02X}"


# ============================================================
# 默认导出
# ============================================================


__all__ = [
    "Palette",
    "LIGHT",
    "DARK",
    "Spacing",
    "Radius",
    "FontSize",
    "SERIF_FAMILIES",
    "SANS_FAMILIES",
    "MONO_FAMILIES",
    "font_family",
    "build_qss",
]
