"""向导 / 设置共用的 UI 组件。

抽离原因：
    - 设置页与向导共用同一批表单组件，避免两套不同口径
    - 组件只关心呈现与本地状态，业务逻辑（实际测试连接、保存配置）由调用方接信号

组件清单：
    - SectionCard   : 卡片容器（标题 + 副标题 + 内容区）
    - EmptyState    : 空状态（图标占位 + 一句静的话）
    - ApiKeyInput   : 密钥输入（遮蔽 + 显示/隐藏 + 测试连接）
    - ProviderSelector : provider 下拉（预设 + 自定义）
    - WhitelistEditor : 白名单三选 + QQ/群名单
    - TutorialDialog : 教程弹窗（接 Markdown 字符串）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..theme import Spacing
from ..widgets.window_chrome import FramelessDialog

# ============================================================
# SectionCard
# ============================================================


class SectionCard(QFrame):
    """卡片容器。标题 + 可选副标题 + 内容区。

    用于向导每一步的主体，也是设置页每个分节的主体。
    """

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        compact: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SectionCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        outer = QVBoxLayout(self)
        margin = Spacing.MD if compact else Spacing.LG
        outer.setContentsMargins(margin, margin, margin, margin)
        outer.setSpacing(Spacing.SM if compact else Spacing.MD)

        if title:
            title_lbl = QLabel(title)
            title_lbl.setProperty("role", "title-3" if compact else "title-2")
            title_lbl.setWordWrap(True)
            outer.addWidget(title_lbl)

        if subtitle:
            sub_lbl = QLabel(subtitle)
            sub_lbl.setProperty("role", "secondary")
            sub_lbl.setWordWrap(True)
            outer.addWidget(sub_lbl)

        # 标题与内容之间一道极淡的分隔
        if title or subtitle:
            sep = QFrame()
            sep.setProperty("role", "separator")
            outer.addWidget(sep)

        # 内容区由外部用 add_content 注入
        self._body = QVBoxLayout()
        self._body.setSpacing(Spacing.MD)
        outer.addLayout(self._body)

    def add_content(self, widget: QWidget) -> None:
        """把一个 widget 加入卡片内容区。"""
        self._body.addWidget(widget)

    def add_layout(self, layout) -> None:
        self._body.addLayout(layout)


# ============================================================
# EmptyState
# ============================================================


class EmptyState(QWidget):
    """空状态。一句静的话，不要喋喋不休。

    布局：标题（T2）+ 简短副标题；上方留 35% 空白，不正中。
    """

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.SM)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        # 上方留白（约 35%）
        layout.addSpacing(int(Spacing.XXL * 1.5))

        title_lbl = QLabel(title)
        title_lbl.setProperty("role", "title-3")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_lbl)

        if subtitle:
            sub_lbl = QLabel(subtitle)
            sub_lbl.setProperty("role", "secondary")
            sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub_lbl.setWordWrap(True)
            layout.addWidget(sub_lbl)

        layout.addStretch(1)


# ============================================================
# ApiKeyInput
# ============================================================


TestState = Literal["idle", "testing", "success", "error"]


class ApiKeyInput(QWidget):
    """API 密钥输入。

    布局：
        [QLineEdit (password) ][显示/隐藏]
        [测试连接]                [状态文案]

    信号：
        test_requested(str)  — 用户按下"测试连接"时触发，参数是当前 key
        text_changed(str)    — key 变化时触发，方便上层做实时校验
    """

    test_requested = Signal(str)
    text_changed = Signal(str)

    def __init__(
        self,
        placeholder: str = "",
        test_button_text: str = "测试连接",
        allow_empty_test: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._allow_empty_test = allow_empty_test

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.SM)

        # 第一行：输入 + 显示/隐藏
        row1 = QHBoxLayout()
        row1.setSpacing(Spacing.SM)

        self._edit = QLineEdit()
        self._edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit.setPlaceholderText(placeholder)
        self._edit.textChanged.connect(self._on_text_changed)
        row1.addWidget(self._edit, 1)

        self._toggle_btn = QPushButton("显示")
        self._toggle_btn.setProperty("role", "secondary")
        self._toggle_btn.setFixedWidth(72)
        self._toggle_btn.clicked.connect(self._toggle_visibility)
        row1.addWidget(self._toggle_btn)

        outer.addLayout(row1)

        # 第二行：测试按钮 + 状态
        row2 = QHBoxLayout()
        row2.setSpacing(Spacing.MD)

        self._test_btn = QPushButton(test_button_text)
        self._test_btn.setProperty("role", "secondary")
        self._test_btn.clicked.connect(self._on_test_clicked)
        row2.addWidget(self._test_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setProperty("role", "secondary")
        row2.addWidget(self._status_lbl, 1)

        outer.addLayout(row2)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.setVisible(False)

        progress_slot = QWidget()
        progress_slot.setFixedHeight(Spacing.SM)
        progress_slot_lay = QVBoxLayout(progress_slot)
        progress_slot_lay.setContentsMargins(0, 0, 0, 0)
        progress_slot_lay.setSpacing(0)
        progress_slot_lay.addStretch(1)
        progress_slot_lay.addWidget(self._progress)
        progress_slot_lay.addStretch(1)
        outer.addWidget(progress_slot)

        self._state: TestState = "idle"

    def text(self) -> str:
        return self._edit.text().strip()

    def is_test_success(self) -> bool:
        return self._state == "success"

    def set_text(self, value: str) -> None:
        self._edit.setText(value)

    def set_test_state(self, state: TestState, message: str = "") -> None:
        """由调用方在 test_requested 处理完后调用，更新状态显示。"""
        self._state = state
        if state == "idle":
            self._status_lbl.setText("")
            self._status_lbl.setProperty("role", "secondary")
            self._edit.setProperty("state", "")
            self._test_btn.setEnabled(True)
            self._progress.setVisible(False)
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
        elif state == "testing":
            self._status_lbl.setText(message or "正在尝试连接……")
            self._status_lbl.setProperty("role", "secondary")
            self._test_btn.setEnabled(False)
            self._progress.setVisible(True)
            self._progress.setRange(0, 0)
        elif state == "success":
            self._status_lbl.setText(message or "已就位")
            self._status_lbl.setProperty("role", "success")
            self._edit.setProperty("state", "success")
            self._test_btn.setEnabled(True)
            self._progress.setVisible(False)
            self._progress.setRange(0, 100)
            self._progress.setValue(100)
        elif state == "error":
            self._status_lbl.setText(message or "未能完成")
            self._status_lbl.setProperty("role", "error")
            self._edit.setProperty("state", "error")
            self._test_btn.setEnabled(True)
            self._progress.setVisible(False)
            self._progress.setRange(0, 100)
            self._progress.setValue(0)
        # 触发样式重算
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)
        self._edit.style().unpolish(self._edit)
        self._edit.style().polish(self._edit)

    # ---- 内部 ----

    def _on_text_changed(self, value: str) -> None:
        # 输入变化时复位状态
        if self._state != "idle":
            self.set_test_state("idle")
        self.text_changed.emit(value)

    def _toggle_visibility(self) -> None:
        if self._edit.echoMode() == QLineEdit.EchoMode.Password:
            self._edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._toggle_btn.setText("隐藏")
        else:
            self._edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._toggle_btn.setText("显示")

    def _on_test_clicked(self) -> None:
        key = self.text()
        if not key and not self._allow_empty_test:
            self.set_test_state("error", "先填一下密钥")
            return
        self.set_test_state("testing")
        self.test_requested.emit(key)


# ============================================================
# ProviderSelector
# ============================================================


@dataclass(slots=True)
class ProviderOption:
    """单个 provider 选项。"""

    value: str
    """传给上层的 id（如 'deepseek' / 'anthropic' / 'custom'）"""

    label: str
    """显示给用户的中文名"""

    description: str = ""
    """悬停或下方副标题文案"""


class ProviderSelector(QWidget):
    """provider 选择。下拉 + 选中后的简短说明。

    把"自行填一个"也作为一个选项，调用方根据 value=='custom' 决定是否展开自定义字段。

    信号：
        selection_changed(str)  — 当前 value
    """

    selection_changed = Signal(str)

    def __init__(
        self,
        options: list[ProviderOption],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._options = options
        self._value: str = options[0].value if options else ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.SM)

        # 用单选按钮组而不是 QComboBox：每项可显示一行说明，更符合"留白即设计"
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        for opt in options:
            row = self._build_option_row(opt)
            outer.addWidget(row)

        outer.addStretch(1)

        # 默认选中第一项
        if self._buttons():
            self._buttons()[0].setChecked(True)

    def _buttons(self) -> list[QRadioButton]:
        return [b for b in self._group.buttons() if isinstance(b, QRadioButton)]

    def _build_option_row(self, opt: ProviderOption) -> QWidget:
        wrapper = QFrame()
        wrapper.setObjectName("Card")
        wlayout = QHBoxLayout(wrapper)
        wlayout.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        wlayout.setSpacing(Spacing.MD)

        btn = QRadioButton(opt.label)
        btn.setProperty("provider_value", opt.value)
        btn.toggled.connect(lambda checked, v=opt.value: self._on_toggled(checked, v))
        self._group.addButton(btn)
        wlayout.addWidget(btn, 0)

        if opt.description:
            desc = QLabel(opt.description)
            desc.setProperty("role", "secondary")
            desc.setWordWrap(True)
            wlayout.addWidget(desc, 1)
        else:
            wlayout.addStretch(1)

        return wrapper

    def _on_toggled(self, checked: bool, value: str) -> None:
        if checked:
            self._value = value
            self.selection_changed.emit(value)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        for btn in self._buttons():
            if btn.property("provider_value") == value:
                btn.setChecked(True)
                self._value = value
                return


# ============================================================
# WhitelistEditor
# ============================================================


@dataclass(slots=True)
class WhitelistState:
    """白名单当前状态。"""

    mode: Literal["verify", "whitelist", "open"] = "verify"
    qq_ids: list[str] = field(default_factory=list)
    group_ids: list[str] = field(default_factory=list)


class WhitelistEditor(QWidget):
    """白名单编辑。

    三种模式：
        verify    : 管理员审核（默认推荐）
        whitelist : 严格白名单（要填名单）
        open      : 对所有人开放（危险，要二次确认）

    信号：
        state_changed(WhitelistState)  — 任何变更
    """

    state_changed = Signal(object)

    def __init__(
        self,
        initial: WhitelistState | None = None,
        on_open_confirm: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = initial or WhitelistState()
        self._on_open_confirm = on_open_confirm

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        # 模式选择
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        self._rb_verify = self._make_mode_row(
            "verify",
            "管理员审核",
            "陌生人加好友 / 加群时，由你确认。当前默认。",
        )
        self._rb_whitelist = self._make_mode_row(
            "whitelist",
            "白名单",
            "只响应名单内的 QQ 和群。最严格。",
        )
        self._rb_open = self._make_mode_row(
            "open",
            "对所有人开放",
            "⚠ 谁都可以触发 Debata。可能产生意外的 API 费用。",
        )
        for r in (self._rb_verify, self._rb_whitelist, self._rb_open):
            outer.addWidget(r)

        # 名单（仅 whitelist 模式显示）
        self._lists_frame = QFrame()
        self._lists_frame.setObjectName("Card")
        lists_layout = QHBoxLayout(self._lists_frame)
        lists_layout.setContentsMargins(Spacing.MD, Spacing.MD, Spacing.MD, Spacing.MD)
        lists_layout.setSpacing(Spacing.LG)

        # QQ 名单
        qq_box = QVBoxLayout()
        qq_box.addWidget(self._small_label("QQ 名单"))
        qq_row = QHBoxLayout()
        self._qq_input = QLineEdit()
        self._qq_input.setPlaceholderText("输入 QQ 号回车添加")
        self._qq_input.returnPressed.connect(self._add_qq)
        qq_row.addWidget(self._qq_input)
        add_qq_btn = QPushButton("添加")
        add_qq_btn.setProperty("role", "secondary")
        add_qq_btn.clicked.connect(self._add_qq)
        qq_row.addWidget(add_qq_btn)
        qq_box.addLayout(qq_row)
        self._qq_list = QListWidget()
        self._qq_list.setMinimumHeight(120)
        self._qq_list.itemDoubleClicked.connect(
            lambda item: self._remove_item(self._qq_list, item, "qq")
        )
        qq_box.addWidget(self._qq_list)
        qq_box.addWidget(self._small_label("双击移除"))
        lists_layout.addLayout(qq_box, 1)

        # 群名单
        grp_box = QVBoxLayout()
        grp_box.addWidget(self._small_label("群名单"))
        grp_row = QHBoxLayout()
        self._grp_input = QLineEdit()
        self._grp_input.setPlaceholderText("输入群号回车添加")
        self._grp_input.returnPressed.connect(self._add_group)
        grp_row.addWidget(self._grp_input)
        add_grp_btn = QPushButton("添加")
        add_grp_btn.setProperty("role", "secondary")
        add_grp_btn.clicked.connect(self._add_group)
        grp_row.addWidget(add_grp_btn)
        grp_box.addLayout(grp_row)
        self._grp_list = QListWidget()
        self._grp_list.setMinimumHeight(120)
        self._grp_list.itemDoubleClicked.connect(
            lambda item: self._remove_item(self._grp_list, item, "group")
        )
        grp_box.addWidget(self._grp_list)
        grp_box.addWidget(self._small_label("双击移除"))
        lists_layout.addLayout(grp_box, 1)

        outer.addWidget(self._lists_frame)
        outer.addStretch(1)

        # 应用初始状态
        self._apply_state(self._state)
        self._update_lists_visibility()

    # ---- 公开 ----

    def state(self) -> WhitelistState:
        return WhitelistState(
            mode=self._state.mode,
            qq_ids=list(self._state.qq_ids),
            group_ids=list(self._state.group_ids),
        )

    def set_state(self, state: WhitelistState) -> None:
        self._apply_state(state)

    # ---- 构造辅助 ----

    def _small_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "caption")
        return lbl

    def _make_mode_row(self, value: str, label: str, desc: str) -> QFrame:
        wrapper = QFrame()
        wrapper.setObjectName("Card")
        wl = QHBoxLayout(wrapper)
        wl.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)

        col = QVBoxLayout()
        col.setSpacing(2)

        rb = QRadioButton(label)
        rb.setProperty("mode_value", value)
        rb.toggled.connect(lambda checked, v=value: self._on_mode_toggled(checked, v))
        self._mode_group.addButton(rb)
        col.addWidget(rb)
        # 把 button 引用挂到 wrapper 上，便于外部取（避免 findChildren 后比对失败）
        wrapper._radio = rb  # type: ignore[attr-defined]

        d = QLabel(desc)
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 0)  # 与单选按钮的文字对齐
        col.addWidget(d)

        wl.addLayout(col)
        wl.addStretch(1)
        return wrapper

    def _radio_for(self, mode: str) -> QRadioButton:
        return {
            "verify": self._rb_verify._radio,  # type: ignore[attr-defined]
            "whitelist": self._rb_whitelist._radio,  # type: ignore[attr-defined]
            "open": self._rb_open._radio,  # type: ignore[attr-defined]
        }[mode]

    def _all_radios(self) -> list[QRadioButton]:
        return [self._radio_for(m) for m in ("verify", "whitelist", "open")]

    # ---- 状态变更 ----

    def _apply_state(self, state: WhitelistState) -> None:
        self._state = WhitelistState(
            mode=state.mode,
            qq_ids=list(state.qq_ids),
            group_ids=list(state.group_ids),
        )
        # 单选：blockSignals 避免 setChecked 又触发 _on_mode_toggled 死递归
        target_btn = self._radio_for(state.mode)
        for btn in self._all_radios():
            btn.blockSignals(True)
        target_btn.setChecked(True)
        for btn in self._all_radios():
            btn.blockSignals(False)
        # 列表
        self._qq_list.clear()
        for qq in state.qq_ids:
            self._qq_list.addItem(QListWidgetItem(qq))
        self._grp_list.clear()
        for g in state.group_ids:
            self._grp_list.addItem(QListWidgetItem(g))

    def _on_mode_toggled(self, checked: bool, value: str) -> None:
        if not checked:
            return
        if value == "open" and self._state.mode != "open":
            if self._on_open_confirm is not None:
                ok = self._on_open_confirm()
                if not ok:
                    # 取消 → 真正回退到原 mode（_apply_state 会 blockSignals 防递归）
                    self._apply_state(self._state)
                    return
        self._state.mode = value  # type: ignore[assignment]
        self._update_lists_visibility()
        self.state_changed.emit(self.state())

    def _update_lists_visibility(self) -> None:
        self._lists_frame.setVisible(self._state.mode == "whitelist")

    def _add_qq(self) -> None:
        v = self._qq_input.text().strip()
        if not v or not v.isdigit():
            return
        if v in self._state.qq_ids:
            self._qq_input.clear()
            return
        self._state.qq_ids.append(v)
        self._qq_list.addItem(QListWidgetItem(v))
        self._qq_input.clear()
        self.state_changed.emit(self.state())

    def _add_group(self) -> None:
        v = self._grp_input.text().strip()
        if not v or not v.isdigit():
            return
        if v in self._state.group_ids:
            self._grp_input.clear()
            return
        self._state.group_ids.append(v)
        self._grp_list.addItem(QListWidgetItem(v))
        self._grp_input.clear()
        self.state_changed.emit(self.state())

    def _remove_item(
        self,
        list_widget: QListWidget,
        item: QListWidgetItem,
        kind: Literal["qq", "group"],
    ) -> None:
        v = item.text()
        if kind == "qq" and v in self._state.qq_ids:
            self._state.qq_ids.remove(v)
        if kind == "group" and v in self._state.group_ids:
            self._state.group_ids.remove(v)
        row = list_widget.row(item)
        list_widget.takeItem(row)
        self.state_changed.emit(self.state())


# ============================================================
# TutorialDialog
# ============================================================


class TutorialDialog(FramelessDialog):
    """教程弹窗。接 Markdown 字符串，用 QTextBrowser 渲染。"""

    def __init__(
        self,
        title: str,
        markdown: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self.setMinimumSize(620, 480)
        self.resize(700, 560)
        self.setModal(True)

        layout = self.body_layout()
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(Spacing.SM)

        browser = QTextBrowser()
        browser.setObjectName("TutorialBrowser")
        browser.setOpenExternalLinks(True)
        browser.document().setDocumentMargin(12)
        browser.document().setDefaultStyleSheet(
            """
            h1 { font-size: 20px; margin: 0 0 10px 0; }
            h2 { font-size: 17px; margin: 12px 0 6px 0; }
            h3 { font-size: 15px; margin: 10px 0 4px 0; }
            p { margin: 4px 0 8px 0; line-height: 1.35; }
            ul, ol { margin-top: 4px; margin-bottom: 8px; }
            li { margin: 3px 0; }
            code { font-family: Consolas, monospace; }
            a { color: #6FA39A; }
            """
        )
        browser.setMarkdown(_strip_leading_h1(markdown))
        f = browser.font()
        f.setPointSize(11)
        browser.setFont(f)
        layout.addWidget(browser, 1)

        btn_box = QDialogButtonBox()
        close_btn = QPushButton("知道了")
        close_btn.setProperty("role", "primary")
        close_btn.clicked.connect(self.accept)
        btn_box.addButton(close_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        layout.addWidget(btn_box)


def _strip_leading_h1(markdown: str) -> str:
    lines = markdown.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def open_feature_guide(guide_name: str, parent: QWidget | None = None) -> None:
    """打开 docs/feature_guides/{name}.md 教程。

    找不到文件时显示一个友好的占位。
    """
    from pathlib import Path

    # docs 在项目根：ui/wizard/components.py → ui → 项目根 → docs/
    project_root = Path(__file__).resolve().parent.parent.parent
    md_path = project_root / "docs" / "feature_guides" / f"{guide_name}.md"
    if not md_path.exists():
        markdown = (
            f"# {guide_name}\n\n"
            f"教程文件 `docs/feature_guides/{guide_name}.md` 尚未撰写。\n\n"
            "欢迎贡献：请在该路径下创建 Markdown 文档。"
        )
        title = guide_name
    else:
        markdown = md_path.read_text(encoding="utf-8")
        # 标题取第一个 # 行
        title = guide_name
        for line in markdown.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

    dlg = TutorialDialog(title, markdown, parent=parent)
    dlg.exec()


# ============================================================
# 公开导出
# ============================================================


__all__ = [
    "SectionCard",
    "EmptyState",
    "ApiKeyInput",
    "ProviderOption",
    "ProviderSelector",
    "WhitelistEditor",
    "WhitelistState",
    "TutorialDialog",
    "TestState",
    "open_feature_guide",
]
