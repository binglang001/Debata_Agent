"""人格选择 —— 三选一：内置 / 创造 / 导入。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..components import SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ...theme import Spacing


def _list_builtin_personas() -> list[str]:
    """列出 personas/ 下的目录名（不含 __init__ / __pycache__）。"""
    root = Path(__file__).resolve().parent.parent.parent.parent / "personas"
    if not root.exists():
        return ["diana"]
    out = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        name = p.name
        if name.startswith("_") or name.startswith("."):
            continue
        if (p / "persona_prompt.py").exists():
            out.append(name)
    return out or ["diana"]


class PersonaStepView(BaseStepView):
    """人格来源三选一。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="赋予一个角色",
            subtitle=(
                "Diana 不是单一角色——你给它什么人格，它就活成谁。\n"
                "可以用内置的示范角色快速开始，或者花十分钟创造一个属于你的。"
            ),
        )
        outer.addWidget(card)

        self._source_group = QButtonGroup(self)
        self._source_group.setExclusive(True)

        self._rb_builtin = self._mk_source_card(
            "builtin",
            COPY["persona.source_repo"],
            COPY["persona.source_repo_desc"],
        )
        self._rb_create = self._mk_source_card(
            "create",
            COPY["persona.source_create"],
            COPY["persona.source_create_desc"],
        )
        self._rb_import = self._mk_source_card(
            "import",
            COPY["persona.source_import"],
            COPY["persona.source_import_desc"],
        )

        card.add_content(self._rb_builtin)
        card.add_content(self._rb_create)
        card.add_content(self._rb_import)

        # builtin 选项：下拉
        self._builtin_combo = QComboBox()
        for name in _list_builtin_personas():
            self._builtin_combo.addItem(name, name)
        card.add_content(self._wrap_field("选哪一个", self._builtin_combo))

        # import：路径
        import_row = QHBoxLayout()
        self._import_path = QLineEdit()
        self._import_path.setPlaceholderText("persona_prompt.py 所在目录")
        import_row.addWidget(self._import_path, 1)
        browse_btn = QPushButton("浏览")
        browse_btn.setProperty("role", "secondary")
        browse_btn.clicked.connect(self._browse_persona)
        import_row.addWidget(browse_btn)
        self._import_widget = QWidget()
        self._import_widget.setLayout(import_row)
        card.add_content(self._wrap_field("导入路径", self._import_widget))

        # admin QQ（可选）
        sep = QFrame()
        sep.setProperty("role", "separator")
        card.add_content(sep)

        self._admin_edit = QLineEdit()
        self._admin_edit.setPlaceholderText("可选 —— 用于审核陌生人加好友/加群")
        card.add_content(self._wrap_field("管理员 QQ", self._admin_edit))

        outer.addStretch(1)

        self._rb_builtin.findChildren(QRadioButton)[0].setChecked(True)
        self._update_visibility()

        # 选项变化时更新显隐
        for rb in self._source_radios():
            rb.toggled.connect(self._update_visibility)

    def _wrap_field(self, label: str, w: QWidget) -> QWidget:
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(24, 0, 0, 0)
        lay.setSpacing(Spacing.MD)
        lbl = QLabel(label)
        lbl.setProperty("role", "secondary")
        lbl.setFixedWidth(80)
        lay.addWidget(lbl)
        lay.addWidget(w, 1)
        return wrap

    def _mk_source_card(self, value: str, title: str, desc: str) -> QFrame:
        wrapper = QFrame()
        wrapper.setObjectName("Card")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        rb = QRadioButton(title)
        rb.setProperty("source_value", value)
        self._source_group.addButton(rb)
        wl.addWidget(rb)
        d = QLabel(desc)
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 0)
        wl.addWidget(d)
        return wrapper

    def _source_radios(self) -> list[QRadioButton]:
        out: list[QRadioButton] = []
        for w in (self._rb_builtin, self._rb_create, self._rb_import):
            for child in w.findChildren(QRadioButton):
                out.append(child)
        return out

    def _current_source(self) -> str:
        for rb in self._source_radios():
            if rb.isChecked():
                return rb.property("source_value") or "builtin"
        return "builtin"

    def _update_visibility(self) -> None:
        src = self._current_source()
        # builtin 下拉只在 builtin 模式可见
        self._builtin_combo.parent().setVisible(src == "builtin")
        self._import_widget.parent().setVisible(src == "import")

    def _browse_persona(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择人格目录")
        if path:
            self._import_path.setText(path)

    def refresh(self) -> None:
        p = self.context.persona
        for rb in self._source_radios():
            rb.setChecked(rb.property("source_value") == p.source)
        idx = self._builtin_combo.findData(p.active)
        if idx >= 0:
            self._builtin_combo.setCurrentIndex(idx)
        self._import_path.setText(p.import_path)
        self._admin_edit.setText(self.context.admin_qq)
        self._update_visibility()

    def save(self) -> bool:
        src = self._current_source()
        p = self.context.persona
        p.source = src  # type: ignore[assignment]

        if src == "builtin":
            p.active = self._builtin_combo.currentData() or "diana"
        elif src == "import":
            path = self._import_path.text().strip()
            if not path or not Path(path).is_dir():
                self.invalid_input.emit("请选择一个存在的人格目录")
                return False
            if not (Path(path) / "persona_prompt.py").exists():
                self.invalid_input.emit("该目录下没有 persona_prompt.py")
                return False
            p.import_path = path
            p.active = Path(path).name
        # create 模式：active 和 brief 在 PERSONA_CREATE 子流程里填

        admin = self._admin_edit.text().strip()
        if admin and not admin.isdigit():
            self.invalid_input.emit("管理员 QQ 应该是纯数字")
            return False
        self.context.admin_qq = admin
        return True
