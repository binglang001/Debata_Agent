"""主题定义 —— 颜色 / 字体 / 间距常量 + QSS 样式表生成。

所有 PySide6 组件统一从这里取色和样式。新增颜色前先查本文件，避免色值漂移。

设计原则见 docs/ui_style_guide.md：
    - 每个交互元素必须有 hover / focus / pressed / disabled 4 态
    - 文字按钮 hover 时左侧出现 2px 青瓷青竖条
    - 留白即设计，宁可少放一个元素
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ============================================================
# 色板
# ============================================================


@dataclass(frozen=True, slots=True)
class Palette:
    """单个主题的完整色板。"""

    name: str             # "light" / "dark"

    bg_primary: str       # 背景主色
    bg_card: str          # 卡片 / 面板
    bg_hover: str         # 悬停浅填充
    border: str           # 分隔线 / 极轻边框

    text_primary: str     # 主要文字
    text_secondary: str   # 次要文字
    text_disabled: str    # 占位 / 禁用

    accent_primary: str   # 青瓷青
    accent_blue: str      # 汝窑蓝
    error: str            # 朱砂红
    warning: str          # 栀子黄
    success: str          # 苔色绿


LIGHT = Palette(
    name="light",
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
    name="dark",
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


SANS_FAMILIES = (
    "Source Han Sans CN",
    "Noto Sans CJK SC",
    "PingFang SC",
    "Microsoft YaHei UI",
    "微软雅黑",
    "Inter",
    "sans-serif",
)


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

    每个交互元素都有 hover / focus / pressed / disabled 4 态。
    用法：
        app.setStyleSheet(build_qss(LIGHT))
    """
    p = palette
    sans = font_family(SANS_FAMILIES)
    serif = font_family(SERIF_FAMILIES)
    mono = font_family(MONO_FAMILIES)

    # 颜色派生
    hover_primary = _darken(p.accent_primary, 0.08)
    pressed_primary = _darken(p.accent_primary, 0.16)
    hover_error = _darken(p.error, 0.08)
    pressed_error = _darken(p.error, 0.16)
    selected_fill = _rgba(p.accent_primary, 0.10)

    return f"""
/* ============================================================
 * 全局基线
 * ============================================================ */
QWidget {{
    background-color: {p.bg_primary};
    color: {p.text_primary};
    font-family: {sans};
    font-size: {FontSize.BODY}px;
}}

/* QMainWindow / QDialog 走 WA_TranslucentBackground，自身不绘背景，由内部 WindowFrame 接管。
 * 注意：仍给 QSS 一个透明 fallback，防止某些控件意外画白底。 */
QMainWindow, QDialog {{
    background-color: {p.bg_primary};
}}
QMainWindow[frameless="true"], QDialog[frameless="true"] {{
    background-color: transparent;
}}

/* ============================================================
 * 外层窗口框：frameless 窗口的圆角与背景都由它承担
 * ============================================================ */
QFrame#WindowFrame {{
    background-color: {p.bg_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.LARGE}px;
}}

/* ============================================================
 * 卡片 / 面板
 * ============================================================ */
QFrame#Card {{
    background-color: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: {Radius.DEFAULT}px;
}}

QFrame#SectionCard {{
    background-color: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: {Radius.DEFAULT}px;
}}

QFrame#SectionCard[selected="true"] {{
    border: 2px solid {p.accent_primary};
}}

QFrame#EmojiTile {{
    background-color: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
}}

QLabel#EmojiDropHint {{
    border: 2px dashed {p.text_disabled};
    border-radius: {Radius.DEFAULT}px;
    padding: {Spacing.LG}px;
}}

QLabel#EmojiPreview {{
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
}}

QFrame#Sidebar {{
    background-color: {p.bg_card};
    border-right: 1px solid {p.border};
}}

QFrame#Topbar {{
    background-color: {p.bg_primary};
    border-bottom: 1px solid {p.border};
}}

QFrame[role="separator"] {{
    background-color: {p.border};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}

QFrame[role="separator-v"] {{
    background-color: {p.border};
    max-width: 1px;
    min-width: 1px;
    border: none;
}}

/* ============================================================
 * 主按钮（青瓷青填充）
 * ============================================================ */
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
    background-color: {hover_primary};
}}
QPushButton[role="primary"]:pressed {{
    background-color: {pressed_primary};
}}
QPushButton[role="primary"]:focus {{
    border: 2px solid {_lighten(p.accent_primary, 0.22)};
    outline: none;
}}
QPushButton[role="primary"]:disabled {{
    background-color: {p.text_disabled};
    color: {p.bg_card};
}}

/* ============================================================
 * 次按钮（透明 + 边框）
 * ============================================================ */
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
    border-color: {p.text_secondary};
}}
QPushButton[role="secondary"]:pressed {{
    background-color: {_darken(p.bg_hover, 0.05)};
}}
QPushButton[role="secondary"]:focus {{
    border-color: {p.accent_primary};
    outline: none;
}}
QPushButton[role="secondary"]:disabled {{
    color: {p.text_disabled};
    border-color: {p.border};
    background-color: transparent;
}}

/* ============================================================
 * 文字按钮（无边框 + hover 时左侧竖条）
 * ============================================================ */
QPushButton[role="text"] {{
    background-color: transparent;
    color: {p.accent_primary};
    border: none;
    border-left: 2px solid transparent;
    border-radius: 0px;
    padding: 4px 8px 4px 10px;
    text-align: left;
    min-height: 28px;
}}
QPushButton[role="text"]:hover {{
    color: {hover_primary};
    border-left: 2px solid {p.accent_primary};
    background-color: {selected_fill};
}}
QPushButton[role="text"]:pressed {{
    color: {pressed_primary};
    background-color: {p.bg_hover};
}}
QPushButton[role="text"]:focus {{
    border-left: 2px solid {p.accent_primary};
    outline: none;
}}
QPushButton[role="text"]:disabled {{
    color: {p.text_disabled};
    border-left-color: transparent;
    background-color: transparent;
}}

QPushButton[role="ghost"] {{
    background-color: transparent;
    color: {p.text_secondary};
    border: none;
    border-radius: {Radius.SMALL}px;
    padding: 4px 8px;
    min-height: 28px;
}}
QPushButton[role="ghost"]:hover {{
    background-color: {selected_fill};
    color: {p.text_primary};
}}
QPushButton[role="ghost"]:pressed {{
    background-color: {p.bg_hover};
    color: {p.accent_primary};
}}
QPushButton[role="ghost"]:focus {{
    border: 1px solid {p.accent_primary};
    outline: none;
}}
QPushButton[role="ghost"]:disabled {{
    color: {p.text_disabled};
    background-color: transparent;
}}

/* ============================================================
 * 危险按钮（朱砂红填充，仅用于不可逆操作）
 * ============================================================ */
QPushButton[role="danger"] {{
    background-color: {p.error};
    color: white;
    border: none;
    border-radius: {Radius.DEFAULT}px;
    padding: 8px 20px;
    min-height: 36px;
    font-weight: 500;
}}
QPushButton[role="danger"]:hover {{
    background-color: {hover_error};
}}
QPushButton[role="danger"]:pressed {{
    background-color: {pressed_error};
}}
QPushButton[role="danger"]:focus {{
    border: 2px solid {_lighten(p.error, 0.22)};
    outline: none;
}}
QPushButton[role="danger"]:disabled {{
    background-color: {p.text_disabled};
    color: {p.bg_card};
}}

/* ============================================================
 * 工具/图标按钮（顶栏切主题、刷新等）
 * ============================================================ */
QPushButton[role="icon"] {{
    background-color: transparent;
    color: {p.text_secondary};
    border: none;
    border-radius: {Radius.SMALL}px;
    padding: 6px;
    min-width: 28px;
    min-height: 28px;
}}
QPushButton[role="icon"]:hover {{
    background-color: {p.bg_hover};
    color: {p.text_primary};
}}
QPushButton[role="icon"]:pressed {{
    background-color: {_darken(p.bg_hover, 0.06)};
}}
QPushButton[role="icon"]:focus {{
    border: 1px solid {p.accent_primary};
    outline: none;
}}
QPushButton[role="icon"]:disabled {{
    color: {p.text_disabled};
}}

/* ============================================================
 * 兜底按钮（未指定 role 的 QPushButton 不至于裸出系统样式）
 * ============================================================ */
QPushButton {{
    background-color: transparent;
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.DEFAULT}px;
    padding: 6px 16px;
    min-height: 32px;
}}
QPushButton:hover {{
    background-color: {p.bg_hover};
}}
QPushButton:pressed {{
    background-color: {_darken(p.bg_hover, 0.05)};
}}
QPushButton:focus {{
    border-color: {p.accent_primary};
    outline: none;
}}
QPushButton:disabled {{
    color: {p.text_disabled};
}}

/* ============================================================
 * 窗口控制按钮（无边框模式下的 min/max/close）
 * ============================================================ */
QPushButton[role="win"] {{
    background-color: transparent;
    color: {p.text_secondary};
    border: none;
    border-radius: 0px;
    font-size: 14px;
    padding: 0;
}}
QPushButton[role="win"]:hover {{
    background-color: {p.bg_hover};
    color: {p.text_primary};
}}
QPushButton[role="win"]:pressed {{
    background-color: {_darken(p.bg_hover, 0.05)};
}}
QPushButton[role="win"]:focus {{
    border: 1px solid {p.accent_primary};
    outline: none;
}}

QPushButton[role="win-close"] {{
    background-color: transparent;
    color: {p.text_secondary};
    border: none;
    border-radius: 0px;
    font-size: 14px;
    padding: 0;
}}
QPushButton[role="win-close"]:hover {{
    background-color: {p.error};
    color: white;
}}
QPushButton[role="win-close"]:pressed {{
    background-color: {_darken(p.error, 0.1)};
    color: white;
}}
QPushButton[role="win-close"]:focus {{
    border: 1px solid {p.error};
    outline: none;
}}

QFrame#DialogTitleBar {{
    background-color: {p.bg_card};
    border-bottom: 1px solid {p.border};
}}

/* ============================================================
 * 模型安装指引浮窗
 * ============================================================ */
QFrame#ModelGuideHero {{
    background-color: {p.bg_card};
    border: 1px solid {p.border};
    border-left: 4px solid {p.accent_primary};
    border-radius: {Radius.SMALL}px;
}}

QTextBrowser#ModelGuideBrowser {{
    background-color: {p.bg_card};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 14px;
    selection-background-color: {p.accent_primary};
    selection-color: white;
}}
QTextBrowser#ModelGuideBrowser:focus {{
    border-color: {p.accent_primary};
}}
QTextBrowser#ModelGuideBrowser QWidget {{
    background-color: {p.bg_card};
}}

QTextBrowser#TutorialBrowser {{
    background-color: {p.bg_card};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 14px;
    selection-background-color: {p.accent_primary};
    selection-color: white;
}}
QTextBrowser#TutorialBrowser:focus {{
    border-color: {p.accent_primary};
}}
QTextBrowser#TutorialBrowser QWidget {{
    background-color: {p.bg_card};
}}

QFrame#ModelGuideDependencyPanel {{
    background-color: {_rgba(p.accent_blue, 0.08)};
    border: 1px solid {_rgba(p.accent_blue, 0.28)};
    border-radius: {Radius.SMALL}px;
}}

/* ============================================================
 * 侧边导航项
 * ============================================================ */
QPushButton[role="nav"] {{
    background-color: transparent;
    color: {p.text_secondary};
    border: none;
    border-left: 3px solid transparent;
    border-radius: 0px;
    padding: 10px 16px 10px 14px;
    text-align: left;
    font-size: {FontSize.BODY}px;
    min-height: 40px;
}}
QPushButton[role="nav"]:hover {{
    background-color: {p.bg_hover};
    color: {p.text_primary};
}}
QPushButton[role="nav"]:pressed {{
    background-color: {_darken(p.bg_hover, 0.05)};
}}
QPushButton[role="nav"]:focus {{
    border-left-color: {p.accent_primary};
    outline: none;
}}
QPushButton[role="nav"][active="true"] {{
    background-color: {selected_fill};
    color: {p.accent_primary};
    border-left: 3px solid {p.accent_primary};
    font-weight: 500;
}}
QPushButton[role="nav"][active="true"]:hover {{
    background-color: {_rgba(p.accent_primary, 0.14)};
}}

/* ============================================================
 * 输入框
 * ============================================================ */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {p.bg_card};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 6px 12px;
    min-height: 24px;
    selection-background-color: {p.accent_primary};
    selection-color: white;
}}
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover {{
    border-color: {p.text_secondary};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {p.accent_primary};
    outline: none;
}}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {{
    background-color: {p.bg_primary};
    color: {p.text_disabled};
    border-color: {p.border};
}}
QLineEdit[state="error"], QTextEdit[state="error"], QPlainTextEdit[state="error"] {{
    border-color: {p.error};
}}
QLineEdit[state="success"] {{
    border-color: {p.success};
}}

/* PlainTextEdit / TextEdit 多行高度自然，不强制 min-height */
QTextEdit, QPlainTextEdit {{
    min-height: 80px;
}}

QLineEdit[role="search"] {{
    padding-left: 32px;
    background-image: none;
    background-color: {p.bg_card};
}}

/* ============================================================
 * ComboBox
 * ============================================================ */
QComboBox {{
    background-color: {p.bg_card};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 6px 12px;
    min-height: 24px;
}}
QComboBox:hover {{
    border-color: {p.text_secondary};
}}
QComboBox:focus {{
    border-color: {p.accent_primary};
}}
QComboBox:disabled {{
    background-color: {p.bg_primary};
    color: {p.text_disabled};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
    subcontrol-origin: padding;
    subcontrol-position: right center;
}}
QComboBox::down-arrow {{
    image: none;
    width: 8px;
    height: 8px;
    border-left: 1px solid {p.text_secondary};
    border-bottom: 1px solid {p.text_secondary};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {p.bg_card};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    selection-background-color: {selected_fill};
    selection-color: {p.text_primary};
    padding: 4px;
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    padding: 6px 10px;
    border-radius: {Radius.SMALL}px;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {p.bg_hover};
}}

/* ============================================================
 * SpinBox / DoubleSpinBox
 * ============================================================ */
QSpinBox, QDoubleSpinBox {{
    background-color: {p.bg_card};
    color: {p.text_primary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 6px 8px;
    min-height: 24px;
}}
QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {p.text_secondary};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {p.accent_primary};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 16px;
}}

/* ============================================================
 * CheckBox / RadioButton
 * ============================================================ */
QCheckBox, QRadioButton {{
    color: {p.text_primary};
    spacing: 8px;
    padding: 4px 0;
    background: transparent;
}}
QCheckBox:hover, QRadioButton:hover {{
    color: {p.accent_primary};
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: {p.text_disabled};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {p.border};
    background-color: {p.bg_card};
}}
QCheckBox::indicator {{
    border-radius: 3px;
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.accent_primary};
}}
QCheckBox::indicator:checked {{
    background-color: {p.accent_primary};
    border-color: {p.accent_primary};
    image: none;
}}
QRadioButton::indicator:checked {{
    background-color: {p.bg_card};
    border: 5px solid {p.accent_primary};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background-color: {p.bg_primary};
    border-color: {p.text_disabled};
}}

/* ============================================================
 * 标签文字层级
 * ============================================================ */
QLabel {{
    background: transparent;
    color: {p.text_primary};
}}
QLabel[role="title-1"] {{
    font-family: {serif};
    font-size: {FontSize.T1}px;
    font-weight: 500;
    color: {p.text_primary};
}}
QLabel[role="title-2"] {{
    font-family: {serif};
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
QLabel[role="small"] {{
    font-size: {FontSize.SMALL}px;
    color: {p.text_secondary};
}}
QLabel[role="caption"] {{
    font-size: {FontSize.SMALL}px;
    color: {p.text_disabled};
}}
QLabel[role="link"] {{
    color: {p.accent_primary};
}}
QLabel[role="error"] {{
    color: {p.error};
    font-size: {FontSize.SECONDARY}px;
}}
QLabel[role="success"] {{
    color: {p.success};
    font-size: {FontSize.SECONDARY}px;
}}
QLabel[role="warning"] {{
    color: {p.warning};
    font-size: {FontSize.SECONDARY}px;
}}
QLabel[role="mono"] {{
    font-family: {mono};
    font-size: {FontSize.CODE}px;
    color: {p.text_primary};
}}

QFrame[role="muted-block"] {{
    background-color: {p.bg_hover};
    border: 1px solid {p.border};
    border-radius: {Radius.DEFAULT}px;
}}

/* ============================================================
 * 列表（聊天列表 / 角色列表 / memory 列表）
 * ============================================================ */
QListWidget, QListView {{
    background-color: {p.bg_primary};
    border: none;
    outline: none;
    padding: 4px;
}}
QListWidget::item, QListView::item {{
    padding: 12px 16px;
    border-radius: {Radius.SMALL}px;
    color: {p.text_primary};
    border-left: 3px solid transparent;
}}
QListWidget::item:hover, QListView::item:hover {{
    background-color: {p.bg_hover};
}}
QListWidget::item:selected, QListView::item:selected {{
    background-color: {selected_fill};
    color: {p.text_primary};
    border-left: 3px solid {p.accent_primary};
}}
QListWidget::item:focus, QListView::item:focus {{
    border-left: 3px solid {p.accent_primary};
    background-color: {_rgba(p.accent_primary, 0.06)};
    outline: none;
}}

/* ============================================================
 * QStackedWidget（页面切换容器）
 * ============================================================ */
QStackedWidget {{
    background-color: {p.bg_primary};
}}

/* ============================================================
 * 滚动条
 * ============================================================ */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 5px;
    min-height: 30px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.text_disabled};
}}
QScrollBar::handle:vertical:pressed {{
    background: {p.text_secondary};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    background: transparent;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {p.border};
    border-radius: 5px;
    min-width: 30px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p.text_disabled};
}}
QScrollBar::handle:horizontal:pressed {{
    background: {p.text_secondary};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
    background: transparent;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

/* ============================================================
 * 状态徽章
 * ============================================================ */
QLabel[role="badge-success"] {{
    background-color: {p.success};
    color: white;
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
    font-weight: 500;
}}
QLabel[role="badge-warning"] {{
    background-color: {p.warning};
    color: white;
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
    font-weight: 500;
}}
QLabel[role="badge-error"] {{
    background-color: {p.error};
    color: white;
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
    font-weight: 500;
}}
QLabel[role="badge-info"] {{
    background-color: {p.accent_blue};
    color: white;
    border-radius: {Radius.SMALL}px;
    padding: 2px 8px;
    font-size: {FontSize.SMALL}px;
    font-weight: 500;
}}
QLabel[role="badge-idle"] {{
    background-color: transparent;
    color: {p.text_secondary};
    border: 1px solid {p.border};
    border-radius: {Radius.SMALL}px;
    padding: 1px 7px;
    font-size: {FontSize.SMALL}px;
}}

/* ============================================================
 * Tab
 * ============================================================ */
QTabBar::tab {{
    background: transparent;
    color: {p.text_secondary};
    padding: 8px 16px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{
    color: {p.text_primary};
    background: {p.bg_hover};
}}
QTabBar::tab:selected {{
    color: {p.text_primary};
    border-bottom: 2px solid {p.accent_primary};
}}
QTabWidget::pane {{
    border: none;
    background: transparent;
}}

/* ============================================================
 * Tooltip
 * ============================================================ */
QToolTip {{
    background-color: {p.text_primary};
    color: {p.bg_primary};
    border: none;
    border-radius: {Radius.SMALL}px;
    padding: 6px 10px;
    font-size: {FontSize.SMALL}px;
}}

/* ============================================================
 * 分组框
 * ============================================================ */
QGroupBox {{
    background: transparent;
    border: 1px solid {p.border};
    border-radius: {Radius.DEFAULT}px;
    margin-top: 16px;
    padding: 16px;
    font-weight: 500;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    background-color: {p.bg_primary};
    color: {p.text_secondary};
}}

/* ============================================================
 * Slider
 * ============================================================ */
QSlider::groove:horizontal {{
    border: none;
    height: 4px;
    background: {p.border};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {p.accent_primary};
    border: none;
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {hover_primary};
}}
QSlider::sub-page:horizontal {{
    background: {p.accent_primary};
    border-radius: 2px;
}}

/* ============================================================
 * Menu（托盘右键菜单等）
 * ============================================================ */
QMenu {{
    background-color: {p.bg_card};
    border: 1px solid {p.border};
    border-radius: {Radius.DEFAULT}px;
    padding: 4px;
    color: {p.text_primary};
}}
QMenu::item {{
    padding: 8px 20px;
    border-radius: {Radius.SMALL}px;
    background: transparent;
}}
QMenu::item:selected {{
    background-color: {selected_fill};
    color: {p.accent_primary};
}}
QMenu::item:disabled {{
    color: {p.text_disabled};
}}
QMenu::separator {{
    height: 1px;
    background: {p.border};
    margin: 4px 8px;
}}

/* ============================================================
 * 进度条
 * ============================================================ */
QProgressBar {{
    background-color: {p.border};
    border: none;
    border-radius: 2px;
    text-align: center;
    height: 4px;
}}
QProgressBar::chunk {{
    background-color: {p.accent_primary};
    border-radius: 2px;
}}

QSizeGrip#WindowSizeGrip {{
    background: transparent;
}}

/* ============================================================
 * Splitter
 * ============================================================ */
QSplitter::handle {{
    background-color: {p.border};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QSplitter::handle:vertical {{
    height: 1px;
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


def _rgba(hex_color: str, alpha: float) -> str:
    """把 #RRGGBB 转成 rgba(r, g, b, a) 字符串。"""
    color = hex_color.lstrip("#")
    if len(color) != 6:
        return hex_color
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    a = max(0.0, min(1.0, alpha))
    return f"rgba({r}, {g}, {b}, {a:.3f})"


ThemeName = Literal["auto", "light", "dark"]


def system_theme_name() -> Literal["light", "dark"]:
    """按当前系统/Qt 调色板判断启动主题。"""
    try:
        from PySide6.QtGui import QPalette
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        palette = app.palette() if app is not None else QPalette()
        return (
            "dark"
            if palette.color(QPalette.ColorRole.Window).lightness() < 128
            else "light"
        )
    except Exception:
        return "light"


def resolve_theme_name(theme: str | None) -> Literal["light", "dark"]:
    if theme == "dark":
        return "dark"
    if theme == "light":
        return "light"
    return system_theme_name()


def palette_for_theme(theme: str | None) -> Palette:
    return DARK if resolve_theme_name(theme) == "dark" else LIGHT


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
    "ThemeName",
    "font_family",
    "build_qss",
    "palette_for_theme",
    "resolve_theme_name",
    "system_theme_name",
]
