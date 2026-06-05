"""模型管理页 —— 本地模型列表 + 手动安装指引 + 状态查看。

取代原来的插件页。所有本地模型（ASR/TTS/Embedding）在此集中管理。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from plugins import PluginManager, PluginRecord, PluginStatus

from ..theme import Spacing
from ..widgets import (
    FramelessDialog,
    find_matching_record_for_folder,
    install_model_folder,
    show_model_install_guide,
)
from ..widgets.window_chrome import show_message
from ..wizard.components import EmptyState, SectionCard

logger = logging.getLogger(__name__)

_STATUS_LABEL: dict[PluginStatus, str] = {
    PluginStatus.NOT_INSTALLED: "未下载",
    PluginStatus.INSTALLED: "已下载",
    PluginStatus.ENABLED: "使用中",
    PluginStatus.ERROR: "错误",
}

_KIND_LABEL: dict[str, str] = {
    "asr": "语音识别",
    "tts": "语音合成",
    "embedding": "向量化",
    "unknown": "未知",
}


class ModelsPage(QWidget):
    """模型管理页。"""

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._records: list[PluginRecord] = []
        self._current_name: str | None = None
        self.setAcceptDrops(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        self._empty = EmptyState(
            "暂无本地模型",
            "插件目录下未发现本地模型。确认 plugins/ 目录下有模型定义。",
        )
        outer.addWidget(self._empty, 1)

        # 主体：左列表 + 右详情
        self._split = QSplitter(Qt.Orientation.Horizontal)
        self._split.setHandleWidth(1)
        self._split.setChildrenCollapsible(False)
        outer.addWidget(self._split, 1)
        self._split.hide()

        # 左：列表
        left = QWidget()
        left_v = QVBoxLayout(left)
        left_v.setContentsMargins(0, 0, Spacing.SM, 0)
        left_v.setSpacing(Spacing.SM)

        title = QLabel("本地模型")
        title.setProperty("role", "title-3")
        left_v.addWidget(title)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_select)
        left_v.addWidget(self._list, 1)

        refresh_btn = QPushButton("重新扫描")
        refresh_btn.setProperty("role", "ghost")
        refresh_btn.clicked.connect(self.refresh)
        left_v.addWidget(refresh_btn)

        drop_hint = QLabel("可把下载好的模型文件夹拖到此页面，Debata 会自动识别并复制到目标目录。")
        drop_hint.setProperty("role", "secondary")
        drop_hint.setWordWrap(True)
        left_v.addWidget(drop_hint)

        self._split.addWidget(left)

        # 右：详情
        self._detail = _ModelDetail(self)
        self._detail.action_download_requested.connect(self._on_download)
        self._split.addWidget(self._detail)

        self._split.setStretchFactor(0, 3)
        self._split.setStretchFactor(1, 5)

        self.refresh()

    def refresh(self) -> None:
        pm = self._get_pm()
        if pm is None:
            self._empty.show()
            self._split.hide()
            return

        try:
            pm.scan()
            self._records = pm.list_all()
        except Exception as e:
            logger.warning(f"模型扫描失败：{e}")
            self._records = []

        if not self._records:
            self._empty.show()
            self._split.hide()
            return

        self._empty.hide()
        self._split.show()

        self._list.blockSignals(True)
        self._list.clear()
        for record in self._records:
            text = (
                f"{record.meta.display_name}\n"
                f"  {_KIND_LABEL.get(record.meta.kind, record.meta.kind)} · "
                f"{_STATUS_LABEL[record.status]}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, record.meta.name)
            self._list.addItem(item)
        self._list.blockSignals(False)

        if self._current_name:
            for i in range(self._list.count()):
                if self._list.item(i).data(Qt.ItemDataRole.UserRole) == self._current_name:
                    self._list.setCurrentRow(i)
                    return
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _get_pm(self) -> PluginManager | None:
        return getattr(self._runtime, "plugin_manager", None)

    def _get_record(self, name: str) -> PluginRecord | None:
        for r in self._records:
            if r.meta.name == name:
                return r
        return None

    def _on_select(self, current, _previous) -> None:
        if current is None:
            self._current_name = None
            self._detail.clear()
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        self._current_name = name
        record = self._get_record(name)
        if record:
            self._detail.set_record(record)

    def _on_download(self, name: str) -> None:
        record = self._get_record(name)
        if record is None:
            return
        show_model_install_guide(self, record)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if _first_local_dir(event.mimeData()) is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        folder = _first_local_dir(event.mimeData())
        if folder is None:
            super().dropEvent(event)
            return
        match = find_matching_record_for_folder(folder, self._records)
        if match is None:
            show_message(
                self,
                "未识别模型",
                "拖入的文件夹没有匹配到当前已知模型。\n\n"
                "请确认下载的是完整模型仓库，或从右侧「安装指引」查看需要的文件列表。",
            )
            event.acceptProposedAction()
            return
        record, source_root = match
        if not show_message(
            self,
            "导入模型文件夹",
            f"识别为：{record.meta.display_name}\n\n"
            f"来源：{source_root}\n"
            f"目标：{record.meta.resolve_model_dir()}\n\n"
            "确认后会复制文件到目标目录，已有同名文件会被覆盖。",
            confirm_text="导入",
            cancel_text="取消",
        ):
            event.acceptProposedAction()
            return
        progress_dlg = FramelessDialog("导入模型", self)
        progress_dlg.setMinimumWidth(460)
        progress_dlg.body_layout().setSpacing(Spacing.SM)
        progress_label = QLabel(f"正在复制：{record.meta.display_name}")
        progress_label.setProperty("role", "secondary")
        progress_label.setWordWrap(True)
        progress_dlg.body_layout().addWidget(progress_label)
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 0)
        progress_bar.setTextVisible(False)
        progress_dlg.body_layout().addWidget(progress_bar)
        progress_dlg.show()
        QApplication.processEvents()

        def _on_copy_progress(done: int, total: int, src: Path) -> None:
            if total <= 0:
                progress_bar.setRange(0, 0)
            else:
                progress_bar.setRange(0, total)
                progress_bar.setValue(done)
            progress_label.setText(f"正在复制 {done}/{total}：{src.name}")
            QApplication.processEvents()

        try:
            install_model_folder(source_root, record, progress=_on_copy_progress)
            self.refresh()
            show_message(self, "导入完成", "模型文件已复制。状态未变化时请点击「重新扫描」。")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"导入模型失败：{e}")
            show_message(self, "导入失败", str(e), is_danger=True)
        finally:
            progress_dlg.close()
            progress_dlg.deleteLater()
        event.acceptProposedAction()


class _ModelDetail(QWidget):
    """单个模型的详情 + 操作面板。"""

    action_download_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._record: PluginRecord | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.SM, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        self._card = SectionCard(title="", subtitle="")
        outer.addWidget(self._card, 1)

        self._title = QLabel("（未选中）")
        self._title.setProperty("role", "title-2")
        self._title.setWordWrap(True)
        self._card.add_content(self._title)

        self._badge = QLabel("")
        self._badge.setProperty("role", "secondary")
        self._card.add_content(self._badge)

        self._desc = QLabel("")
        self._desc.setProperty("role", "secondary")
        self._desc.setWordWrap(True)
        self._card.add_content(self._desc)

        # 元信息
        meta_frame = QFrame()
        meta_frame.setProperty("role", "muted-block")
        meta_v = QVBoxLayout(meta_frame)
        meta_v.setContentsMargins(Spacing.SM, Spacing.SM, Spacing.SM, Spacing.SM)
        meta_v.setSpacing(Spacing.XS)

        self._meta_size = QLabel("")
        self._meta_size.setProperty("role", "small")
        meta_v.addWidget(self._meta_size)
        self._meta_dir = QLabel("")
        self._meta_dir.setProperty("role", "small")
        self._meta_dir.setWordWrap(True)
        meta_v.addWidget(self._meta_dir)
        self._meta_deps = QLabel("")
        self._meta_deps.setProperty("role", "small")
        self._meta_deps.setWordWrap(True)
        meta_v.addWidget(self._meta_deps)
        self._meta_err = QLabel("")
        self._meta_err.setProperty("role", "error")
        self._meta_err.setWordWrap(True)
        meta_v.addWidget(self._meta_err)

        self._card.add_content(meta_frame)

        # 按钮区
        btn_row = QHBoxLayout()
        btn_row.setSpacing(Spacing.SM)

        self._btn_download = QPushButton("安装指引")
        self._btn_download.clicked.connect(self._on_download_clicked)
        btn_row.addWidget(self._btn_download)

        self._btn_open_dir = QPushButton("打开目录")
        self._btn_open_dir.setProperty("role", "secondary")
        self._btn_open_dir.clicked.connect(self._on_open_dir)
        btn_row.addWidget(self._btn_open_dir)

        btn_row.addStretch(1)
        self._card.add_layout(btn_row)

        self.clear()

    def clear(self) -> None:
        self._record = None
        self._title.setText("（未选中）")
        self._badge.setText("")
        self._desc.setText("从左侧选一个模型查看详情。")
        self._meta_size.setText("")
        self._meta_dir.setText("")
        self._meta_deps.setText("")
        self._meta_err.setText("")
        self._btn_download.setEnabled(False)
        self._btn_open_dir.setEnabled(False)

    def set_record(self, record: PluginRecord) -> None:
        self._record = record
        meta = record.meta

        self._title.setText(meta.display_name)
        self._badge.setText(
            f"{_KIND_LABEL.get(meta.kind, meta.kind)} · {_STATUS_LABEL[record.status]}"
        )
        self._desc.setText(meta.description or "（无描述）")
        self._meta_size.setText(
            f"模型体积：约 {meta.size_mb} MB" if meta.size_mb else "模型体积：未知"
        )
        self._meta_dir.setText(f"模型目录：{meta.resolve_model_dir()}")
        if meta.python_deps:
            self._meta_deps.setText("Python 依赖：" + ", ".join(meta.python_deps))
        else:
            self._meta_deps.setText("Python 依赖：无")
        if record.status == PluginStatus.ERROR and record.error:
            self._meta_err.setText(f"错误：{record.error}")
        elif record.status == PluginStatus.NOT_INSTALLED:
            self._meta_err.setText("需手动安装：点击「安装指引」会打开模型页面和目标目录，或直接拖入下载好的模型文件夹。")
        elif record.status in (PluginStatus.INSTALLED, PluginStatus.ENABLED):
            self._meta_err.setText("模型已就绪。如需启用对应能力，请到设置页打开 ASR/TTS/Embedding。")
        else:
            self._meta_err.setText("")

        can_download = record.status in (PluginStatus.NOT_INSTALLED, PluginStatus.ERROR)
        self._btn_download.setEnabled(can_download)
        self._btn_download.setText("安装指引")
        self._btn_open_dir.setEnabled(True)

    def _on_download_clicked(self) -> None:
        if self._record:
            self.action_download_requested.emit(self._record.meta.name)

    def _on_open_dir(self) -> None:
        if self._record:
            d = self._record.meta.resolve_model_dir()
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))


def _first_local_dir(mime) -> Path | None:
    if mime is None or not mime.hasUrls():
        return None
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        if path.is_dir():
            return path
    return None
