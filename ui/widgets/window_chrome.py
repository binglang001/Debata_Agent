"""无边框窗口的标题栏 + 窗口控制（最小化 / 最大化 / 关闭）。

PySide6 默认窗口带 OS 标题栏，本项目主窗口走 Qt.FramelessWindowHint 后，
需要：
    - DragBar：可拖动 + 双击切最大化/还原的"假标题栏"
    - make_window_controls(target)：返回三按钮 widget（min/max/close）
    - FramelessDialog：QDialog 的无边框基类，配标题栏 + close 按钮
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QMouseEvent, QPainterPath, QRegion
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)


def apply_rounded_mask(window: QWidget, radius: int = 12) -> None:
    """对 frameless 窗口设圆角 mask。

    QSS 的 border-radius 在 frameless 窗口上只画背景，OS 给的窗口外形仍是矩形 —
    四角会露出底色。setMask 让 OS 层面真正按圆角裁剪，实现真圆角窗口。
    需要在 resizeEvent 中调用以跟随窗口大小变化。
    """
    path = QPainterPath()
    path.addRoundedRect(QRectF(window.rect()), radius, radius)
    polygon = path.toFillPolygon().toPolygon()
    window.setMask(QRegion(polygon))


# ============================================================
# DragBar
# ============================================================


class DragBar(QFrame):
    """可拖动的标题栏。构造时传入需要被拖动的 target 窗口。

    - 左键按住 + 拖动 → 移动窗口（拖动时如果是最大化态，先还原）
    - 左键双击 → 切最大化 / 还原
    """

    def __init__(self, target: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target = target
        self._drag_pos: QPoint | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self._target.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._target.isMaximized():
                # 最大化态下拖动 → 先还原，把光标锚定在窗口顶部偏左
                self._target.showNormal()
                self._drag_pos = QPoint(self._target.width() // 2, 20)
            self._target.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._target.isMaximized():
                self._target.showNormal()
            else:
                self._target.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


# ============================================================
# 窗口控制按钮
# ============================================================


def make_window_controls(target: QWidget) -> QWidget:
    """构造 [最小化][最大化][关闭] 三按钮的 widget。

    返回的 widget 可加到任意 horizontal layout。close 按钮命中 target.close()。
    最大化按钮自动在 ☐/❐ 间切换标签。
    """
    wrap = QWidget()
    wrap.setObjectName("WindowControls")
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)

    min_btn = QPushButton("─")
    min_btn.setObjectName("WinMin")
    min_btn.setProperty("role", "win")
    min_btn.setFixedSize(38, 28)
    min_btn.setToolTip("最小化")
    min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    min_btn.clicked.connect(target.showMinimized)
    lay.addWidget(min_btn)

    max_btn = QPushButton("☐")
    max_btn.setObjectName("WinMax")
    max_btn.setProperty("role", "win")
    max_btn.setFixedSize(38, 28)
    max_btn.setToolTip("最大化 / 还原")
    max_btn.setCursor(Qt.CursorShape.PointingHandCursor)

    def _toggle_max() -> None:
        if target.isMaximized():
            target.showNormal()
            max_btn.setText("☐")
        else:
            target.showMaximized()
            max_btn.setText("❐")

    max_btn.clicked.connect(_toggle_max)
    lay.addWidget(max_btn)

    close_btn = QPushButton("✕")
    close_btn.setObjectName("WinClose")
    close_btn.setProperty("role", "win-close")
    close_btn.setFixedSize(38, 28)
    close_btn.setToolTip("关闭")
    close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    close_btn.clicked.connect(target.close)
    lay.addWidget(close_btn)

    return wrap


# ============================================================
# FramelessDialog
# ============================================================


class FramelessDialog(QDialog):
    """无边框对话框基类。自带顶部标题栏（标题文本 + 关闭按钮）+ 拖动支持。

    子类在 self.body_layout() 上叠加自己的内容即可。
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(title)

        # Dialog 本身透明，所有内容塞到 WindowFrame 里取 border-radius
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        frame = QFrame()
        frame.setObjectName("WindowFrame")
        outer.addWidget(frame)

        root = QVBoxLayout(frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏
        title_bar = DragBar(self)
        title_bar.setObjectName("DialogTitleBar")
        title_bar.setFixedHeight(40)
        tlay = QHBoxLayout(title_bar)
        tlay.setContentsMargins(16, 0, 4, 0)
        tlay.setSpacing(8)

        self._title_label = QLabel(title)
        self._title_label.setProperty("role", "title-3")
        tlay.addWidget(self._title_label)
        tlay.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setProperty("role", "win-close")
        close_btn.setFixedSize(38, 28)
        close_btn.clicked.connect(self.reject)
        tlay.addWidget(close_btn)

        root.addWidget(title_bar)

        # 主体容器
        self._body = QWidget()
        body_lay = QVBoxLayout(self._body)
        body_lay.setContentsMargins(24, 16, 24, 16)
        body_lay.setSpacing(16)
        root.addWidget(self._body, 1)

        self._root_layout = root
        self._body_layout = body_lay

    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    def set_title(self, text: str) -> None:
        self._title_label.setText(text)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        apply_rounded_mask(self, radius=12)


# ============================================================
# 简易消息框（替换 QMessageBox 的 frameless 版本）
# ============================================================


def show_message(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    confirm_text: str = "知道了",
    cancel_text: str | None = None,
    is_danger: bool = False,
) -> bool:
    """无边框信息 / 确认弹窗。返回 True = 用户点 confirm；False = 取消或关闭。

    cancel_text=None 时只有一个确认按钮（信息提示）；
    传 cancel_text 时是两按钮（确认 / 取消）。
    """
    dlg = FramelessDialog(title, parent)
    dlg.setMinimumWidth(420)

    body = QLabel(text)
    body.setWordWrap(True)
    dlg.body_layout().addWidget(body)
    dlg.body_layout().addStretch(1)

    btn_row = QHBoxLayout()
    btn_row.addStretch(1)
    if cancel_text:
        cancel_btn = QPushButton(cancel_text)
        cancel_btn.setProperty("role", "secondary")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
    confirm_btn = QPushButton(confirm_text)
    confirm_btn.setProperty("role", "danger" if is_danger else "primary")
    confirm_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(confirm_btn)
    dlg.body_layout().addLayout(btn_row)

    return dlg.exec() == QDialog.DialogCode.Accepted


__all__ = [
    "DragBar",
    "FramelessDialog",
    "apply_rounded_mask",
    "make_window_controls",
    "show_message",
]
