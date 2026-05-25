"""日志页 —— 接 root logger 显示流式日志。

实现细节：
    - 自定义 logging.Handler 把记录推到 QListWidget
    - widget 销毁时主动 detach handler（避免 RuntimeError 死递归）
    - 顶部过滤：级别（DEBUG/INFO/WARNING/ERROR）+ 模块前缀 + 搜索
    - 缓冲上限 2000 行，超出删头
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from .copy import DASHBOARD_COPY


_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
}


class _LogBridge(QObject):
    """跨线程把 logging record 推到 UI。

    logging 可能从 worker 线程发出；用 Qt signal 派发到主线程。
    """

    log_emitted = Signal(object)  # logging.LogRecord


class _SignalHandler(logging.Handler):
    """把 LogRecord emit 出去的 handler。"""

    def __init__(self, bridge: _LogBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._bridge.log_emitted.emit(record)
        except RuntimeError:
            # bridge 已被销毁 —— root logger 仍持有此 handler 时会触发
            pass


class LogsPage(QWidget):
    """日志查看。"""

    MAX_BUFFER = 2000

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        # 过滤栏
        filter_row = QHBoxLayout()
        filter_row.setSpacing(Spacing.MD)

        self._level_combo = QComboBox()
        self._level_combo.addItem("全部", 0)
        self._level_combo.addItem("调试 以上", logging.DEBUG)
        self._level_combo.addItem("信息 以上", logging.INFO)
        self._level_combo.addItem("留意 以上", logging.WARNING)
        self._level_combo.addItem("出错", logging.ERROR)
        self._level_combo.setCurrentIndex(2)  # 默认 INFO
        self._level_combo.currentIndexChanged.connect(self._refilter)
        filter_row.addWidget(QLabel(DASHBOARD_COPY["logs.filter_level"]))
        filter_row.addWidget(self._level_combo)

        self._module_edit = QLineEdit()
        self._module_edit.setPlaceholderText("模块前缀过滤")
        self._module_edit.setMaximumWidth(220)
        self._module_edit.textChanged.connect(self._refilter)
        filter_row.addWidget(QLabel(DASHBOARD_COPY["logs.filter_module"]))
        filter_row.addWidget(self._module_edit)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(DASHBOARD_COPY["logs.search_placeholder"])
        self._search_edit.textChanged.connect(self._refilter)
        filter_row.addWidget(self._search_edit, 1)

        clear_btn = QPushButton(DASHBOARD_COPY["logs.clear_button"])
        clear_btn.setProperty("role", "secondary")
        clear_btn.clicked.connect(self._on_clear)
        filter_row.addWidget(clear_btn)

        outer.addLayout(filter_row)

        # 日志列表
        self._list = QListWidget()
        self._list.setUniformItemSizes(True)
        outer.addWidget(self._list, 1)

        # 缓冲
        self._records: deque[logging.LogRecord] = deque(maxlen=self.MAX_BUFFER)

        # 装 handler
        self._bridge = _LogBridge(self)
        self._bridge.log_emitted.connect(self._on_log_record, Qt.ConnectionType.QueuedConnection)
        self._handler = _SignalHandler(self._bridge)
        self._handler.setLevel(logging.DEBUG)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(self._handler)

        # 销毁时 detach（避免 widget gone after 触发 RuntimeError）
        self.destroyed.connect(self._detach_handler)

    def _detach_handler(self, *_: Any) -> None:
        try:
            logging.getLogger().removeHandler(self._handler)
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        self._detach_handler()
        super().closeEvent(event)

    # ---- 处理日志 ----

    def _on_log_record(self, record: logging.LogRecord) -> None:
        self._records.append(record)
        if self._should_show(record):
            self._append_item(record)
            # 自动滚到底
            self._list.scrollToBottom()

    def _should_show(self, record: logging.LogRecord) -> bool:
        threshold = self._level_combo.currentData() or 0
        if threshold and record.levelno < threshold:
            return False
        prefix = self._module_edit.text().strip()
        if prefix and not record.name.startswith(prefix):
            return False
        search = self._search_edit.text().strip().lower()
        if search:
            msg = record.getMessage().lower()
            if search not in msg and search not in record.name.lower():
                return False
        return True

    def _append_item(self, record: logging.LogRecord) -> None:
        try:
            asctime = logging.Formatter("%(asctime)s").format(record).split(",")[0]
        except Exception:
            asctime = ""
        level_name = _LEVEL_NAMES.get(record.levelno, record.levelname)
        text = f"{asctime}  {level_name:<5}  {record.name}  :  {record.getMessage()}"
        item = QListWidgetItem(text)
        # 等级染色
        if record.levelno >= logging.ERROR:
            item.setForeground(Qt.GlobalColor.red)
        elif record.levelno >= logging.WARNING:
            item.setForeground(Qt.GlobalColor.darkYellow)
        elif record.levelno >= logging.INFO:
            pass
        else:
            item.setForeground(Qt.GlobalColor.gray)
        self._list.addItem(item)

    def _refilter(self, *_: Any) -> None:
        self._list.clear()
        for r in list(self._records):
            if self._should_show(r):
                self._append_item(r)
        self._list.scrollToBottom()

    def _on_clear(self) -> None:
        self._records.clear()
        self._list.clear()
