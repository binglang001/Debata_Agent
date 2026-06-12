"""Persona and appearance sections for SettingsPage.

This module is a mechanical split from ``ui.dashboard.settings_page``. Keep
behavior equivalent; do not change persona, emoji, theme UI, or theme-save logic
while moving methods.
"""

from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QRadioButton, QWidget

from ...wizard.components import SectionCard
from ..copy import DASHBOARD_COPY


class SettingsPersonaAppearanceMixin:
    # ============================================================
    # 人格节
    # ============================================================

    def _build_persona_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_persona"],
            subtitle="切换角色请到左侧「角色」页（涉及人格档案复制 / 激活 / 导入导出）。",
        )
        card.add_content(QLabel(f"当前：{self._cfg().persona.active}"))
        return card

    # ============================================================
    # 外观节
    # ============================================================

    def _build_emoji_section(self) -> QWidget:
        from ..emoji_section import EmojiSection

        emoji_dir = self._runtime.paths.EMOJI_DIR if self._runtime and self._runtime.paths else None
        if emoji_dir is None:
            # 占位
            card = SectionCard(title="表情包", subtitle="（运行时未就绪）")
            return card
        return EmojiSection(emoji_dir)

    def _build_appearance_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_appearance"],
            subtitle="主题切换立即生效，并会保存到配置。",
        )
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)

        rb_auto = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_auto"])
        rb_auto.setProperty("theme_value", "auto")
        rb_light = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_light"])
        rb_light.setProperty("theme_value", "light")
        rb_dark = QRadioButton(DASHBOARD_COPY["settings.appearance_theme_dark"])
        rb_dark.setProperty("theme_value", "dark")
        self._theme_group.addButton(rb_auto)
        self._theme_group.addButton(rb_light)
        self._theme_group.addButton(rb_dark)

        rb_auto.toggled.connect(lambda on: on and self._on_theme_rb_changed("auto"))
        rb_light.toggled.connect(lambda on: on and self._on_theme_rb_changed("light"))
        rb_dark.toggled.connect(lambda on: on and self._on_theme_rb_changed("dark"))

        self._current_theme = self._cfg().app.theme
        if self._current_theme == "auto":
            rb_auto.setChecked(True)
        elif self._current_theme == "dark":
            rb_dark.setChecked(True)
        else:
            rb_light.setChecked(True)

        row = QHBoxLayout()
        row.addWidget(rb_auto)
        row.addWidget(rb_light)
        row.addWidget(rb_dark)
        row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        card.add_content(wrap)
        return card

    def _on_theme_rb_changed(self, target: str) -> None:
        if self._suppress_signals:
            return
        if self._cfg().app.theme == target:
            self._current_theme = target
            return
        self._current_theme = target
        self._cfg().app.theme = target
        self._save_now(needs_restart=False, change_desc=f"app.theme={target} (hot)")
        self.theme_changed.emit(target)
