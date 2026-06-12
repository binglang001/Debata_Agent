"""角色页 —— 列表 / 新建 / 激活 / 复制 / 删除 / 导入 / 导出。"""

from __future__ import annotations

import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
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

from agents.persona_gen_agent import PersonaBrief, PersonaGenResult, render_persona_file
from agents.persona_import import (
    PersonaImportError,
    copy_persona_dir,
    import_persona_zip,
)
from agents.persona_loader import validate_persona_name
from ui.wizard.context import WizardContext
from ui.wizard.persona_render import (
    admin_entries as _admin_entries,
)
from ui.wizard.persona_render import (
    admin_entries_from_brief as _admin_entries_from_brief,
)
from ui.wizard.persona_render import (
    render_minimal_persona as _render_minimal_persona,
)

from ..theme import Spacing
from ..widgets import show_message
from .copy import DASHBOARD_COPY
from .persona_dialogs import _PersonaCreatorDialog, _PersonaRefineDialog

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

        self._create_btn = QPushButton(DASHBOARD_COPY["personas.add_button"])
        self._create_btn.setProperty("role", "primary")
        self._create_btn.clicked.connect(self._on_create_persona)
        actions.addWidget(self._create_btn)

        self._activate_btn = QPushButton(DASHBOARD_COPY["personas.activate_button"])
        self._activate_btn.setProperty("role", "secondary")
        self._activate_btn.clicked.connect(self._on_activate)
        actions.addWidget(self._activate_btn)

        self._duplicate_btn = QPushButton(DASHBOARD_COPY["personas.duplicate_button"])
        self._duplicate_btn.setProperty("role", "secondary")
        self._duplicate_btn.clicked.connect(self._on_duplicate)
        actions.addWidget(self._duplicate_btn)

        self._modify_btn = QPushButton("修改")
        self._modify_btn.setProperty("role", "secondary")
        self._modify_btn.clicked.connect(self._on_modify)
        actions.addWidget(self._modify_btn)

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
        selected = self._selected()
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
        target = selected or self._active_name
        if target:
            self._select_name(target)
        elif self._list.count() > 0:
            self._list.setCurrentRow(0)
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
        self._create_btn.setEnabled(self._runtime is not None)
        self._activate_btn.setEnabled(has and name != self._active_name)
        self._duplicate_btn.setEnabled(has)
        self._modify_btn.setEnabled(has)
        self._export_btn.setEnabled(has)
        # 内置 / 当前激活不允许删除
        self._delete_btn.setEnabled(
            has and name != self._active_name and name not in _BUILTIN
        )

    # ---- 动作 ----

    def _on_create_persona(self) -> None:
        context = self._build_creator_context()
        if context is None:
            return
        dlg = _PersonaCreatorDialog(context, runtime=self._runtime, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            created = self._save_generated_persona(context)
        except PersonaImportError as e:
            show_message(self, "无法保存", str(e), is_danger=True)
            return
        except Exception as e:
            logger.exception("保存生成人格失败")
            show_message(self, "未能完成", str(e), is_danger=True)
            return
        self.refresh()
        self._select_name(created)
        show_message(self, "已创建", f"已创建角色「{created}」。需要使用它时，点「切换为当前」。")

    def _build_creator_context(self) -> WizardContext | None:
        if self._runtime is None or self._runtime.config is None:
            show_message(self, "运行时未就绪", "暂时无法读取模型配置。", is_danger=True)
            return None
        cfg = self._runtime.config
        agent_cfg = cfg.agents.persona_gen or cfg.agents.chat
        provider_cfg = cfg.providers.get(agent_cfg.provider)
        if provider_cfg is None:
            show_message(
                self,
                "模型配置有误",
                f"人格生成使用的 provider「{agent_cfg.provider}」不存在。",
                is_danger=True,
            )
            return None
        api_key = ""
        if provider_cfg.api_key_id and self._runtime.secrets is not None:
            api_key = self._runtime.secrets.get(provider_cfg.api_key_id) or ""
        if not api_key:
            show_message(
                self,
                "缺少密钥",
                "人格生成需要可用的模型 API 密钥。请先在设置页补齐 provider 密钥。",
                is_danger=True,
            )
            return None

        from ui.wizard.step_views.main_model_custom import _PRESET_DEFAULTS

        preset = provider_cfg.preset or "custom"
        preset_info = _PRESET_DEFAULTS.get(preset, {})
        protocol_raw = provider_cfg.protocol or preset_info.get("protocol") or "openai_compat"
        protocol = "anthropic" if protocol_raw == "anthropic" else "openai_compat"

        context = WizardContext()
        context.main.preset = preset
        context.main.display_name = provider_cfg.display_name or preset_info.get("display") or agent_cfg.provider
        context.main.api_key = api_key
        context.main.model = agent_cfg.model
        context.main.base_url = provider_cfg.base_url or preset_info.get("url", "")
        context.main.protocol = protocol
        context.main.temperature = agent_cfg.temperature
        context.main.top_p = agent_cfg.top_p
        context.main.max_tokens = agent_cfg.max_tokens
        if agent_cfg.reasoning and agent_cfg.reasoning.enabled:
            context.main.reasoning_enabled = True
            context.main.reasoning_budget = agent_cfg.reasoning.budget
            context.main.reasoning_max_tokens = agent_cfg.reasoning.max_tokens
        return context

    def _save_generated_persona(self, context: WizardContext) -> str:
        p = context.persona
        if p.source != "create" or not p.generated_xml:
            raise PersonaImportError("还没有可保存的人格内容")
        validate_persona_name(p.active)
        target = self._personas_dir / p.active
        if target.exists():
            raise PersonaImportError(f"角色「{p.active}」已存在，请换一个名字")
        target.mkdir(parents=True, exist_ok=True)
        (target / "__init__.py").touch(exist_ok=True)
        result = PersonaGenResult(persona_prompt=p.generated_xml, display_name=p.active)
        admins = _admin_entries_from_brief(p.brief) or _admin_entries(context.admin_qq, context.admin_name)
        file_text = (
            render_persona_file(result, p.brief, admins=admins)
            if p.brief
            else _render_minimal_persona(p.active, p.generated_xml, admins=admins)
        )
        (target / "persona_prompt.py").write_text(file_text, encoding="utf-8")
        return p.active

    def _on_modify(self) -> None:
        name = self._selected()
        if not name:
            return
        context = self._build_creator_context()
        if context is None:
            return
        persona_file = self._personas_dir / name / "persona_prompt.py"
        try:
            prompt = _extract_persona_prompt(persona_file)
        except Exception as e:  # noqa: BLE001
            show_message(self, "无法读取", str(e), is_danger=True)
            return
        dlg = _PersonaRefineDialog(name, prompt, context, runtime=self._runtime, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.result_prompt
        if not updated.strip():
            return
        if not show_message(
            self,
            "确认覆盖？",
            f"将覆盖角色「{name}」的人格文件。覆盖前会自动备份原文件。",
            confirm_text="覆盖",
            cancel_text="取消",
            is_danger=True,
        ):
            return
        try:
            self._backup_persona_file(persona_file)
            brief = PersonaBrief(name=name)
            result = PersonaGenResult(persona_prompt=updated, display_name=name)
            admins = _read_existing_admins(persona_file)
            gender = _read_existing_gender(persona_file)
            if gender:
                brief.gender = gender
            persona_file.write_text(render_persona_file(result, brief, admins=admins), encoding="utf-8")
            show_message(self, "已修改", f"已覆盖角色「{name}」。重启 Debata 后生效。")
            self.refresh()
            self._select_name(name)
        except Exception as e:  # noqa: BLE001
            logger.exception("修改人格失败")
            show_message(self, "未能完成", str(e), is_danger=True)

    def _backup_persona_file(self, persona_file: Path) -> Path:
        backup_dir = persona_file.parent.parent / ".backups" / persona_file.parent.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"persona_prompt_{ts}.py"
        shutil.copy2(persona_file, target)
        return target

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


def _extract_persona_prompt(persona_file: Path) -> str:
    module = _load_persona_module(persona_file)
    prompt = getattr(module, "PERSONA_PROMPT", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{persona_file} 缺少 PERSONA_PROMPT")
    return prompt.strip()


def _read_existing_admins(persona_file: Path) -> list[dict[str, object]]:
    module = _load_persona_module(persona_file)
    vars_obj = getattr(module, "PERSONA_VARS", {})
    if not isinstance(vars_obj, dict):
        return []
    admins = vars_obj.get("admins", [])
    return list(admins) if isinstance(admins, list) else []


def _read_existing_gender(persona_file: Path) -> str:
    module = _load_persona_module(persona_file)
    vars_obj = getattr(module, "PERSONA_VARS", {})
    if not isinstance(vars_obj, dict):
        return ""
    gender = vars_obj.get("gender", "")
    return str(gender) if gender else ""


def _load_persona_module(persona_file: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"_dashboard_persona_{persona_file.parent.name}", persona_file)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载 {persona_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

