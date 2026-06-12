"""P2/体验修复的轻量回归测试。"""

# ruff: noqa: E402

from __future__ import annotations

import logging
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
QApplication = QtWidgets.QApplication
QWidget = QtWidgets.QWidget
Qt = QtCore.Qt

from app_config.schema import (
    AgentConfig,
    AgentsConfig,
    FeaturesConfig,
    LongTermMemoryConfig,
    NapCatAdapterConfig,
    ProviderConfig,
    RootConfig,
)
from memory.important import ImportantMemoryManager
from memory.rag_store import RagEntry
from ui.dashboard.chats_page import _group_records_by_conversation
from ui.dashboard.logs_page import _format_record
from ui.dashboard.memory_page import MemoryPage
from ui.dashboard.personas_page import PersonasPage
from ui.dashboard.settings_page import SettingsPage
from ui.widgets.window_chrome import _resize_edges_for_local_pos
from ui.wizard.components import ApiKeyInput
from ui.wizard.context import WizardContext
from ui.wizard.step_views.features import _TTSFeatureCard
from ui.wizard.step_views.welcome import WelcomeStepView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _minimal_root_config() -> RootConfig:
    return RootConfig(
        providers={"ds": ProviderConfig(preset="deepseek", api_key_id="ds_key")},
        adapters={"default": NapCatAdapterConfig()},
        agents=AgentsConfig(chat=AgentConfig(provider="ds", model="deepseek-chat")),
    )


def test_chats_group_records_by_metadata_and_legacy_header():
    records = [
        {
            "role": "user",
            "content": "hello",
            "metadata": {
                "messages": [
                    {
                        "scope": "private",
                        "target_id": "10001",
                        "user_id": "10001",
                        "nickname": "Alice",
                    }
                ]
            },
        },
        {"role": "assistant", "content": "hi"},
        {
            "role": "user",
            "content": "【2026-05-27 12:00:00 群聊 20002 Bob(30003) msg_id=9】群消息",
        },
    ]

    grouped = _group_records_by_conversation(records)

    assert grouped[0]["key"] == "group:20002"
    assert grouped[0]["label"] == "群聊 20002"
    assert grouped[1]["key"] == "private:10001"
    assert len(grouped[1]["records"]) == 2


@pytest.mark.asyncio
async def test_important_memory_delete_by_id_is_exact(tmp_path):
    mgr = ImportantMemoryManager(tmp_path / "important.json")
    await mgr.load()
    await mgr.replace_all(
        [
            {"timestamp": "t1", "content": "喜欢红茶"},
            {"timestamp": "t2", "content": "也喜欢红茶蛋糕"},
        ]
    )

    deleted = await mgr.delete_by_id("t1")

    assert deleted is True
    assert [item["timestamp"] for item in mgr.items()] == ["t2"]


def test_log_detail_format_includes_exception():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("tests.demo").makeRecord(
            "tests.demo",
            logging.ERROR,
            __file__,
            1,
            "failed: %s",
            ("x",),
            exc_info=sys.exc_info(),
        )

    text = _format_record(record, single_line=False)

    assert "模块：tests.demo" in text
    assert "RuntimeError: boom" in text


def test_welcome_requires_explicit_path_choice(qapp):
    view = WelcomeStepView(WizardContext())
    errors: list[str] = []
    view.invalid_input.connect(errors.append)

    assert view.save() is False
    assert errors == ["请先选择推荐路径或自定义路径。"]


def test_tts_feature_card_keeps_configured_model_dir(qapp):
    card = _TTSFeatureCard()
    card._model_dir_edit.setText("F:/models/custom-voxcpm2")

    state = card.state()

    assert state["model_dir"] == "F:/models/custom-voxcpm2"


def test_tts_feature_card_local_reference_audio_is_optional(qapp, monkeypatch):
    import ui.wizard.step_views.features as features_module

    card = _TTSFeatureCard()
    card._check.setChecked(True)
    card._type_combo.setCurrentIndex(card._type_combo.findData("local"))
    card._ref_audio_edit.clear()
    card._prompt_edit.setText("年轻女性，温柔语气")
    monkeypatch.setattr(features_module, "_directory_has_files", lambda _path: True)

    assert card.ensure_ready(card) is True
    assert card.state()["reference_audio"] == ""


def test_api_key_input_progress_slot_keeps_layout_height(qapp):
    widget = ApiKeyInput(allow_empty_test=True)
    try:
        widget.ensurePolished()
        widget.adjustSize()
        idle_hint = widget.sizeHint().height()

        widget.set_test_state("testing")
        widget.adjustSize()
        testing_hint = widget.sizeHint().height()

        widget.set_test_state("success", "ok")
        widget.adjustSize()
        success_hint = widget.sizeHint().height()
    finally:
        widget.deleteLater()

    assert testing_hint == idle_hint
    assert success_hint == idle_hint


def test_settings_restore_opened_config_writes_snapshot(tmp_paths):
    cfg = _minimal_root_config()
    opened = cfg.model_copy(deep=True)
    cfg.app.theme = "dark"

    class FakeStatus:
        def __init__(self):
            self.calls = []
            self.error = ""

        def set_changes(self, count: int, *, needs_restart: bool) -> None:
            self.calls.append((count, needs_restart))

        def mark_error(self, msg: str) -> None:
            self.error = msg

    class Dummy:
        def __init__(self):
            self._opened_snapshot = opened
            self._runtime = type("RuntimeStub", (), {"paths": tmp_paths, "config": cfg})()
            self._baseline = cfg.model_copy(deep=True)
            self._status = FakeStatus()
            self.refreshed = False

        def refresh(self) -> None:
            self.refreshed = True

    page = Dummy()
    SettingsPage._restore_opened_config(page)

    assert page._runtime.config.app.theme == "auto"
    assert page._status.calls[-1] == (0, True)
    assert page.refreshed is True


def test_memory_page_rag_mode_shows_index_view(qapp):
    class FakeImportant:
        def items(self):
            return [{"timestamp": "t1", "content": "用户喜欢红茶"}]

    class FakeRagStore:
        def all_entries(self):
            return [
                RagEntry(
                    id="t1",
                    text="用户喜欢红茶",
                    vector=[0.1, 0.2],
                    meta={"timestamp": "t1"},
                )
            ]

    cfg = _minimal_root_config()
    cfg.features = FeaturesConfig(
        long_term_memory=LongTermMemoryConfig(mode="rag")
    )
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "important": FakeImportant(),
            "rag_store": FakeRagStore(),
            "embedding_service": object(),
        },
    )()
    page = MemoryPage(runtime)
    try:
        page.refresh()

        assert page._title.text() == "RAG 记忆索引"
        assert "索引 1 条" in page._rag_status.text()
        assert page._list.count() == 1
        assert page._add_row_widget.isHidden()
        assert page._action_row_widget.isHidden()
    finally:
        page.deleteLater()


def test_personas_page_can_build_and_save_generated_persona(qapp, tmp_path):
    from agents.persona_gen_agent import PersonaBrief

    cfg = _minimal_root_config()
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
        assert page._create_btn.text() == "AI 生成角色"
        context = page._build_creator_context()
        assert context is not None
        assert context.main.api_key == "sk-test"
        assert context.main.model == "deepseek-chat"

        context.persona.source = "create"
        context.persona.active = "Mika"
        context.persona.generated_xml = "<identity>你是 Mika</identity>"
        context.persona.brief = PersonaBrief(name="Mika", admin_name="Lily", admin_qq="123456")
        context.admin_name = "Lily"
        context.admin_qq = "123456"

        assert page._save_generated_persona(context) == "Mika"
        saved = personas_dir / "Mika" / "persona_prompt.py"
        assert saved.exists()
        text = saved.read_text(encoding="utf-8")
        assert "<identity>你是 Mika</identity>" in text
        assert '"qq": 123456' in text
    finally:
        page.deleteLater()


def test_window_resize_edges_cover_all_sides(qapp):
    window = QWidget()
    try:
        window.resize(300, 200)

        assert _resize_edges_for_local_pos(window, QtCore.QPoint(0, 100)) & Qt.Edge.LeftEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(299, 100)) & Qt.Edge.RightEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(150, 0)) & Qt.Edge.TopEdge
        assert _resize_edges_for_local_pos(window, QtCore.QPoint(150, 199)) & Qt.Edge.BottomEdge
        top_left = _resize_edges_for_local_pos(window, QtCore.QPoint(0, 0))
        assert top_left & Qt.Edge.LeftEdge
        assert top_left & Qt.Edge.TopEdge
    finally:
        window.deleteLater()
