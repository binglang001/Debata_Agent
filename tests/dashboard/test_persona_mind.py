"""人格后台页回归测试。"""

# ruff: noqa: E402, I001

from __future__ import annotations

from types import SimpleNamespace

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from ui.dashboard.persona_mind_page import PersonaMindPage

from tests.dashboard.helpers import (
    dashboard_runtime,
    minimal_root_config,
    pump_dashboard_events,
    wait_for_dashboard_condition,
)


class _FakePersonaAgent:
    physiology_energy_mode = "tool"
    physiology_satiety_mode = "tool"

    def get_state_snapshot(self):
        return SimpleNamespace(
            mood="安稳",
            social_need="想聊两句",
            energy=72.5,
            satiety=66,
            current_action="整理最近的关系线索",
            latest_monologue="今天适合慢慢把线索归档。",
            last_tick_at="2026-06-12 10:00:00",
        )


class _FakePersonaDb:
    async def get_active_effects(self, now):
        return [{"title": "短期影响", "content": "午后精神更平稳"}]

    async def get_todos(self, include_completed=False):
        assert include_completed is False
        return [{"title": "提醒事项", "content": "晚上提醒用户喝水"}]

    async def get_cues(self, now):
        return [{"text": "用户提到周末茶会"}]

    async def all_profiles(self):
        return [
            {
                "user_id": "10001",
                "display_name": "Alice",
                "summary": "喜欢红茶和安静聊天",
                "traits": ["慢热", "重视承诺"],
                "affinity": 0.86,
                "interaction_count": 12,
                "last_interaction_at": 1_780_000_001.0,
            },
            {"user_id": "10002"},
        ]

    async def recent_monologues(self, limit=20):
        return [{"content": "把今天的情绪波动记下来"}]

    async def recent_trajectories(self, limit=20):
        return [{"summary": "关系从陌生转向熟悉"}]

    async def recent_state_logs(self, limit=50):
        return [{"mood": "安稳", "action": "整理状态日志"}]

    async def recent_sleep_records(self, limit=20):
        return [{"content": "午睡 20 分钟"}]

    async def recent_eat_records(self, limit=20):
        return [{"content": "吃了三明治"}]

    async def recent_arc_events(self, limit=20):
        return [{"content": "完成一次人格弧线整理"}]


class _PartialFailingPersonaDb:
    async def get_active_effects(self, now):
        raise RuntimeError("effects failed")

    async def get_todos(self, include_completed=False):
        return [{"content": "保留可用待办"}]

    async def get_cues(self, now):
        return [{"text": "保留可用线索"}]

    async def all_profiles(self):
        return [{"name": "Bob"}]

    async def recent_monologues(self, limit=20):
        return []

    async def recent_trajectories(self, limit=20):
        return []

    async def recent_state_logs(self, limit=50):
        raise RuntimeError("state logs failed")

    async def recent_sleep_records(self, limit=20):
        return [{"content": "睡眠记录仍可显示"}]

    async def recent_eat_records(self, limit=20):
        return [{"content": "进食记录仍可显示"}]

    async def recent_arc_events(self, limit=20):
        raise RuntimeError("arc failed")


def test_persona_mind_page_disabled_or_missing_runtime_shows_empty(qapp, tmp_paths):
    page = PersonaMindPage(dashboard_runtime(tmp_paths))
    try:
        page._timer.stop()
        page.refresh()
        qapp.processEvents()

        assert not page._empty.isHidden()
        assert page._content.isHidden()
        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "人格后台未启用" in labels
    finally:
        page.deleteLater()


@pytest.mark.asyncio
async def test_persona_mind_page_refresh_shows_fake_agent_and_db_data(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.persona_management.enabled = True
    cfg.persona.active = "debata"
    cfg.persona_management.age.overrides["debata"] = 19
    runtime = dashboard_runtime(tmp_paths, cfg)
    runtime.persona = SimpleNamespace(name="Debata", get_age=lambda: 18)
    runtime.persona_agent = _FakePersonaAgent()
    runtime.persona_db = _FakePersonaDb()
    runtime.model_activity = {
        "state": "thinking",
        "text": "后台整理中",
        "agent": "人格后台",
        "model": "deepseek-chat",
    }
    page = PersonaMindPage(runtime)
    try:
        page._timer.stop()
        page.refresh()
        await wait_for_dashboard_condition(
            qapp,
            lambda: any(
                "晚上提醒用户喝水" in page._todos_cues.item(i).text()
                for i in range(page._todos_cues.count())
            ),
        )

        text = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        list_text = "\n".join(
            widget.item(i).text()
            for widget in (
                page._state_logs,
                page._effects,
                page._todos_cues,
                page._profiles,
                page._sleep_eat,
                page._arc,
            )
            for i in range(widget.count())
        )

        assert "Debata" in text
        assert "已启用" in text
        assert "覆盖年龄 19 岁" in text
        assert "后台整理中" in text
        assert "安稳" in text
        assert "今天适合慢慢把线索归档。" in text
        assert "72.5" in text
        assert "晚上提醒用户喝水" in list_text
        assert "用户提到周末茶会" in list_text
        assert "整理状态日志" in list_text
        assert "午睡 20 分钟" in list_text
        assert "完成一次人格弧线整理" in list_text

        profile_text = "\n".join(
            page._profiles.item(i).text()
            for i in range(page._profiles.count())
        )
        assert "用户: 10001" in profile_text
        assert "显示名: Alice" in profile_text
        assert "摘要: 喜欢红茶和安静聊天" in profile_text
        assert "特征: 慢热、重视承诺" in profile_text
        assert "亲近度: 0.86" in profile_text
        assert "互动次数: 12" in profile_text
        assert "最近互动: 2026-" in profile_text
        assert "用户: 10002" in profile_text
    finally:
        page.deleteLater()


@pytest.mark.asyncio
async def test_persona_mind_page_reads_idle_activity_from_runtime_each_refresh(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.persona_management.enabled = True
    runtime = dashboard_runtime(tmp_paths, cfg)
    runtime.persona_agent = _FakePersonaAgent()
    runtime.persona_db = _FakePersonaDb()
    runtime.model_activity = {"state": "thinking", "text": "后台整理中"}
    page = PersonaMindPage(runtime)
    try:
        page._timer.stop()
        page.refresh()
        await pump_dashboard_events(qapp, rounds=2)

        runtime.model_activity = {"state": "idle", "text": "空闲"}
        page.refresh()
        await pump_dashboard_events(qapp, rounds=2)

        text = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        assert "空闲" in text
        assert "后台整理中" not in text
    finally:
        page.deleteLater()


@pytest.mark.asyncio
async def test_persona_mind_page_partial_db_failure_keeps_realtime_state(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.persona_management.enabled = True
    runtime = dashboard_runtime(tmp_paths, cfg)
    runtime.persona_agent = _FakePersonaAgent()
    runtime.persona_db = _PartialFailingPersonaDb()
    runtime.model_activity = {"state": "idle", "text": "空闲"}
    page = PersonaMindPage(runtime)
    try:
        page._timer.stop()
        page.refresh()
        await wait_for_dashboard_condition(
            qapp,
            lambda: any(
                "保留可用待办" in page._todos_cues.item(i).text()
                for i in range(page._todos_cues.count())
            ),
        )

        labels = "\n".join(label.text() for label in page.findChildren(QtWidgets.QLabel))
        list_text = "\n".join(
            widget.item(i).text()
            for widget in (
                page._state_logs,
                page._effects,
                page._todos_cues,
                page._profiles,
                page._sleep_eat,
                page._arc,
            )
            for i in range(widget.count())
        )

        assert "安稳" in labels
        assert "空闲" in labels
        assert "暂无动向" in list_text
        assert "暂无短期影响" in list_text
        assert "保留可用待办" in list_text
        assert "保留可用线索" in list_text
        assert "睡眠记录仍可显示" in list_text
        assert "进食记录仍可显示" in list_text
        assert "读取失败" not in list_text
    finally:
        page.deleteLater()


def test_persona_mind_page_state_log_created_at_is_human_readable(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.persona_management.enabled = True
    runtime = dashboard_runtime(tmp_paths, cfg)
    runtime.persona_agent = _FakePersonaAgent()
    runtime.persona_db = _FakePersonaDb()
    page = PersonaMindPage(runtime)
    try:
        page._timer.stop()
        page._render_db_data({"state_logs": [{"created_at": 1_780_000_001.0}]})

        text = page._state_logs.item(0).text()
        assert "created_at: 178" not in text
        assert "时间: 2026-" in text
    finally:
        page.deleteLater()


def test_persona_mind_page_formats_after_turn_state_log_summary(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.persona_management.enabled = True
    runtime = dashboard_runtime(tmp_paths, cfg)
    runtime.persona_agent = _FakePersonaAgent()
    runtime.persona_db = _FakePersonaDb()
    page = PersonaMindPage(runtime)
    try:
        page._timer.stop()
        page._render_db_data(
            {
                "state_logs": [
                    {
                        "event": "after_turn",
                        "conversation_id": "private:10001",
                        "reason": "用户刚结束晚间聊天",
                        "created_at": 1_780_000_001.0,
                        "state": {
                            "mood": "安稳",
                            "social_need": "想聊两句",
                            "energy": 72.5,
                            "satiety": 66,
                            "current_action": "整理最近的关系线索",
                            "latest_monologue": "今天适合慢慢把线索归档。",
                            "last_tick_at": 1_780_000_000.0,
                            "large_payload": {"raw": ["不应原样展开"]},
                        },
                    }
                ]
            }
        )

        text = page._state_logs.item(0).text()
        assert "对话后状态" in text
        assert "2026-" in text
        assert "对话: private:10001" in text
        assert "原因: 用户刚结束晚间聊天" in text
        assert "心情: 安稳" in text
        assert "社交需求: 想聊两句" in text
        assert "精力: 72.5" in text
        assert "饱腹: 66" in text
        assert "最新独白: 今天适合慢慢把线索归档。" in text
        assert "large_payload" not in text
        assert "{'raw'" not in text
    finally:
        page.deleteLater()


def test_persona_mind_page_state_log_shows_existing_source(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.persona_management.enabled = True
    runtime = dashboard_runtime(tmp_paths, cfg)
    runtime.persona_agent = _FakePersonaAgent()
    runtime.persona_db = _FakePersonaDb()
    page = PersonaMindPage(runtime)
    try:
        page._timer.stop()
        page._render_db_data(
            {
                "state_logs": [
                    {
                        "event": "periodic_tick",
                        "created_at": 1_780_000_001.0,
                        "metadata": {"source": "offline_reconcile"},
                        "state": {"energy": 42},
                    },
                    {
                        "event": "wakeup",
                        "created_at": 1_780_000_002.0,
                        "reason": {"source": "sleep_record", "reason": "自然醒"},
                    },
                ]
            }
        )

        first = page._state_logs.item(0).text()
        second = page._state_logs.item(1).text()
        assert "来源: offline_reconcile" in first
        assert "来源: sleep_record" in second
        assert "原因: 自然醒" in second
    finally:
        page.deleteLater()


def test_persona_mind_page_formats_real_lifecycle_state_log(qapp, tmp_paths):
    cfg = minimal_root_config()
    cfg.persona_management.enabled = True
    runtime = dashboard_runtime(tmp_paths, cfg)
    runtime.persona_agent = _FakePersonaAgent()
    runtime.persona_db = _FakePersonaDb()
    page = PersonaMindPage(runtime)
    try:
        page._timer.stop()
        page._render_db_data(
            {
                "state_logs": [
                    {
                        "event": "sleep_start",
                        "created_at": 1_780_000_001.0,
                        "previous_action": "整理最近的关系线索",
                        "duration_minutes": 90,
                        "sleep_type": "nap",
                        "record_id": "sleep-1",
                        "description": "午后小睡",
                        "state": {"energy": 18, "current_action": "睡觉"},
                    }
                ]
            }
        )

        text = page._state_logs.item(0).text()
        assert "开始睡眠" in text
        assert "sleep_start" not in text
        assert "持续时间: 90" in text
        assert "上一动作: 整理最近的关系线索" in text
        assert "睡眠类型: nap" in text
        assert "描述: 午后小睡" in text
        assert "精力: 18" in text
    finally:
        page.deleteLater()
