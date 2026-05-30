"""插件页 —— 列表 + 详情 + 启停 + 安装指引。

布局：
    [插件列表表格]              [插件详情面板（右侧 ~40%）]
       name / kind / 状态        描述 / 依赖 / 模型目录 / 配置表单
       size / 操作按钮            操作区（启用/停用/安装指引）
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from plugins import PluginManager, PluginRecord, PluginStatus

from ..theme import Spacing
from ..widgets import show_model_install_guide
from ..widgets.window_chrome import show_message
from ..wizard.components import EmptyState, SectionCard

logger = logging.getLogger(__name__)


# ============================================================
# 状态徽标文案与色
# ============================================================


_STATUS_LABEL: dict[PluginStatus, str] = {
    PluginStatus.NOT_INSTALLED: "未安装",
    PluginStatus.INSTALLED: "未启用",
    PluginStatus.ENABLED: "使用中",
    PluginStatus.ERROR: "错误",
}

_STATUS_BADGE_ROLE: dict[PluginStatus, str] = {
    PluginStatus.NOT_INSTALLED: "badge-muted",
    PluginStatus.INSTALLED: "badge-info",
    PluginStatus.ENABLED: "badge-success",
    PluginStatus.ERROR: "badge-error",
}


_KIND_LABEL: dict[str, str] = {
    "asr": "语音识别",
    "tts": "语音合成",
    "embedding": "向量化",
    "unknown": "未知",
}


# ============================================================
# Plugins 页主体
# ============================================================


class PluginsPage(QWidget):
    """插件管理页。

    数据来源：runtime.plugin_manager（如有），否则显示 EmptyState。
    """

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._records: list[PluginRecord] = []
        self._current_name: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        # 内容栈：empty / split
        self._empty = EmptyState(
            "暂无插件",
            "尚未发现插件。确认 plugins/ 目录下有插件目录且模型文件就位。",
        )
        outer.addWidget(self._empty, 1)

        # 主体（split）—— 只在 plugin_manager 存在且有记录时显示
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

        title = QLabel("已扫描插件")
        title.setProperty("role", "title-3")
        left_v.addWidget(title)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.currentItemChanged.connect(self._on_select)
        left_v.addWidget(self._list, 1)

        refresh_btn = QPushButton("重新扫描")
        refresh_btn.setProperty("role", "ghost")
        refresh_btn.clicked.connect(self.refresh)
        left_v.addWidget(refresh_btn)

        self._split.addWidget(left)

        # 右：详情
        self._detail = _PluginDetail(self)
        self._detail.action_install_requested.connect(self._on_install)
        self._detail.action_enable_requested.connect(self._on_enable)
        self._detail.action_disable_requested.connect(self._on_disable)
        self._split.addWidget(self._detail)

        self._split.setStretchFactor(0, 3)
        self._split.setStretchFactor(1, 5)

        self.refresh()

    # ============================================================
    # 数据装载
    # ============================================================

    def refresh(self) -> None:
        """重新扫描 plugin_manager 并刷新 UI。"""
        pm = self._get_pm()
        if pm is None:
            self._empty.show()
            self._split.hide()
            return

        # 真扫
        try:
            pm.scan()
            self._records = pm.list_all()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"插件扫描失败：{e}")
            self._records = []

        if not self._records:
            self._empty.show()
            self._split.hide()
            return

        self._empty.hide()
        self._split.show()

        # 填列表
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

        # 还原选中
        if self._current_name:
            for i in range(self._list.count()):
                if (
                    self._list.item(i).data(Qt.ItemDataRole.UserRole)
                    == self._current_name
                ):
                    self._list.setCurrentRow(i)
                    return
        # 默认选第一项
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _get_pm(self) -> PluginManager | None:
        """从 runtime 拿 PluginManager；可能 None（未配置）。"""
        return getattr(self._runtime, "plugin_manager", None)

    def _get_record(self, name: str) -> PluginRecord | None:
        for r in self._records:
            if r.meta.name == name:
                return r
        return None

    # ============================================================
    # 事件
    # ============================================================

    def _on_select(self, current: QListWidgetItem | None, _: QListWidgetItem | None) -> None:
        if current is None:
            self._current_name = None
            self._detail.clear()
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        self._current_name = name
        record = self._get_record(name)
        if record:
            self._detail.set_record(record)

    def _on_install(self, name: str) -> None:
        """打开模型安装指引。"""
        record = self._get_record(name)
        if record is None:
            return
        show_model_install_guide(self, record)

    def _on_enable(self, name: str, config: dict[str, Any]) -> None:
        """启用插件。同步更新 config 确保重启 Runtime 后仍生效。"""
        pm = self._get_pm()
        if pm is None:
            return
        try:
            pm.build(name, config)
        except Exception as e:  # noqa: BLE001
            show_message(self, "启用失败", str(e), is_danger=True)
            return
        # 同步写入 config（含表单配置），重启 Runtime 后 _setup_plugins 才能重新 build
        record = pm.get(name)
        if record:
            self._sync_feature_enabled(record.meta.kind, True, config)
        show_message(
            self,
            "已启用",
            f"插件 {name} 已启用。某些功能可能需要重启 Runtime 才完全生效。",
        )
        self.refresh()

    def _on_disable(self, name: str) -> None:
        """停用插件，同步关掉 config 开关。"""
        pm = self._get_pm()
        if pm is None:
            return
        record = pm.get(name)
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(pm.shutdown(name))
            else:
                loop.run_until_complete(pm.shutdown(name))
        except Exception as e:  # noqa: BLE001
            show_message(self, "停用失败", str(e), is_danger=True)
            return
        if record:
            self._sync_feature_enabled(record.meta.kind, False)
        self.refresh()

    def _sync_feature_enabled(self, kind: str, enabled: bool, form_data: dict | None = None) -> None:
        """按插件 kind 同步 config.features.{kind} 字段并保存。"""
        from app_config.loader import save_config

        cfg = self._runtime.config
        fd = form_data or {}
        if kind == "asr":
            cfg.features.asr.enabled = enabled
            if enabled:
                cfg.features.asr.type = "local"
                cfg.features.asr.device = fd.get("device", cfg.features.asr.device)
                cfg.features.asr.language = fd.get("language", cfg.features.asr.language)
                cfg.features.asr.model_dir = fd.get("model_dir", cfg.features.asr.model_dir)
        elif kind == "tts":
            cfg.features.tts.enabled = enabled
            if enabled:
                cfg.features.tts.type = "local"
                cfg.features.tts.reference_audio = fd.get("reference_audio", cfg.features.tts.reference_audio)
                cfg.features.tts.default_prompt = fd.get("default_prompt", cfg.features.tts.default_prompt)
                if not cfg.features.tts.model_dir:
                    cfg.features.tts.model_dir = "data/models/VoxCPM2/"
        elif kind == "embedding":
            cfg.features.embedding.enabled = enabled
        else:
            return
        save_config(self._runtime.paths, cfg)


# ============================================================
# 详情面板
# ============================================================


class _PluginDetail(QWidget):
    """单个插件的详情 + 操作面板。

    会按 PluginMeta.config_schema 动态生成表单。
    保存按钮调用 enable_requested(name, config_dict)。
    """

    action_install_requested = Signal(str)
    action_enable_requested = Signal(str, dict)
    action_disable_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._record: PluginRecord | None = None
        self._field_widgets: dict[str, QWidget] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.SM, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        # 头部卡片
        self._card = SectionCard(title="", subtitle="")
        outer.addWidget(self._card, 1)

        # 头部信息
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

        # 元信息表
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
        self._meta_err.setProperty("role", "small")
        self._meta_err.setStyleSheet("color: #C0584F;")
        self._meta_err.setWordWrap(True)
        meta_v.addWidget(self._meta_err)

        self._card.add_content(meta_frame)

        # 配置表单（动态填充到这里）
        self._form_title = QLabel("插件配置")
        self._form_title.setProperty("role", "title-3")
        self._card.add_content(self._form_title)

        self._form_widget = QWidget()
        self._form_layout = QFormLayout(self._form_widget)
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._form_layout.setSpacing(Spacing.SM)
        self._card.add_content(self._form_widget)

        # 操作按钮区
        btn_row = QHBoxLayout()
        btn_row.setSpacing(Spacing.SM)

        self._btn_install = QPushButton("安装指引")
        self._btn_install.clicked.connect(self._on_install_clicked)
        btn_row.addWidget(self._btn_install)

        self._btn_enable = QPushButton("启用")
        self._btn_enable.setProperty("role", "primary")
        self._btn_enable.clicked.connect(self._on_enable_clicked)
        btn_row.addWidget(self._btn_enable)

        self._btn_disable = QPushButton("停用")
        self._btn_disable.clicked.connect(self._on_disable_clicked)
        btn_row.addWidget(self._btn_disable)

        btn_row.addStretch(1)
        self._card.add_layout(btn_row)

        self.clear()

    # ============================================================
    # 渲染
    # ============================================================

    def clear(self) -> None:
        self._record = None
        self._title.setText("（未选中）")
        self._badge.setText("")
        self._desc.setText("从左侧选一个插件查看详情。")
        self._meta_size.setText("")
        self._meta_dir.setText("")
        self._meta_deps.setText("")
        self._meta_err.setText("")
        self._clear_form()
        self._btn_install.setEnabled(False)
        self._btn_enable.setEnabled(False)
        self._btn_disable.setEnabled(False)

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
        self._meta_dir.setText(f"模型目录：{meta.model_dir or '（无需模型）'}")
        if meta.python_deps:
            self._meta_deps.setText("Python 依赖：" + ", ".join(meta.python_deps))
        else:
            self._meta_deps.setText("Python 依赖：无")
        if record.status == PluginStatus.ERROR and record.error:
            self._meta_err.setText(f"⚠ 错误：{record.error}")
        else:
            self._meta_err.setText("")

        # 表单
        self._build_form(meta.config_schema, record.status == PluginStatus.ENABLED)

        # 按钮态
        can_install = record.status in (PluginStatus.NOT_INSTALLED, PluginStatus.ERROR)
        enabled_state = record.status == PluginStatus.ENABLED
        self._btn_install.setEnabled(can_install)
        self._btn_install.setText("安装指引")
        self._btn_enable.setEnabled(
            record.status == PluginStatus.INSTALLED
            or record.status == PluginStatus.ERROR
        )
        self._btn_disable.setEnabled(enabled_state)

    def _clear_form(self) -> None:
        while self._form_layout.rowCount() > 0:
            self._form_layout.removeRow(0)
        self._field_widgets.clear()
        self._form_title.setVisible(False)
        self._form_widget.setVisible(False)

    def _build_form(self, schema: dict[str, dict[str, Any]], readonly: bool) -> None:
        self._clear_form()
        if not schema:
            return
        self._form_title.setVisible(True)
        self._form_widget.setVisible(True)
        for key, spec in schema.items():
            label_text = spec.get("label", key)
            field_type = spec.get("type", "string")
            default = spec.get("default")
            widget = self._make_field_widget(field_type, default, spec)
            widget.setEnabled(not readonly)
            self._field_widgets[key] = widget
            row_label = QLabel(label_text)
            row_label.setToolTip(spec.get("help", ""))
            self._form_layout.addRow(row_label, widget)

    def _make_field_widget(
        self, field_type: str, default: Any, spec: dict[str, Any]
    ) -> QWidget:
        if field_type == "bool":
            w = QCheckBox()
            w.setChecked(bool(default))
            return w
        if field_type == "int":
            w = QSpinBox()
            w.setRange(spec.get("min", -10**9), spec.get("max", 10**9))
            w.setValue(int(default) if default is not None else 0)
            return w
        if field_type == "float":
            w = QDoubleSpinBox()
            w.setRange(spec.get("min", -1e9), spec.get("max", 1e9))
            w.setValue(float(default) if default is not None else 0.0)
            return w
        if field_type == "select":
            w = QComboBox()
            for opt in spec.get("options", []):
                w.addItem(str(opt))
            if default is not None:
                idx = w.findText(str(default))
                if idx >= 0:
                    w.setCurrentIndex(idx)
            return w
        # 默认 string
        w = QLineEdit()
        if default is not None:
            w.setText(str(default))
        if "help" in spec:
            w.setPlaceholderText(spec["help"][:40])
        return w

    def _collect_form(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, w in self._field_widgets.items():
            if isinstance(w, QCheckBox):
                out[key] = w.isChecked()
            elif isinstance(w, QSpinBox):
                out[key] = w.value()
            elif isinstance(w, QDoubleSpinBox):
                out[key] = w.value()
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            elif isinstance(w, QLineEdit):
                out[key] = w.text()
        return out

    # ============================================================
    # 按钮回调
    # ============================================================

    def _on_install_clicked(self) -> None:
        if self._record:
            self.action_install_requested.emit(self._record.meta.name)

    def _on_enable_clicked(self) -> None:
        if self._record:
            config = self._collect_form()
            self.action_enable_requested.emit(self._record.meta.name, config)

    def _on_disable_clicked(self) -> None:
        if self._record:
            self.action_disable_requested.emit(self._record.meta.name)
