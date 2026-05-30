"""表情包管理 section + 添加对话框。

列表视图（缩略图 + 文件名 + 重命名 / 删除按钮）
+ 添加对话框（拖放区 + 文件选择 + 多文件预览命名）。

表情包目录：runtime.paths.EMOJI_DIR（data/emoji/）。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..theme import Radius, Spacing
from ..widgets.window_chrome import (
    DragBar,
    apply_rounded_mask,
    make_window_controls,
    show_message,
)
from ..wizard.components import SectionCard

logger = logging.getLogger(__name__)


SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


# ============================================================
# 表情包 section（嵌在设置页）
# ============================================================


class EmojiSection(SectionCard):
    """设置页的「表情包」节。"""

    def __init__(self, emoji_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(
            title="表情包",
            subtitle="管理 Debata 在聊天中可用的表情包图片。AI 通过文件名引用，建议起易读的名字（如「笑哭」「困」）。",
            parent=parent,
        )
        self._emoji_dir = emoji_dir
        self._items: list[tuple[Path, QFrame]] = []
        self._columns = 4

        # 顶部操作行
        top = QHBoxLayout()
        top.setSpacing(Spacing.SM)
        self._count_lbl = QLabel("")
        self._count_lbl.setProperty("role", "secondary")
        top.addWidget(self._count_lbl)
        top.addStretch(1)
        add_btn = QPushButton("添加表情包...")
        add_btn.setProperty("role", "primary")
        add_btn.clicked.connect(self._on_add)
        top.addWidget(add_btn)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)
        self.add_layout(top)

        # 滚动容器
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(280)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setSpacing(Spacing.SM)
        self._grid.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self._grid_host)
        self.add_content(scroll)

        self.refresh()

    # ============================================================
    # 刷新
    # ============================================================

    def refresh(self) -> None:
        # 清空网格
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._emoji_dir.exists():
            self._count_lbl.setText("（表情包目录尚未创建）")
            return

        files = sorted(
            [p for p in self._emoji_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS],
            key=lambda p: p.name.lower(),
        )
        self._count_lbl.setText(f"共 {len(files)} 个表情包" if files else "暂无表情包，点击右上「添加」")

        self._columns = self._column_count()
        for idx, p in enumerate(files):
            tile = self._make_tile(p)
            row, col = divmod(idx, self._columns)
            self._grid.addWidget(tile, row, col)
        # 占位以让网格保持左对齐
        self._grid.setRowStretch(self._grid.rowCount(), 1)

    def _column_count(self) -> int:
        width = max(self._grid_host.width(), self.width(), 180)
        return max(1, min(6, width // 180))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        columns = self._column_count()
        if columns != self._columns:
            self.refresh()

    def _make_tile(self, path: Path) -> QFrame:
        tile = QFrame()
        tile.setObjectName("EmojiTile")
        tile.setFrameShape(QFrame.Shape.StyledPanel)
        tile.setMinimumWidth(150)
        tile.setStyleSheet(
            f"QFrame#EmojiTile {{ border: 1px solid rgba(0,0,0,0.1); "
            f"border-radius: {Radius.SMALL}px; }}"
        )
        v = QVBoxLayout(tile)
        v.setContentsMargins(Spacing.SM, Spacing.SM, Spacing.SM, Spacing.SM)
        v.setSpacing(Spacing.XS)

        # 缩略图
        thumb = QLabel()
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setFixedSize(140, 100)
        pix = QPixmap(str(path))
        if not pix.isNull():
            thumb.setPixmap(
                pix.scaled(
                    140, 100,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            thumb.setText("（无法加载）")
        v.addWidget(thumb)

        # 文件名
        name_lbl = QLabel(path.name)
        name_lbl.setProperty("role", "small")
        name_lbl.setWordWrap(True)
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(name_lbl)

        # 操作按钮
        btns = QHBoxLayout()
        btns.setSpacing(Spacing.XS)
        rename_btn = QPushButton("改名")
        rename_btn.clicked.connect(lambda _checked=False, p=path: self._on_rename(p))
        btns.addWidget(rename_btn)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(lambda _checked=False, p=path: self._on_delete(p))
        btns.addWidget(del_btn)
        v.addLayout(btns)

        return tile

    # ============================================================
    # 事件
    # ============================================================

    def _on_add(self) -> None:
        dlg = EmojiAddDialog(self._emoji_dir, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def _on_rename(self, path: Path) -> None:
        new_name, ok = QInputDialog.getText(
            self, "重命名表情包", "新文件名（含扩展名）：", text=path.name
        )
        if not ok or not new_name.strip() or new_name == path.name:
            return
        new_name = new_name.strip()
        if "/" in new_name or "\\" in new_name:
            show_message(self, "重命名失败", "文件名不能包含 / 或 \\", is_danger=True)
            return
        new_path = path.parent / new_name
        if new_path.exists():
            show_message(self, "重命名失败", f"已存在同名文件：{new_name}", is_danger=True)
            return
        try:
            path.rename(new_path)
        except OSError as e:
            show_message(self, "重命名失败", str(e), is_danger=True)
            return
        self.refresh()

    def _on_delete(self, path: Path) -> None:
        if not show_message(
            self,
            "删除表情包",
            f"确定删除 {path.name} 吗？此操作不可撤销。",
            confirm_text="删除",
            cancel_text="算了",
            is_danger=True,
        ):
            return
        try:
            path.unlink()
        except OSError as e:
            show_message(self, "删除失败", str(e), is_danger=True)
            return
        self.refresh()


# ============================================================
# 添加表情包对话框（拖放 + 多文件预览命名）
# ============================================================


class EmojiAddDialog(QDialog):
    """添加表情包对话框。

    工作流：
        1. 用户拖入图片 / 点击「打开...」选文件
        2. 列表展示每张图（缩略图 + 默认文件名输入框）
        3. 点击图片右侧可预览大图
        4. 全部命好名 → 「保存全部」复制到 emoji_dir
    """

    def __init__(self, emoji_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._emoji_dir = emoji_dir
        self._staged: list[_StagedItem] = []

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setMinimumSize(720, 540)
        self.setAcceptDrops(True)

        # 圆角容器
        self._root = QFrame(self)
        self._root.setObjectName("WindowFrame")
        self._root.setGeometry(self.rect())

        root = QVBoxLayout(self._root)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶栏
        topbar = DragBar(self)
        top_lay = QHBoxLayout(topbar)
        top_lay.setContentsMargins(Spacing.MD, 0, Spacing.SM, 0)
        top_lay.setSpacing(Spacing.SM)
        title = QLabel("添加表情包")
        title.setProperty("role", "title-3")
        top_lay.addWidget(title)
        top_lay.addStretch(1)
        top_lay.addWidget(make_window_controls(self, show_min=False, show_max=False))
        root.addWidget(topbar)

        # 主体
        body = QHBoxLayout()
        body.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        body.setSpacing(Spacing.MD)

        # 左：拖放区 + 文件列表
        left = QVBoxLayout()
        left.setSpacing(Spacing.SM)

        self._drop_hint = QLabel("把图片拖到这里\n或者点击下方按钮选择文件")
        self._drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_hint.setProperty("role", "secondary")
        self._drop_hint.setStyleSheet(
            f"QLabel {{ border: 2px dashed rgba(0,0,0,0.2); "
            f"border-radius: {Radius.DEFAULT}px; padding: {Spacing.LG}px; }}"
        )
        self._drop_hint.setMinimumHeight(120)
        left.addWidget(self._drop_hint)

        pick_btn = QPushButton("打开文件...")
        pick_btn.clicked.connect(self._on_pick)
        left.addWidget(pick_btn)

        self._list = QListWidget()
        self._list.setIconSize(QSize(48, 48))
        self._list.itemSelectionChanged.connect(self._on_select)
        left.addWidget(self._list, 1)

        body.addLayout(left, 4)

        # 右：单个文件预览 + 命名
        right = QVBoxLayout()
        right.setSpacing(Spacing.SM)

        self._preview = QLabel("（选中左侧文件预览）")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(280, 280)
        self._preview.setStyleSheet(
            f"QLabel {{ border: 1px solid rgba(0,0,0,0.1); "
            f"border-radius: {Radius.SMALL}px; }}"
        )
        right.addWidget(self._preview, 1)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("文件名"))
        self._name_edit = QLineEdit()
        self._name_edit.editingFinished.connect(self._on_name_edited)
        name_row.addWidget(self._name_edit, 1)
        right.addLayout(name_row)

        body.addLayout(right, 3)

        root.addLayout(body)

        # 底部按钮
        btns = QHBoxLayout()
        btns.setContentsMargins(Spacing.LG, 0, Spacing.LG, Spacing.LG)
        btns.setSpacing(Spacing.SM)
        btns.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(cancel_btn)
        save_btn = QPushButton("保存全部")
        save_btn.setProperty("role", "primary")
        save_btn.clicked.connect(self._on_save_all)
        btns.addWidget(save_btn)
        root.addLayout(btns)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._root.setGeometry(self.rect())
        apply_rounded_mask(self)

    # ============================================================
    # 拖放
    # ============================================================

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls()
        paths = [Path(u.toLocalFile()) for u in urls if u.isLocalFile()]
        self._stage_files(paths)

    # ============================================================
    # 文件选择
    # ============================================================

    def _on_pick(self) -> None:
        filters = "图片文件 (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;所有文件 (*)"
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择表情包图片", "", filters
        )
        self._stage_files([Path(f) for f in files])

    def _stage_files(self, paths: list[Path]) -> None:
        added = 0
        for p in paths:
            if not p.is_file():
                continue
            if p.suffix.lower() not in SUPPORTED_EXTS:
                continue
            staged = _StagedItem(src=p, dest_name=p.stem)
            self._staged.append(staged)
            item = QListWidgetItem(f"{staged.dest_name}{p.suffix}")
            item.setData(Qt.ItemDataRole.UserRole, len(self._staged) - 1)
            pix = QPixmap(str(p))
            if not pix.isNull():
                item.setIcon(pix.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio))
            self._list.addItem(item)
            added += 1
        self._refresh_staged_names()
        if added > 0:
            self._drop_hint.setText(f"已添加 {len(self._staged)} 张。继续拖放或点击保存。")

    # ============================================================
    # 预览与命名
    # ============================================================

    def _on_select(self) -> None:
        items = self._list.selectedItems()
        if not items:
            self._preview.clear()
            self._preview.setText("（选中左侧文件预览）")
            self._name_edit.clear()
            self._name_edit.setEnabled(False)
            return
        idx = items[0].data(Qt.ItemDataRole.UserRole)
        staged = self._staged[idx]
        pix = QPixmap(str(staged.src))
        if not pix.isNull():
            self._preview.setPixmap(
                pix.scaled(
                    self._preview.width() - 8,
                    self._preview.height() - 8,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self._preview.setText("（无法预览）")
        self._name_edit.setEnabled(True)
        self._name_edit.setText(staged.dest_name)

    def _on_name_edited(self) -> None:
        items = self._list.selectedItems()
        if not items:
            return
        idx = items[0].data(Qt.ItemDataRole.UserRole)
        new_name = self._name_edit.text().strip()
        if not new_name:
            return
        # 不允许斜杠
        if "/" in new_name or "\\" in new_name:
            show_message(self, "文件名非法", "不能包含 / 或 \\", is_danger=True)
            self._name_edit.setText(self._staged[idx].dest_name)
            return
        self._staged[idx].dest_name = new_name
        self._refresh_staged_names()

    # ============================================================
    # 保存
    # ============================================================

    def _on_save_all(self) -> None:
        if not self._staged:
            show_message(self, "什么也没有", "先拖入或选择图片")
            return
        self._on_name_edited()
        self._emoji_dir.mkdir(parents=True, exist_ok=True)
        final_names = self._final_dest_names()
        saved = 0
        failed: list[str] = []
        for staged, final_name in zip(self._staged, final_names):
            dest = self._emoji_dir / final_name
            try:
                shutil.copy2(staged.src, dest)
                saved += 1
            except OSError as e:
                failed.append(f"{staged.src.name}: {e}")

        if failed:
            show_message(
                self,
                "部分失败",
                f"成功 {saved} 张，失败 {len(failed)} 张：\n" + "\n".join(failed[:5]),
            )
        self.accept()

    def _refresh_staged_names(self) -> None:
        final_names = self._final_dest_names()
        for idx, final_name in enumerate(final_names):
            item = self._list.item(idx)
            if item is not None:
                item.setText(final_name)

    def _final_dest_names(self) -> list[str]:
        existing = set()
        if self._emoji_dir.exists():
            existing = {p.name.lower() for p in self._emoji_dir.iterdir() if p.is_file()}
        used = set(existing)
        final_names: list[str] = []
        for staged in self._staged:
            base = staged.dest_name.strip() or staged.src.stem
            suffix = staged.src.suffix
            candidate = f"{base}{suffix}"
            counter = 0
            while candidate.lower() in used:
                counter += 1
                candidate = f"{base}_{counter}{suffix}"
            used.add(candidate.lower())
            final_names.append(candidate)
        return final_names


class _StagedItem:
    """添加对话框里待保存的一条。"""

    __slots__ = ("src", "dest_name")

    def __init__(self, src: Path, dest_name: str) -> None:
        self.src = src
        self.dest_name = dest_name


__all__ = ["EmojiSection", "EmojiAddDialog"]
