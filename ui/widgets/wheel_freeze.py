"""滚轮冻结：阻止未获焦点的 SpinBox/ComboBox 响应鼠标滚轮。

在滚动区域中包含 QSpinBox / QDoubleSpinBox / QComboBox 时，
用户滚页面可能意外改变这些控件的值。
安装此过滤器后，仅已聚焦的控件才响应滚轮。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QComboBox,
    QScrollArea,
    QWidget,
)


class WheelFreezeFilter(QObject):
    """阻止未获焦点的数字输入框/下拉框响应滚轮事件。"""

    def __init__(self, root: QWidget) -> None:
        super().__init__(root)
        self._root = root

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            if not _belongs_to_root(obj, self._root):
                return False
            control = _wheel_control(obj)
            if control is not None:
                scroll = _nearest_scroll_area(control)
                if scroll is not None:
                    delta = getattr(event, "angleDelta", lambda: None)()
                    if delta is not None:
                        bar = scroll.verticalScrollBar()
                        bar.setValue(bar.value() - delta.y())
                return True
        if (
            event.type() == QEvent.Type.ChildAdded
            and isinstance(obj, QWidget)
            and _belongs_to_root(obj, self._root)
        ):
            child = event.child()
            if isinstance(child, QWidget):
                _install_recursive(child, self)
        return False


def install_wheel_freeze(root: QWidget) -> WheelFreezeFilter:
    """给 root 及其所有子控件安装滚轮冻结过滤器。"""
    f = WheelFreezeFilter(root)
    app = QApplication.instance()
    if app is not None:
        app.installEventFilter(f)
    root.installEventFilter(f)
    _install_recursive(root, f)
    return f


def _install_recursive(widget: QWidget, f: WheelFreezeFilter) -> None:
    if isinstance(widget, (QAbstractSpinBox, QComboBox)):
        widget.installEventFilter(f)
    widget.installEventFilter(f)
    for child in widget.findChildren(QWidget):
        child.installEventFilter(f)
        if isinstance(child, (QAbstractSpinBox, QComboBox)):
            child.installEventFilter(f)


def _wheel_control(obj: QObject) -> QWidget | None:
    if not isinstance(obj, QWidget):
        return None
    cur: QWidget | None = obj
    while cur is not None:
        if isinstance(cur, (QAbstractSpinBox, QComboBox)):
            return cur
        cur = cur.parentWidget()
    return None


def _nearest_scroll_area(widget: QWidget) -> QScrollArea | None:
    cur = widget.parentWidget()
    while cur is not None:
        if isinstance(cur, QScrollArea):
            return cur
        cur = cur.parentWidget()
    return None


def _belongs_to_root(obj: QObject, root: QWidget) -> bool:
    if obj is root:
        return True
    if not isinstance(obj, QWidget):
        return False
    return obj is root or root.isAncestorOf(obj)
