"""完成总览 —— 列出所有选择，确认后启动。"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ..components import SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ...theme import Spacing


class SummaryStepView(BaseStepView):
    """配置摘要展示。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(title="确认一下", subtitle=COPY["summary.intro"])
        outer.addWidget(card)

        self._summary_layout = QVBoxLayout()
        self._summary_layout.setSpacing(Spacing.SM)
        card.add_layout(self._summary_layout)

        hint = QLabel(COPY["summary.adjust_later_hint"])
        hint.setProperty("role", "secondary")
        card.add_content(hint)

        outer.addStretch(1)

    def _clear_summary(self) -> None:
        while self._summary_layout.count():
            item = self._summary_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            else:
                lay = item.layout()
                if lay:
                    self._clear_layout(lay)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _section(self, title: str, lines: list[tuple[str, str]]) -> QFrame:
        wrapper = QFrame()
        wrapper.setObjectName("Card")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        wl.setSpacing(Spacing.XS)

        t = QLabel(title)
        t.setProperty("role", "title-3")
        wl.addWidget(t)

        for key, val in lines:
            row = QHBoxLayout()
            row.setSpacing(Spacing.MD)
            k = QLabel(key)
            k.setProperty("role", "secondary")
            k.setFixedWidth(120)
            row.addWidget(k)
            v = QLabel(val)
            v.setWordWrap(True)
            row.addWidget(v, 1)
            wl.addLayout(row)

        return wrapper

    def refresh(self) -> None:
        self._clear_summary()
        c = self.context

        # 模型
        model_lines = [
            ("提供商", c.main.display_name),
            ("模型", c.main.model),
            ("温度", f"{c.main.temperature}"),
        ]
        if c.main.reasoning_enabled:
            model_lines.append(("推理", "开"))
        if c.path == "custom":
            model_lines.append(
                ("主动思考",
                 "禁用" if not c.proactive.enabled
                 else ("同主模型" if c.proactive.use_main else f"{c.proactive.preset} / {c.proactive.model}"))
            )
            model_lines.append(
                ("历史总结",
                 "禁用" if not c.summary.enabled
                 else ("同主模型" if c.summary.use_main else f"{c.summary.preset} / {c.summary.model}"))
            )
        self._summary_layout.addWidget(self._section(COPY["summary.section_model"], model_lines))

        # 功能
        feat_lines = []
        for label, choice in [
            ("看懂图片", c.vision),
            ("听懂语音", c.asr),
            ("用声音说话", c.tts),
            ("查天气", c.weather),
            ("联网搜索", c.web_search),
        ]:
            feat_lines.append((label, "开" if choice.enabled else "—"))
        feat_lines.append(
            ("长期记忆", "向量模式" if c.long_term_memory_mode == "rag" else "文件模式")
        )
        self._summary_layout.addWidget(self._section(COPY["summary.section_features"], feat_lines))

        # 渠道
        adapter_lines = [
            ("连接方式", "Diana 连过去" if c.adapter.mode == "client" else "NapCat 连过来"),
            ("地址", f"{c.adapter.host}:{c.adapter.port}{c.adapter.path}"),
            ("白名单", {"open": "对所有人开放", "verify": "管理员审核", "whitelist": "白名单"}[c.adapter.whitelist.mode]),
        ]
        if c.adapter.manage_process:
            adapter_lines.append(("托管进程", c.adapter.process_path))
        self._summary_layout.addWidget(self._section(COPY["summary.section_adapter"], adapter_lines))

        # 角色
        persona_lines = [
            ("来源", {"builtin": "仓库自带", "create": "自定义生成", "import": "导入"}[c.persona.source]),
            ("名称", c.persona.active or "(未填)"),
        ]
        if c.admin_qq:
            persona_lines.append(("管理员 QQ", c.admin_qq))
        self._summary_layout.addWidget(self._section(COPY["summary.section_persona"], persona_lines))

    def save(self) -> bool:
        # Summary 不收集字段；window 接到下一步信号时直接做写入操作
        return True
