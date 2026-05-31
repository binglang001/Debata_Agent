"""角色页 —— 列表 / 新建 / 激活 / 复制 / 删除 / 导入 / 导出。"""

from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from agents.persona_import import (
    PersonaImportError,
    copy_persona_dir,
    import_persona_zip,
)
from agents.persona_loader import validate_persona_name

from ..theme import Spacing
from ..widgets import show_message
from .copy import DASHBOARD_COPY

logger = logging.getLogger(__name__)


_BUILTIN = {"debata"}  # 仓库自带的，不允许删除


class PersonasPage(QWidget):
    """人格管理。"""

    restart_requested = Signal()

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        head = QHBoxLayout()
        title = QLabel(DASHBOARD_COPY["personas.list_title"])
        title.setProperty("role", "title-2")
        head.addWidget(title)
        head.addStretch(1)
        outer.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(Spacing.MD)

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._update_button_states)
        body.addWidget(self._list, 1)

        # 右侧操作列
        actions = QVBoxLayout()
        actions.setSpacing(Spacing.SM)

        self._activate_btn = QPushButton(DASHBOARD_COPY["personas.activate_button"])
        self._activate_btn.setProperty("role", "primary")
        self._activate_btn.clicked.connect(self._on_activate)
        actions.addWidget(self._activate_btn)

        self._duplicate_btn = QPushButton(DASHBOARD_COPY["personas.duplicate_button"])
        self._duplicate_btn.setProperty("role", "secondary")
        self._duplicate_btn.clicked.connect(self._on_duplicate)
        actions.addWidget(self._duplicate_btn)

        self._export_btn = QPushButton(DASHBOARD_COPY["personas.export_button"])
        self._export_btn.setProperty("role", "secondary")
        self._export_btn.clicked.connect(self._on_export)
        actions.addWidget(self._export_btn)

        self._import_btn = QPushButton("导入 zip")
        self._import_btn.setProperty("role", "secondary")
        self._import_btn.clicked.connect(self._on_import_zip)
        actions.addWidget(self._import_btn)

        self._import_dir_btn = QPushButton("导入目录")
        self._import_dir_btn.setProperty("role", "secondary")
        self._import_dir_btn.clicked.connect(self._on_import_dir)
        actions.addWidget(self._import_dir_btn)

        self._delete_btn = QPushButton(DASHBOARD_COPY["personas.delete_button"])
        self._delete_btn.setProperty("role", "danger")
        self._delete_btn.clicked.connect(self._on_delete)
        actions.addWidget(self._delete_btn)

        actions.addStretch(1)
        body.addLayout(actions)

        outer.addLayout(body, 1)

        # 注解
        note = QLabel("切换当前角色后需重启 Debata 才能生效。")
        note.setProperty("role", "secondary")
        outer.addWidget(note)

        self.refresh()

    # ---- 数据 ----

    @property
    def _personas_dir(self) -> Path:
        return self._runtime.paths.PERSONAS_DIR

    @property
    def _active_name(self) -> str:
        return self._runtime.config.persona.active if self._runtime and self._runtime.config else ""

    def _list_personas(self) -> list[str]:
        out: list[str] = []
        if not self._personas_dir.exists():
            return out
        for p in sorted(self._personas_dir.iterdir()):
            if not p.is_dir() or p.name.startswith("_") or p.name.startswith("."):
                continue
            if (p / "persona_prompt.py").exists():
                out.append(p.name)
        return out

    def refresh(self) -> None:
        self._list.clear()
        for name in self._list_personas():
            tags = []
            if name == self._active_name:
                tags.append(f"［{DASHBOARD_COPY['personas.active_badge']}］")
            if name in _BUILTIN:
                tags.append(f"［{DASHBOARD_COPY['personas.builtin_badge']}］")
            tag_str = " ".join(tags)
            line = f"{name}   {tag_str}".strip()
            item = QListWidgetItem(line)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._list.addItem(item)
        self._update_button_states()

    def _select_name(self, name: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                self._list.setCurrentRow(i)
                return

    def _selected(self) -> str | None:
        it = self._list.currentItem()
        if it is None:
            return None
        return it.data(Qt.ItemDataRole.UserRole)

    def _update_button_states(self) -> None:
        name = self._selected()
        has = name is not None
        self._activate_btn.setEnabled(has and name != self._active_name)
        self._duplicate_btn.setEnabled(has)
        self._export_btn.setEnabled(has)
        # 内置 / 当前激活不允许删除
        self._delete_btn.setEnabled(
            has and name != self._active_name and name not in _BUILTIN
        )

    # ---- 动作 ----

    def _on_activate(self) -> None:
        name = self._selected()
        if not name or self._runtime is None:
            return
        try:
            cfg = self._runtime.config
            cfg.persona.active = name
            from app_config.loader import save_config
            save_config(self._runtime.paths, cfg)
            restart_now = show_message(
                self,
                "已记住",
                f"已切换到「{name}」。是否立即重启 Debata 服务让它生效？",
                confirm_text="重启",
                cancel_text="稍后",
            )
            self.refresh()
            self._select_name(name)
            if restart_now:
                self.restart_requested.emit()
        except Exception as e:
            logger.exception("激活人格失败")
            show_message(self, "未能完成", str(e), is_danger=True)

    def _on_duplicate(self) -> None:
        name = self._selected()
        if not name:
            return
        new_name, ok = QInputDialog.getText(self, "复制角色", "新角色名（目录名）：", text=f"{name}_copy")
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()
        try:
            validate_persona_name(new_name)
        except ValueError as e:
            show_message(self, "名称不合法", str(e), is_danger=True)
            return
        src = self._personas_dir / name
        dst = self._personas_dir / new_name
        if dst.exists():
            show_message(self, "已存在", f"目录「{new_name}」已存在", is_danger=True)
            return
        try:
            shutil.copytree(src, dst)
            self.refresh()
            self._select_name(new_name)
            show_message(self, "已复制", f"已创建角色「{new_name}」。")
        except Exception as e:
            show_message(self, "未能完成", str(e), is_danger=True)

    def _on_export(self) -> None:
        name = self._selected()
        if not name:
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "导出角色", f"{name}.zip", "Zip (*.zip)"
        )
        if not target:
            return
        try:
            src = self._personas_dir / name
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in src.rglob("*"):
                    if p.is_file():
                        zf.write(p, p.relative_to(src.parent))
            show_message(self, "已导出", f"已写入：{target}")
        except Exception as e:
            show_message(self, "未能完成", str(e), is_danger=True)

    def _on_import_zip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入角色 zip", "", "Zip (*.zip)"
        )
        if not path:
            return
        try:
            self._finish_import(import_persona_zip(Path(path), self._personas_dir))
        except PersonaImportError as e:
            show_message(self, "无法导入", str(e), is_danger=True)
        except Exception as e:
            show_message(self, "未能完成", str(e), is_danger=True)

    def _on_import_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "导入角色目录")
        if not path:
            return
        try:
            self._finish_import(copy_persona_dir(Path(path), self._personas_dir))
        except PersonaImportError as e:
            show_message(self, "无法导入", str(e), is_danger=True)
        except Exception as e:
            show_message(self, "未能完成", str(e), is_danger=True)

    def _finish_import(self, imported_name: str) -> None:
        self.refresh()
        self._select_name(imported_name)
        show_message(self, "已导入", f"已导入角色「{imported_name}」。")

    def _on_delete(self) -> None:
        name = self._selected()
        if not name:
            return
        if name in _BUILTIN:
            show_message(self, "不能删除", "仓库自带的角色不允许删除")
            return
        if name == self._active_name:
            show_message(
                self,
                "不能删除",
                DASHBOARD_COPY["personas.delete_active_warning"],
            )
            return

        if not show_message(
            self,
            DASHBOARD_COPY["personas.delete_confirm_title"],
            DASHBOARD_COPY["personas.delete_confirm_body"],
            confirm_text="删除",
            cancel_text="算了",
            is_danger=True,
        ):
            return

        try:
            shutil.rmtree(self._personas_dir / name)
            self.refresh()
        except Exception as e:
            show_message(self, "未能完成", str(e), is_danger=True)
