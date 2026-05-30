"""无边框窗口的标题栏 + 窗口控制（最小化 / 最大化 / 关闭）。

PySide6 默认窗口带 OS 标题栏，本项目主窗口走 Qt.FramelessWindowHint 后，
需要：
    - DragBar：可拖动 + 双击切最大化/还原的"假标题栏"
    - make_window_controls(target)：返回三按钮 widget（min/max/close）
    - FramelessDialog：QDialog 的无边框基类，配标题栏 + close 按钮
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QRect,
    Qt,
)
from PySide6.QtGui import QMouseEvent, QPainterPath, QRegion
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollBar,
    QSizeGrip,
    QTabBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


_RESIZE_MARGIN = 7

_WM_NCHITTEST = 0x0084
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17


class _WinMSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


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


def attach_size_grip(window: QWidget, size: int = 18) -> QSizeGrip:
    """给 frameless 顶层窗口加右下角 resize grip。"""
    grip = QSizeGrip(window)
    grip.setFixedSize(size, size)
    grip.setObjectName("WindowSizeGrip")
    grip.raise_()
    position_size_grip(window, grip)
    return grip


def native_resize_hit_test(
    window: QWidget,
    event_type,
    message,
    margin: int = _RESIZE_MARGIN,
) -> tuple[bool, int] | None:
    """Windows frameless 窗口原生四边缩放命中测试。

    Qt 事件过滤器只能兜底处理进入 widget 的鼠标事件；无边框窗口的真实边缘
    resize 更适合交给 WM_NCHITTEST，系统会负责光标、拖拽和跨屏 DPI 细节。
    """
    if not sys.platform.startswith("win") or window.isMaximized():
        return None
    try:
        event_name = (
            bytes(event_type).decode("ascii", errors="ignore")
            if not isinstance(event_type, str)
            else event_type
        )
    except Exception:
        event_name = str(event_type)
    if "windows" not in event_name.lower():
        return None

    try:
        msg = _WinMSG.from_address(int(message))
    except Exception:
        return None
    if msg.message != _WM_NCHITTEST:
        return None

    lparam = int(msg.lParam)
    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
    geo = window.frameGeometry()
    left = geo.x()
    top = geo.y()
    right = left + geo.width()
    bottom = top + geo.height()

    on_left = left <= x < left + margin
    on_right = right - margin <= x < right
    on_top = top <= y < top + margin
    on_bottom = bottom - margin <= y < bottom

    if on_top and on_left:
        return True, _HTTOPLEFT
    if on_top and on_right:
        return True, _HTTOPRIGHT
    if on_bottom and on_left:
        return True, _HTBOTTOMLEFT
    if on_bottom and on_right:
        return True, _HTBOTTOMRIGHT
    if on_left:
        return True, _HTLEFT
    if on_right:
        return True, _HTRIGHT
    if on_top:
        return True, _HTTOP
    if on_bottom:
        return True, _HTBOTTOM
    return None


class WindowResizeFilter(QObject):
    """给 frameless 窗口提供四边和四角 resize 命中区。"""

    def __init__(self, target: QWidget, root: QWidget, margin: int = _RESIZE_MARGIN) -> None:
        super().__init__(target)
        self._target = target
        self._root = root
        self._margin = margin
        self._filtered_widgets: list[QWidget] = []
        self._manual_edges = Qt.Edge(0)
        self._manual_start_pos: QPoint | None = None
        self._manual_start_geo: QRect | None = None
        target.destroyed.connect(self._remove_all_filters)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        root = getattr(self, "_root", None)
        target = getattr(self, "_target", None)
        if root is None or target is None:
            return False
        try:
            etype = event.type()
        except (AttributeError, RuntimeError):
            return False

        if not isinstance(obj, QWidget) or not _belongs_to_root(obj, root):
            return False
        if _is_interactive_widget(obj) or target.isMaximized():
            return False
        if not isinstance(event, QMouseEvent):
            return False

        if etype == QEvent.Type.MouseMove:
            if self._manual_start_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self._resize_manually(event.globalPosition().toPoint())
                event.accept()
                return True
            edges = _resize_edges_for_global_pos(
                target,
                event.globalPosition().toPoint(),
                self._margin,
            )
            cursor = _cursor_for_edges(edges)
            if cursor is None:
                obj.unsetCursor()
            else:
                obj.setCursor(cursor)
            return False

        if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            edges = _resize_edges_for_global_pos(
                target,
                event.globalPosition().toPoint(),
                self._margin,
            )
            if edges:
                if self._start_native_resize(edges):
                    event.accept()
                    return True
                self._manual_edges = edges
                self._manual_start_pos = event.globalPosition().toPoint()
                self._manual_start_geo = self._target.geometry()
                event.accept()
                return True

        if etype == QEvent.Type.MouseButtonRelease:
            self._manual_edges = Qt.Edge(0)
            self._manual_start_pos = None
            self._manual_start_geo = None
            return False

        return False

    def add_widget(self, widget: QWidget) -> None:
        widget.installEventFilter(self)
        self._filtered_widgets.append(widget)

    def _remove_all_filters(self) -> None:
        for widget in list(getattr(self, "_filtered_widgets", [])):
            try:
                widget.removeEventFilter(self)
            except RuntimeError:
                pass
        self._filtered_widgets.clear()

    def _start_native_resize(self, edges) -> bool:
        handle = self._target.windowHandle()
        if handle is None or not hasattr(handle, "startSystemResize"):
            return False
        try:
            return bool(handle.startSystemResize(edges))
        except Exception:
            return False

    def _resize_manually(self, global_pos: QPoint) -> None:
        if self._manual_start_pos is None or self._manual_start_geo is None:
            return
        delta = global_pos - self._manual_start_pos
        geo = QRect(self._manual_start_geo)
        min_w = max(1, self._target.minimumWidth())
        min_h = max(1, self._target.minimumHeight())
        edges = self._manual_edges

        if edges & Qt.Edge.LeftEdge:
            new_x = geo.x() + delta.x()
            new_w = geo.width() - delta.x()
            if new_w < min_w:
                new_x = geo.right() - min_w + 1
                new_w = min_w
            geo.setX(new_x)
            geo.setWidth(new_w)
        elif edges & Qt.Edge.RightEdge:
            geo.setWidth(max(min_w, geo.width() + delta.x()))

        if edges & Qt.Edge.TopEdge:
            new_y = geo.y() + delta.y()
            new_h = geo.height() - delta.y()
            if new_h < min_h:
                new_y = geo.bottom() - min_h + 1
                new_h = min_h
            geo.setY(new_y)
            geo.setHeight(new_h)
        elif edges & Qt.Edge.BottomEdge:
            geo.setHeight(max(min_h, geo.height() + delta.y()))

        self._target.setGeometry(geo)


def install_window_resize(root: QWidget, target: QWidget, margin: int = _RESIZE_MARGIN) -> WindowResizeFilter:
    """给 root 及子控件安装 frameless 窗口全向 resize 过滤器。"""
    f = WindowResizeFilter(target, root, margin)
    _install_window_resize_recursive(root, f)
    return f


def _install_window_resize_recursive(widget: QWidget, f: WindowResizeFilter) -> None:
    if _is_interactive_widget(widget):
        return
    f.add_widget(widget)
    for child in widget.findChildren(QWidget):
        if _is_interactive_widget(child):
            continue
        f.add_widget(child)


def _resize_edges_for_global_pos(window: QWidget, global_pos: QPoint, margin: int):
    return _resize_edges_for_local_pos(
        window,
        window.mapFromGlobal(global_pos),
        margin,
    )


def _resize_edges_for_local_pos(window: QWidget, pos: QPoint, margin: int = _RESIZE_MARGIN):
    edges = Qt.Edge(0)
    x = pos.x()
    y = pos.y()
    if 0 <= x <= margin:
        edges |= Qt.Edge.LeftEdge
    elif window.width() - margin <= x < window.width():
        edges |= Qt.Edge.RightEdge
    if 0 <= y <= margin:
        edges |= Qt.Edge.TopEdge
    elif window.height() - margin <= y < window.height():
        edges |= Qt.Edge.BottomEdge
    return edges


def _cursor_for_edges(edges):
    if not edges:
        return None
    if (edges & Qt.Edge.LeftEdge and edges & Qt.Edge.TopEdge) or (
        edges & Qt.Edge.RightEdge and edges & Qt.Edge.BottomEdge
    ):
        return Qt.CursorShape.SizeFDiagCursor
    if (edges & Qt.Edge.RightEdge and edges & Qt.Edge.TopEdge) or (
        edges & Qt.Edge.LeftEdge and edges & Qt.Edge.BottomEdge
    ):
        return Qt.CursorShape.SizeBDiagCursor
    if edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
        return Qt.CursorShape.SizeHorCursor
    return Qt.CursorShape.SizeVerCursor


def position_size_grip(window: QWidget, grip: QSizeGrip | None) -> None:
    if grip is None:
        return
    if window.isMaximized():
        grip.hide()
        return
    grip.show()
    margin = 4
    grip.move(
        max(0, window.width() - grip.width() - margin),
        max(0, window.height() - grip.height() - margin),
    )


def fade_in_window(window: QWidget, duration_ms: int = 140) -> None:
    """轻量窗口淡入；失败时静默回退到无动画。"""
    try:
        window.setWindowOpacity(0.0)
        anim = QPropertyAnimation(window, b"windowOpacity", window)
        anim.setDuration(duration_ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        window._fade_animation = anim  # type: ignore[attr-defined]
        anim.start()
    except Exception:
        try:
            window.setWindowOpacity(1.0)
        except Exception:
            pass


class WindowDragFilter(QObject):
    """让非交互空白区域也能拖动 frameless 主窗口。"""

    def __init__(self, target: QWidget, root: QWidget) -> None:
        super().__init__(target)
        self._target = target
        self._root = root
        self._filtered_widgets: list[QWidget] = []
        self._drag_pos: QPoint | None = None
        target.destroyed.connect(self._remove_all_filters)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        root = getattr(self, "_root", None)
        target = getattr(self, "_target", None)
        if root is None or target is None:
            return False
        try:
            etype = event.type()
        except (AttributeError, RuntimeError):
            return False

        if not isinstance(obj, QWidget) or not _belongs_to_root(obj, root):
            return False
        if _is_interactive_widget(obj):
            return False
        if isinstance(event, QMouseEvent):
            try:
                if _resize_edges_for_global_pos(
                    target,
                    event.globalPosition().toPoint(),
                    _RESIZE_MARGIN,
                ):
                    return False
            except RuntimeError:
                return False

        if etype == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.LeftButton:
                if self._start_native_move():
                    self._drag_pos = None
                    event.accept()
                    return True
                self._drag_pos = (
                    event.globalPosition().toPoint()
                    - target.frameGeometry().topLeft()
                )
                event.accept()
                return True
        if etype == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
                if target.isMaximized():
                    target.showNormal()
                    self._drag_pos = QPoint(target.width() // 2, 20)
                target.move(event.globalPosition().toPoint() - self._drag_pos)
                event.accept()
                return True
        if etype == QEvent.Type.MouseButtonRelease:
            self._drag_pos = None
            return False
        if etype == QEvent.Type.MouseButtonDblClick and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.LeftButton:
                if target.isMaximized():
                    target.showNormal()
                else:
                    target.showMaximized()
                event.accept()
                return True
        return False

    def _remove_all_filters(self) -> None:
        for widget in list(getattr(self, "_filtered_widgets", [])):
            try:
                widget.removeEventFilter(self)
            except RuntimeError:
                pass
        self._filtered_widgets.clear()

    def _start_native_move(self) -> bool:
        try:
            handle = self._target.windowHandle()
        except RuntimeError:
            return False
        if handle is None or not hasattr(handle, "startSystemMove"):
            return False
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False


def install_window_drag(root: QWidget, target: QWidget) -> WindowDragFilter:
    """给 root 及其子控件安装空白区域拖动过滤器。"""
    f = WindowDragFilter(target, root)
    _install_window_drag_recursive(root, f)
    return f


def _window_drag_filter_add_widget(f: WindowDragFilter, widget: QWidget) -> None:
    widget.installEventFilter(f)
    f._filtered_widgets.append(widget)


def _install_window_drag_recursive(widget: QWidget, f: WindowDragFilter) -> None:
    if _is_interactive_widget(widget):
        return
    _window_drag_filter_add_widget(f, widget)
    for child in widget.findChildren(QWidget):
        if _is_interactive_widget(child):
            continue
        _window_drag_filter_add_widget(f, child)


def _belongs_to_root(obj: QObject, root: QWidget) -> bool:
    return isinstance(obj, QWidget) and (obj is root or root.isAncestorOf(obj))


def _is_interactive_widget(widget: QWidget) -> bool:
    interactive = (
        QAbstractButton,
        QAbstractItemView,
        QAbstractSlider,
        QAbstractSpinBox,
        QComboBox,
        QLineEdit,
        QPlainTextEdit,
        QScrollBar,
        QSizeGrip,
        QTabBar,
        QTextEdit,
    )
    cur: QWidget | None = widget
    while cur is not None:
        if isinstance(cur, interactive):
            return True
        cur = cur.parentWidget()
    return False


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
            if self._start_native_move():
                self._drag_pos = None
                event.accept()
                return
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

    def _start_native_move(self) -> bool:
        try:
            handle = self._target.windowHandle()
        except RuntimeError:
            return False
        if handle is None or not hasattr(handle, "startSystemMove"):
            return False
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False


# ============================================================
# 窗口控制按钮
# ============================================================


def make_window_controls(
    target: QWidget, *, show_min: bool = True, show_max: bool = True
) -> QWidget:
    """构造 [最小化][最大化][关闭] 三按钮的 widget。

    返回的 widget 可加到任意 horizontal layout。close 按钮命中 target.close()。
    show_min / show_max=False 可隐藏对应按钮（弹窗场景）。
    """
    wrap = QWidget()
    wrap.setObjectName("WindowControls")
    lay = QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)

    if show_min:
        min_btn = QPushButton("─")
        min_btn.setObjectName("WinMin")
        min_btn.setProperty("role", "win")
        min_btn.setFixedSize(38, 28)
        min_btn.setToolTip("最小化")
        min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        min_btn.clicked.connect(target.showMinimized)
        lay.addWidget(min_btn)

    if show_max:
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
        self.setProperty("frameless", True)
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
        self._size_grip = attach_size_grip(self)
        self._window_resize_filter = install_window_resize(frame, self)

    def body_layout(self) -> QVBoxLayout:
        return self._body_layout

    def set_title(self, text: str) -> None:
        self._title_label.setText(text)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        apply_rounded_mask(self, radius=12)
        position_size_grip(self, self._size_grip)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        fade_in_window(self)

    def nativeEvent(self, eventType, message):  # type: ignore[override]
        hit = native_resize_hit_test(self, eventType, message)
        if hit is not None:
            return hit
        return super().nativeEvent(eventType, message)


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
    "attach_size_grip",
    "fade_in_window",
    "install_window_drag",
    "install_window_resize",
    "make_window_controls",
    "native_resize_hit_test",
    "position_size_grip",
    "show_message",
]
