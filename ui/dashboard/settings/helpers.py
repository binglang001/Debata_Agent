"""Small helpers shared by settings page modules."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...theme import Spacing


def _progress_slot(progress: QProgressBar, *, width: int | None = None, height: int = Spacing.SM) -> QWidget:
    """固定进度条占位，避免忙碌动画出现时挤动表单控件。"""
    progress.setFixedHeight(4)
    slot = QWidget()
    slot.setFixedHeight(height)
    if width is not None:
        slot.setFixedWidth(width)
    lay = QVBoxLayout(slot)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addStretch(1)
    lay.addWidget(progress)
    lay.addStretch(1)
    return slot


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


def _format_tool_result_overrides(value: dict[str, int]) -> str:
    return ", ".join(f"{name}={tokens}" for name, tokens in sorted(value.items()))


def _parse_tool_result_overrides(text: str) -> dict[str, int]:
    text = text.strip()
    if not text:
        return {}
    result: dict[str, int] = {}
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("工具软阈值覆盖格式应为 tool=token，用逗号分隔")
        name, raw_value = item.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError("工具名不能为空")
        try:
            tokens = int(raw_value.strip())
        except ValueError as e:
            raise ValueError(f"{name} 的 token 阈值不是整数") from e
        if tokens < 64:
            raise ValueError(f"{name} 的 token 阈值不能小于 64")
        result[name] = tokens
    return result


def _tool_budget_group_hint(group_name: str) -> str:
    hints = {
        "消息动作": "发送、撤回、上传和唤醒类工具。结果只需要说明真实执行状态。",
        "查询工具": "联系人、用户信息、天气和搜索。列表类工具应分页，不一次返回全部。",
        "资料工具": "文件、历史、合并转发和代码输出。资料过长时写完整文件，不把头尾预览当正文。",
        "子 Agent": "资料处理入口。工具会等待子 Agent 完成，并把摘要、正文片段和结果文件作为本次工具结果返回。",
    }
    return hints.get(group_name, "")


__all__ = [
    "_format_tool_result_overrides",
    "_parse_tool_result_overrides",
    "_path_picker_row",
    "_progress_slot",
    "_set_form_field_visible",
    "_tool_budget_group_hint",
]
