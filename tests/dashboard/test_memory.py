"""记忆页回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

import pytest

QtCore = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
Qt = QtCore.Qt

from app_config.schema import FeaturesConfig, LongTermMemoryConfig
from memory.important import ImportantMemoryManager
from memory.rag_store import RagEntry
from ui.dashboard.memory_page import MemoryPage

from tests.dashboard.helpers import minimal_root_config, wait_for_dashboard_condition


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


def test_memory_page_rag_mode_keeps_important_memory_tab(qapp):
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

    cfg = minimal_root_config()
    cfg.features = FeaturesConfig(long_term_memory=LongTermMemoryConfig(mode="rag"))
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

        assert not page._tabs.isHidden()
        assert page._tabs.tabText(0) == "重要记忆"
        assert page._tabs.tabText(1) == "RAG 历史索引"
        assert page._title.text() == "重要记忆"
        assert "重要记忆始终启用" in page._rag_status.text()
        assert page._list.count() == 1
        assert "用户喜欢红茶" in page._list.item(0).text()
        assert not page._add_row_widget.isHidden()
        assert not page._action_row_widget.isHidden()

        page._tabs.setCurrentIndex(1)
        qapp.processEvents()

        assert page._title.text() == "RAG 历史向量索引"
        assert "索引 1 条" in page._rag_status.text()
        assert "重要记忆仍单独保存和注入" in page._rag_status.text()
        assert page._list.count() == 1
        assert page._add_row_widget.isHidden()
        assert page._action_row_widget.isHidden()
        assert page._metadata_row_widget.isHidden()
    finally:
        page.deleteLater()


@pytest.mark.asyncio
async def test_memory_page_update_metadata_calls_runtime_and_updates_item(qapp):
    class FakeImportant:
        def __init__(self):
            self.calls = []
            self._items = [
                {
                    "timestamp": "t1",
                    "content": "用户喜欢红茶",
                    "scope": "global",
                    "pinned": False,
                }
            ]

        def items(self):
            return list(self._items)

        async def update_metadata(self, item_id, *, scope=None, pinned=None):
            self.calls.append((item_id, scope, pinned))
            self._items[0]["scope"] = scope
            self._items[0]["pinned"] = pinned
            return True

    cfg = minimal_root_config()
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "important": FakeImportant(),
            "rag_store": None,
            "embedding_service": None,
        },
    )()
    page = MemoryPage(runtime)
    try:
        page._list.setCurrentRow(0)
        page._scope_edit.setText("group:123")
        page._pinned_check.setChecked(True)

        page._on_update_metadata()
        await wait_for_dashboard_condition(
            qapp,
            lambda: runtime.important.calls == [("t1", "group:123", True)],
        )

        item = page._list.currentItem()
        assert item is not None
        data = item.data(Qt.ItemDataRole.UserRole)
        assert data["id"] == "t1"
        assert data["scope"] == "group:123"
        assert data["pinned"] is True
        assert "[group:123 / 置顶]" in item.text()
        assert page._scope_edit.text() == "group:123"
        assert page._pinned_check.isChecked()
        assert page._metadata_btn.text() == "已保存"
    finally:
        page.deleteLater()


def test_memory_page_rag_not_ready_copy_keeps_important_memory_enabled(qapp):
    class FakeImportant:
        def items(self):
            return [{"timestamp": "t1", "content": "用户喜欢红茶"}]

    cfg = minimal_root_config()
    cfg.features = FeaturesConfig(long_term_memory=LongTermMemoryConfig(mode="rag"))
    runtime = type(
        "RuntimeStub",
        (),
        {
            "config": cfg,
            "important": FakeImportant(),
            "rag_store": None,
            "embedding_service": None,
        },
    )()
    page = MemoryPage(runtime)
    try:
        page._tabs.setCurrentIndex(1)
        page.refresh()

        assert "重要记忆仍照常可用" in page._rag_status.text()
        assert "不读写重要记忆文件" not in page._rag_status.text()
    finally:
        page.deleteLater()
