"""Shared helpers for wizard step views."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ...theme import Spacing

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _path_picker_row(
    edit: QLineEdit,
    *,
    parent: QWidget,
    title: str,
    directory: bool,
    file_filter: str = "所有文件 (*)",
) -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(Spacing.SM)
    lay.addWidget(edit, 1)
    btn = QPushButton("浏览")
    btn.setProperty("role", "secondary")

    def _pick() -> None:
        start = edit.text().strip()
        if directory:
            path = QFileDialog.getExistingDirectory(parent, title, start)
        else:
            path, _ = QFileDialog.getOpenFileName(parent, title, start, file_filter)
        if path:
            edit.setText(path)

    btn.clicked.connect(_pick)
    lay.addWidget(btn)
    return row


def _set_form_field_visible(form: QFormLayout, field: QWidget, visible: bool) -> None:
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)
    field.setVisible(visible)


def _open_directory(path: str) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))


def _resolve_project_path(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (_PROJECT_ROOT / p).resolve()


def _directory_has_files(path: str) -> bool:
    p = _resolve_project_path(path)
    if not p.exists() or not p.is_dir():
        return False
    try:
        return any(child.is_file() for child in p.rglob("*"))
    except OSError:
        return False


def _prompt_download_model(
    parent: QWidget,
    title: str,
    message: str,
    on_download: Callable[[], None],
) -> None:
    from ...widgets.window_chrome import show_message

    if show_message(
        parent,
        title,
        message,
        confirm_text="查看安装指引",
        cancel_text="先不处理",
    ):
        on_download()


def _start_plugin_download(
    parent,
    plugin_name: str,
    plugin_dir: str,
    display_name: str,
    on_finished: Callable[[], None] | None = None,
) -> None:
    """Open the model installation guide from wizard feature cards."""
    from plugins import PluginManager

    from ...widgets import show_model_install_guide
    from ...widgets.window_chrome import show_message

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    plugins_root = project_root / "plugins"

    if not plugins_root.exists():
        show_message(parent, "目录不存在", f"未找到 plugins 目录：{plugins_root}")
        return

    pm = PluginManager(plugins_root)
    try:
        pm.scan()
    except Exception as e:
        show_message(parent, "扫描失败", str(e))
        return

    record = pm.get(plugin_name)
    if record is None:
        for r in pm.list_all():
            if r.module_path and r.module_path.parent.name == plugin_dir:
                record = r
                break
    if record is None:
        show_message(parent, "未找到插件", f"未找到名为 {plugin_name} 的模型插件。")
        return

    show_model_install_guide(parent, record)
    if on_finished is not None:
        on_finished()


def _add_guide_button(layout, guide_name: str, parent_widget) -> None:
    """Add a compact tutorial button to a card header layout."""
    from ..components import open_feature_guide

    btn = QPushButton("教程")
    btn.setFlat(True)
    btn.setProperty("role", "ghost")
    btn.clicked.connect(lambda: open_feature_guide(guide_name, parent_widget))
    layout.addWidget(btn)


__all__ = [
    "_add_guide_button",
    "_directory_has_files",
    "_open_directory",
    "_path_picker_row",
    "_prompt_download_model",
    "_resolve_project_path",
    "_set_form_field_visible",
    "_start_plugin_download",
]
