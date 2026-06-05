"""模型下拉输入框。

用于所有“可获取模型”的位置：远程获取后显示模型 ID，并用 tooltip 标注能力；
输入框获得焦点时自动展开下拉，减少用户错过下拉按钮的概率。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox

from providers.model_capabilities import capability_badges


class ModelComboBox(QComboBox):
    """可编辑模型选择框。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setMinimumWidth(280)
        self.setPlaceholderText("点击「获取模型」或手动输入模型 ID")
        self.activated.connect(self._on_activated)

    def focusInEvent(self, event) -> None:  # type: ignore[override]
        super().focusInEvent(event)
        if self.count() > 0:
            self.showPopup()

    def set_models(
        self,
        model_ids: list[str],
        *,
        provider_id: str = "",
        current: str = "",
    ) -> None:
        current = current or self.current_model_id()
        self.clear()
        for mid in model_ids:
            self.add_model(mid, provider_id=provider_id)
        if current:
            idx = self.findData(current)
            if idx >= 0:
                self.setCurrentIndex(idx)
                self.setEditText(current)
            else:
                self.setEditText(current)
        elif self.count() > 0:
            first = str(self.itemData(0) or self.itemText(0))
            self.setCurrentIndex(0)
            self.setEditText(first)

    def clear(self) -> None:  # type: ignore[override]
        super().clear()
        self.setEditText("")

    def add_model(self, model_id: str, *, provider_id: str = "") -> None:
        badges = capability_badges(provider_id, model_id) if provider_id else ""
        label = model_id
        self.addItem(label, model_id)
        if badges:
            idx = self.count() - 1
            self.setItemData(idx, f"{model_id}\n能力：{badges}", Qt.ItemDataRole.ToolTipRole)

    def current_model_id(self) -> str:
        text = self.currentText().strip()
        data = self.currentData()
        if data and self.currentIndex() >= 0:
            item_text = self.itemText(self.currentIndex())
            if text == item_text or text == str(data):
                return str(data)
        return text

    def text(self) -> str:
        """兼容 QLineEdit 风格调用。"""
        return self.current_model_id()

    def setText(self, value: str) -> None:  # noqa: N802 - Qt 风格兼容方法
        """兼容 QLineEdit 风格调用。"""
        self.setEditText(value)

    def _on_activated(self, index: int) -> None:
        data = self.itemData(index)
        if data:
            self.setEditText(str(data))


__all__ = ["ModelComboBox"]
