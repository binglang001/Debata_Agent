"""角色管理页回归测试。"""

from __future__ import annotations

from agents.persona_gen_agent import PersonaBrief
from app_config.schema import ReasoningConfig
from tests.dashboard.helpers import minimal_root_config
from ui.dashboard.personas_page import PersonasPage, _PersonaCreatorDialog
from ui.wizard.context import WizardContext


def test_personas_page_can_build_and_save_generated_persona(qapp, tmp_path):
    cfg = minimal_root_config()
    cfg.agents.chat.reasoning = ReasoningConfig(enabled=True, budget="high", max_tokens=2048)
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    debata_dir = personas_dir / "debata"
    debata_dir.mkdir()
    (debata_dir / "persona_prompt.py").write_text("PERSONA_PROMPT = 'x'\n", encoding="utf-8")

    class FakeSecrets:
        def get(self, key_id: str) -> str | None:
            return "sk-test" if key_id == "ds_key" else None

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "secrets": FakeSecrets(),
            "paths": type("Paths", (), {"PERSONAS_DIR": personas_dir})(),
        },
    )()
    page = PersonasPage(runtime)
    try:
        assert page._create_btn.text() == "新建角色"
        context = page._build_creator_context()
        assert context is not None
        assert context.main.api_key == "sk-test"
        assert context.main.model == "deepseek-chat"
        assert context.main.reasoning_enabled is True
        assert context.main.reasoning_budget == "high"
        assert context.main.reasoning_max_tokens == 2048

        context.persona.source = "create"
        context.persona.active = "Mika"
        context.persona.generated_xml = "<identity>你是 Mika</identity>"
        context.persona.brief = PersonaBrief(
            name="Mika",
            gender="female",
            age=18,
            admins=[
                {"name": "Lily", "qq": "123456", "relation": "创作者"},
                {"name": "Robin", "qq": "654321", "relation": "朋友"},
            ],
        )
        context.admin_name = "Lily"
        context.admin_qq = "123456"

        assert page._save_generated_persona(context) == "Mika"
        saved = personas_dir / "Mika" / "persona_prompt.py"
        assert saved.exists()
        text = saved.read_text(encoding="utf-8")
        assert "<identity>你是 Mika</identity>" in text
        assert "'qq': 123456" in text
        assert "'qq': 654321" in text
        assert "'gender': 'female'" in text
        assert "'age': 18" in text
    finally:
        page.deleteLater()


def test_persona_creator_dialog_wires_runtime_usage_callbacks(qapp):
    context = WizardContext()

    class RuntimeStub:
        async def _record_model_usage(self, usage, metadata):
            return None

        def _update_model_activity(self, payload):
            return None

    runtime = RuntimeStub()
    dlg = _PersonaCreatorDialog(context, runtime=runtime)
    try:
        assert dlg._creator.usage_recorder == runtime._record_model_usage
        assert dlg._creator.status_callback == runtime._update_model_activity
    finally:
        dlg.deleteLater()


def test_personas_page_selects_active_persona_on_refresh(qapp, tmp_path):
    cfg = minimal_root_config()
    cfg.persona.active = "Mika"
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    for name in ("debata", "Mika"):
        d = personas_dir / name
        d.mkdir()
        (d / "persona_prompt.py").write_text("PERSONA_PROMPT = 'x'\n", encoding="utf-8")

    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "secrets": None,
            "paths": type("Paths", (), {"PERSONAS_DIR": personas_dir})(),
        },
    )()
    page = PersonasPage(runtime)
    try:
        page.refresh()
        assert page._selected() == "Mika"
    finally:
        page.deleteLater()
