from __future__ import annotations

import pytest

import agents.persona_agent as persona_agent_mod
from mind import Cue, Effect, PersonaState, Todo, UserProfile
from tests.persona_agent.helpers import FakeDB, FakeProvider, _agent


@pytest.mark.asyncio
async def test_after_turn_valid_json_updates_state_and_runtime_records():
    db = FakeDB(PersonaState())
    statuses = []
    provider = FakeProvider(
        [
            """
            {
              "mood": 81,
              "social_need": 22,
              "latest_monologue": "今天记得主动问候。",
              "effect": {
                "name": "安心",
                "effect_type": "mood",
                "intensity": 2.5,
                "prompt_hint": "语气更安心",
                "source_detail": "unit",
                "expires_at": 9999999999
              },
              "profile": {
                "display_name": "小林",
                "affinity": 0.8,
                "summary": "喜欢短句反馈",
                "traits": ["短句"],
                "interaction_count": 3
              },
              "todo": {
                "title": "稍后确认状态",
                "reason": "对方刚提到疲惫",
                "priority": 2,
                "scope": "private:u1"
              },
              "cue": {
                "summary": "对方可能需要休息提醒",
                "expires_at": 9999999999
              }
            }
            """
        ]
    )
    agent = _agent(db, provider=provider, status_callback=statuses.append)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "聊到今天很累")

    snapshot = agent.get_state_snapshot()
    assert snapshot.mood == 81
    assert snapshot.social_need == 22
    assert snapshot.latest_monologue == "今天记得主动问候。"
    assert db.effects[0].prompt_hint == "语气更安心"
    assert db.profiles["u1"].summary == "喜欢短句反馈"
    assert db.todos[0].title == "稍后确认状态"
    assert db.cues[0].conversation_id == "private:u1"
    assert db.monologues[0]["text"] == "今天记得主动问候。"
    assert db.logs[-1]["event"] == "after_turn"
    audit = db.update_audits[-1]
    assert audit["trigger"] == "after_turn"
    assert audit["conversation_id"] == "private:u1"
    assert audit["inferred_user_id"] == "u1"
    assert audit["raw_update"]["mood"] == 81
    assert audit["state_before"]["mood"] == 65.0
    assert audit["state_after"]["mood"] == 81
    assert audit["profile_before"] is None
    assert audit["profile_after"]["user_id"] == "u1"
    assert audit["applied_changes"]["state"]
    assert audit["applied_changes"]["profiles"]
    assert audit["applied_changes"]["effects"][0]["operation"] == "create"
    assert audit["applied_changes"]["effects"][0]["id"].startswith("effect_")
    assert audit["applied_changes"]["todos"][0]["operation"] == "create"
    assert audit["applied_changes"]["todos"][0]["id"].startswith("todo_")
    assert audit["applied_changes"]["cues"][0]["operation"] == "create"
    assert audit["applied_changes"]["cues"][0]["id"].startswith("cue_")
    prompt = provider.calls[0]["messages"][1]["content"]
    assert "助手、assistant、当前回复、角色刚说的话，都是当前人格自己的发言" in prompt
    assert "latest_monologue 必须是一人称内心状态" in prompt
    assert "稳定偏好、称呼、长期习惯、关系变化" in prompt
    assert "profile 字段包括 user_id、display_name、summary、traits、affinity" in prompt
    assert "traits 必须是 JSON 字符串数组" in prompt
    assert "私聊可省略 user_id 由系统推断，群聊必须带 user_id" in prompt
    assert "affinity 是 0-100 的绝对亲近度" in prompt
    assert "不是 0-1，也不是 1-10" in prompt
    assert "0=陌生/排斥" in prompt
    assert "30=疏离" in prompt
    assert "50=普通熟人" in prompt
    assert "70=信任友好" in prompt
    assert "85=亲近在意" in prompt
    assert "95=核心亲密关系" in prompt
    assert "relationship/affinity_delta 是本轮增减分" in prompt
    assert "普通一轮互动通常 -5 到 +5" in prompt
    assert "强烈事件可更大，但要写 reason" in prompt
    assert "优先用 affinity_delta" in prompt
    assert "不要随意用低 absolute affinity 覆盖" in prompt
    assert "profile 事实仍不要塞短期情绪、一次性事件或临时状态" in prompt
    assert "social_need 表示社交未满足度" in prompt
    assert "0-5 非常罕见" in prompt
    assert "10-25 是被回应、被关心、聊天满足后的常见低位" in prompt
    assert "30-60 是普通稳定区间" in prompt
    assert "单轮普通亲密互动通常只小幅下降" in prompt
    assert "想继续聊、害羞、上头、亲近余韵不要挤到 social_need" in prompt
    assert "effects[] 用于会持续一段时间的临时情绪、身体感、语气倾向、行动倾向或关系余韵" in prompt
    assert "当本轮互动留下明显短期余韵时，应创建或更新 effect" in prompt
    assert "不要把短期余韵塞进长期 profile" in prompt
    assert "cues[] 用于对当前会话或近期互动有用" in prompt
    assert "不要因为它不是长期记忆就丢掉" in prompt
    assert "todos[] 用于之后需要执行、检查、提醒或收尾的具体事项" in prompt
    assert "用户说“你一定要去睡哦”且角色接受时" in prompt
    assert [(item["state"], item["text"]) for item in statuses] == [
        ("thinking", "人格状态更新中"),
        ("idle", "人格状态更新完成"),
    ]
    context = agent.get_context_for_chat("private:u1")
    assert "语气更安心" in context
    assert "稍后确认状态" in context
    assert "对方可能需要休息提醒" in context
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_prompt_uses_persona_context_view_ids_audits_and_delta_guidance(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    db = FakeDB(PersonaState(mood=64, social_need=41))
    db.profiles["u1"] = UserProfile(
        user_id="u1",
        display_name="小林",
        affinity=82.0,
        summary="长期偏好短句反馈",
        traits=["会直接夸奖"],
        interaction_count=8,
        last_interaction_at=900.0,
    )
    db.effects = [
        Effect(
            id="effect_real_1",
            name="被认可",
            effect_type="mood",
            intensity=20,
            prompt_hint="回应时更放松",
            source_detail="用户刚夸奖",
            created_at=900.0,
            expires_at=9999999999,
        )
    ]
    db.cues = [
        Cue(
            id="cue_real_1",
            cue_type="conversation",
            summary="可以延续夸奖话题",
            conversation_id="private:u1",
            created_at=910.0,
            expires_at=9999999999,
        )
    ]
    db.todos = [
        Todo(
            id="todo_real_1",
            title="稍后确认项目进展",
            reason="用户提到项目",
            priority=5,
            scope="private:u1",
            created_at=920.0,
            expires_at=9999999999,
        )
    ]
    db.update_audits.append(
        {
            "id": "audit_recent_1",
            "conversation_id": "private:u1",
            "user_id": "u1",
            "field": "affinity",
            "old_value": 80,
            "new_value": 82,
            "summary": "上轮因稳定互动小幅上调",
        }
    )
    db.important = [
        {"id": "mem_user_1", "scope": "user:u1", "content": "用户偏好自然短句。"},
    ]
    provider = FakeProvider(["{}"])
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn(
        "private:u1",
        [{"user_id": "u1", "nickname": "小林"}],
        "用户夸奖角色说你今天很厉害。",
    )

    prompt = provider.calls[0]["messages"][1]["content"]
    assert "<事件>" in prompt
    assert "<聊天现场>" in prompt
    assert "<当前对象画像>" in prompt
    assert "用户夸奖角色说你今天很厉害" in prompt
    assert "好感: 82" in prompt
    assert "effect_real_1" in prompt
    assert "cue_real_1" in prompt
    assert "todo_real_1" in prompt
    assert "audit_recent_1" in prompt
    assert "最近画像变动" in prompt
    assert "mem_user_1 用户偏好自然短句" in prompt
    assert "普通一轮互动优先使用 affinity_delta" in prompt
    assert "绝对 affinity 只用于首次建档或明确校准" in prompt
    assert "不要随意用低 absolute affinity 覆盖" in prompt
    assert "已有项操作必须带上下文里出现过的真实 id" in prompt
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_episode_context_is_append_only_within_active_episode(monkeypatch):
    ticks = iter([1000.0, 1000.0, 1010.0, 1010.0])
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: next(ticks, 1010.0))
    db = FakeDB(PersonaState())
    provider = FakeProvider(["{}", "{}"])
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "第一轮摘要")
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "第二轮摘要")

    first_prompt = provider.calls[0]["messages"][1]["content"]
    second_prompt = provider.calls[1]["messages"][1]["content"]
    assert "第一轮摘要" in first_prompt
    assert "第二轮摘要" not in first_prompt
    assert "第一轮摘要" in second_prompt
    assert "第二轮摘要" in second_prompt
    assert "片段数: 2" in second_prompt
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_operations_update_close_and_drop_unknown_ids(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    db = FakeDB(PersonaState())
    db.effects = [
        Effect("effect_update", "旧影响", "mood", 10, "旧提示", "旧来源", 100.0, 9999999999),
        Effect("effect_close", "待关闭", "mood", 5, "关闭提示", "旧来源", 100.0, 9999999999),
        Effect("effect_keep", "保留", "mood", 5, "保留提示", "旧来源", 100.0, 9999999999),
    ]
    db.cues = [
        Cue("cue_update", "conversation", "旧线索", "private:u1", 100.0, 9999999999),
        Cue("cue_close", "conversation", "待关闭线索", "private:u1", 100.0, 9999999999),
        Cue("cue_keep", "conversation", "保留线索", "private:u1", 100.0, 9999999999),
    ]
    db.todos = [
        Todo("todo_update", "旧待办", "旧原因", 3, "private:u1", 100.0, 9999999999),
        Todo("todo_close", "待关闭待办", "旧原因", 4, "private:u1", 100.0, 9999999999),
        Todo("todo_keep", "保留待办", "旧原因", 2, "private:u1", 100.0, 9999999999),
    ]
    provider = FakeProvider(
        [
            """
            {
              "effects": [
                {"id": "effect_update", "operation": "update", "prompt_hint": "新提示"},
                {"id": "effect_close", "operation": "close"},
                {"id": "effect_missing", "operation": "delete"}
              ],
              "cues": [
                {"id": "cue_update", "operation": "update", "summary": "新线索"},
                {"id": "cue_close", "operation": "delete"},
                {"id": "cue_missing", "operation": "update", "summary": "不要新建"}
              ],
              "todos": [
                {"id": "todo_update", "operation": "update", "title": "新待办"},
                {"id": "todo_close", "operation": "complete"},
                {"id": "todo_missing", "operation": "cancel", "title": "不要新建"}
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "关闭和更新已有项目")

    effects = {effect.id: effect for effect in db.effects}
    assert set(effects) == {"effect_update", "effect_keep"}
    assert effects["effect_update"].name == "旧影响"
    assert effects["effect_update"].prompt_hint == "新提示"
    cues = {cue.id: cue for cue in db.cues}
    assert set(cues) == {"cue_update", "cue_keep"}
    assert cues["cue_update"].summary == "新线索"
    todos = {todo.id: todo for todo in db.todos}
    assert set(todos) == {"todo_update", "todo_keep"}
    assert todos["todo_update"].title == "新待办"
    assert set(db.closed_todos) == {"todo_close"}
    assert db.closed_todos["todo_close"]["status"] == "completed"
    assert "todo_missing" not in db.closed_todos

    audit = db.update_audits[-1]["applied_changes"]
    assert any(item["operation"] == "dropped_unknown_id" and item["id"] == "effect_missing" for item in audit["effects"])
    assert any(item["operation"] == "dropped_unknown_id" and item["id"] == "cue_missing" for item in audit["cues"])
    assert any(item["operation"] == "dropped_unknown_id" and item["id"] == "todo_missing" for item in audit["todos"])
    assert any(item["operation"] == "close" and item["id"] == "effect_close" for item in audit["effects"])
    assert any(item["operation"] == "delete" and item["id"] == "cue_close" for item in audit["cues"])
    assert any(item["operation"] == "completed" and item["id"] == "todo_close" for item in audit["todos"])
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_drops_unknown_ids_without_operation_for_state_items(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    db = FakeDB(PersonaState())
    provider = FakeProvider(
        [
            """
            {
              "effects": [
                {
                  "id": "effect_unknown_no_operation",
                  "name": "不应新建",
                  "effect_type": "mood",
                  "intensity": 20,
                  "prompt_hint": "不要写入",
                  "source_detail": "模型幻觉 id"
                }
              ],
              "cues": [
                {
                  "id": "cue_unknown_no_operation",
                  "summary": "不要写入"
                }
              ],
              "todos": [
                {
                  "id": "todo_unknown_no_operation",
                  "title": "不要写入",
                  "reason": "模型幻觉 id"
                }
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "模型返回了未知 id")

    assert db.effects == []
    assert db.cues == []
    assert db.todos == []
    assert db.closed_todos == {}

    audit = db.update_audits[-1]["applied_changes"]
    assert any(
        item["operation"] == "dropped_unknown_id"
        and item["id"] == "effect_unknown_no_operation"
        and item["requested_operation"] is None
        for item in audit["effects"]
    )
    assert any(
        item["operation"] == "dropped_unknown_id"
        and item["id"] == "cue_unknown_no_operation"
        and item["requested_operation"] is None
        for item in audit["cues"]
    )
    assert any(
        item["operation"] == "dropped_unknown_id"
        and item["id"] == "todo_unknown_no_operation"
        and item["requested_operation"] is None
        for item in audit["todos"]
    )
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_relationship_update_and_plain_interaction_touch(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    db = FakeDB(PersonaState())
    db.profiles["u1"] = UserProfile(
        user_id="u1",
        display_name="旧名",
        affinity=20.0,
        summary="旧印象",
        traits=["稳重"],
        interaction_count=2,
        last_interaction_at=900.0,
    )
    provider = FakeProvider(
        [
            """
            {
              "relationship": {
                "affinity_delta": 12,
                "summary": "刚刚有一次轻松亲近的互动",
                "traits": ["会主动关心"]
              }
            }
            """,
            "{}"
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1", "nickname": "小林"}], "用户关心了一句")

    profile = db.profiles["u1"]
    assert profile.display_name == "小林"
    assert profile.affinity == 32.0
    assert profile.summary == "刚刚有一次轻松亲近的互动"
    assert profile.traits == ["会主动关心"]
    assert profile.interaction_count == 3
    assert profile.last_interaction_at == 1000.0

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1100.0)
    await agent.after_turn("system:global", [{"user_id": "u2", "nickname": "阿泉"}], "系统轮实际面向 u2")

    assert db.profiles["u2"].display_name == "阿泉"
    assert db.profiles["u2"].interaction_count == 1
    assert db.profiles["u2"].last_interaction_at == 1100.0
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_accepts_string_traits_for_profiles_and_relationships(monkeypatch):
    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 1000.0)
    db = FakeDB(PersonaState())
    provider = FakeProvider(
        [
            """
            {
              "profiles": [
                {
                  "user_id": "u_profile",
                  "display_name": "画像对象",
                  "summary": "对 AI 和技术话题很敏感",
                  "traits": "技术好奇、套话倾向、能懂AI"
                }
              ],
              "relationships": [
                {
                  "user_id": "u_relationship",
                  "display_name": "关系对象",
                  "summary": "互动里呈现出清晰偏好",
                  "traits": "a,b; c",
                  "affinity_delta": 3,
                  "reason": "测试字符串 traits 容错"
                }
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户聊到技术和 AI")

    assert db.profiles["u_profile"].traits == ["技术好奇", "套话倾向", "能懂AI"]
    assert db.profiles["u_relationship"].traits == ["a", "b", "c"]
    assert db.logs[-1]["event"] == "after_turn"
    assert db.logs[-1]["fallback"] is False
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_accepts_level_words_for_intensity_and_priority():
    db = FakeDB(PersonaState())
    provider = FakeProvider(
        [
            """
            {
              "effect": {
                "name": "被理解",
                "effect_type": "buff",
                "intensity": "medium",
                "prompt_hint": "更愿意继续回应",
                "source_detail": "unit",
                "expires_at": 9999999999
              },
              "todo": {
                "title": "稍后补一句关心",
                "priority": "medium"
              }
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "对方表达了压力")

    assert len(provider.calls) == 1
    assert db.effects[0].intensity == 50.0
    assert db.todos[0].priority == 5
    assert db.logs[-1]["fallback"] is False
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_accepts_iso_expires_at_for_cue():
    db = FakeDB(PersonaState())
    provider = FakeProvider(
        [
            """
            {
              "mood_delta": 1,
              "cue": {
                "summary": "三天后可能有安排",
                "expires_at": "2026-06-16T00:00:00Z"
              }
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户提到三天后的安排")

    assert len(provider.calls) == 1
    assert db.logs[-1]["fallback"] is False
    assert db.cues[0].id.startswith("cue_")
    assert db.cues[0].summary == "三天后可能有安排"
    assert isinstance(db.cues[0].expires_at, float)
    assert db.cues[0].expires_at == pytest.approx(1781568000.0)
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_filters_empty_duplicate_and_keeps_specific_new_todos():
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_existing",
            title="已经安排复诊",
            reason="unit",
            priority=2,
            scope="private:u1",
            created_at=10.0,
        ),
        Todo(
            id="todo_water",
            title="提醒喝水",
            reason="unit",
            priority=1,
            scope="private:u1",
            created_at=11.0,
        ),
    ]
    provider = FakeProvider(
        [
            """
            {
              "todos": [
                {"title": "   ", "scope": "private:u1", "priority": 9},
                {"title": "已经安排复诊。", "scope": "private:u1", "priority": 9},
                {
                  "id": "todo_existing",
                  "title": "确认复诊时间",
                  "scope": "private:u1",
                  "priority": 6
                },
                {
                  "id": "todo_model_new_id",
                  "title": "提醒喝水",
                  "scope": "private:u1",
                  "priority": 8
                },
                {
                  "title": "明早叫醒用户",
                  "scope": "private:u1",
                  "priority": 8,
                  "expires_at": 9999999999
                },
                {
                  "title": "晚上提醒吃饭",
                  "scope": "private:u1",
                  "priority": 7,
                  "expires_at": 9999999999
                }
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户说明早需要叫醒")

    todos_by_title = {todo.title: todo for todo in db.todos}
    assert set(todos_by_title) == {"确认复诊时间", "提醒喝水", "明早叫醒用户", "晚上提醒吃饭"}
    assert todos_by_title["确认复诊时间"].id == "todo_existing"
    assert todos_by_title["确认复诊时间"].priority == 6
    assert todos_by_title["提醒喝水"].id == "todo_water"
    assert todos_by_title["明早叫醒用户"].scope == "private:u1"
    assert todos_by_title["晚上提醒吃饭"].scope == "private:u1"
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_can_close_todos_and_removes_from_context():
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_completed",
            title="提醒复诊",
            reason="用户要求",
            priority=6,
            scope="private:u1",
            created_at=10.0,
            expires_at=9999999999,
        ),
        Todo(
            id="todo_done",
            title="提醒喝水",
            reason="生理提醒",
            priority=3,
            scope="private:u1",
            created_at=11.0,
            expires_at=9999999999,
        ),
        Todo(
            id="todo_cancelled",
            title="提醒订票",
            reason="用户临时取消",
            priority=4,
            scope="private:u1",
            created_at=12.0,
            expires_at=9999999999,
        ),
        Todo(
            id="todo_open",
            title="保留的待办",
            reason="仍需执行",
            priority=2,
            scope="private:u1",
            created_at=13.0,
            expires_at=9999999999,
        ),
    ]
    provider = FakeProvider(
        [
            """
            {
              "todos": [
                {"id": "todo_completed", "completed": true},
                {"id": "todo_done", "done": true},
                {"id": "todo_cancelled", "status": "cancelled"}
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "用户说这些提醒已经不用了")

    assert [todo.id for todo in db.todos] == ["todo_open"]
    assert set(db.closed_todos) == {"todo_completed", "todo_done", "todo_cancelled"}
    assert db.closed_todos["todo_completed"]["status"] == "completed"
    assert db.closed_todos["todo_done"]["status"] == "completed"
    assert db.closed_todos["todo_cancelled"]["status"] == "cancelled"
    context = agent.get_context_for_chat("private:u1")
    proactive_titles = [todo.title for todo in agent.get_todos_for_proactive()]
    assert proactive_titles == ["保留的待办"]
    assert "保留的待办" in context
    assert "提醒复诊" not in context
    assert "提醒喝水" not in context
    assert "提醒订票" not in context
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_partial_todo_update_preserves_metadata():
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_existing",
            title="原始标题",
            reason="保留原因",
            priority=7,
            scope="private:u1",
            created_at=10.0,
            expires_at=9999999999,
        )
    ]
    provider = FakeProvider(
        [
            """
            {
              "todos": [
                {"id": "todo_existing", "title": "更新标题"}
              ]
            }
            """
        ]
    )
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [{"user_id": "u1"}], "只改标题")

    assert len(db.todos) == 1
    updated = db.todos[0]
    assert updated.id == "todo_existing"
    assert updated.title == "更新标题"
    assert updated.reason == "保留原因"
    assert updated.priority == 7
    assert updated.scope == "private:u1"
    assert updated.created_at == 10.0
    assert updated.expires_at == 9999999999
    await agent.shutdown()


@pytest.mark.asyncio
async def test_start_marks_expired_todos_missed_and_filters_from_context(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_expired",
            title="已经过期",
            reason="unit",
            priority=9,
            scope="private:u1",
            created_at=1.0,
            expires_at=100.0,
        ),
        Todo(
            id="todo_open",
            title="仍然有效",
            reason="unit",
            priority=1,
            scope="private:u1",
            created_at=2.0,
            expires_at=9999999999,
        ),
    ]
    agent = _agent(db)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await agent.start()
    assert [todo.title for todo in agent.get_todos_for_proactive()] == ["仍然有效"]
    assert "已经过期" not in agent.get_context_for_chat("private:u1")
    assert "todo_expired" in db.closed_todos
    assert db.closed_todos["todo_expired"]["status"] == "missed"
    assert db.closed_todos["todo_expired"]["completed"] is True
    assert [todo.id for todo in await db.get_todos(include_completed=False)] == ["todo_open"]

    audit_todos = await db.get_todos(include_completed=True)
    audit_ids = {todo.id if isinstance(todo, Todo) else todo["id"] for todo in audit_todos}
    assert audit_ids == {"todo_expired", "todo_open"}

    await agent.periodic_tick()
    await agent.periodic_tick()
    assert db.missed_todo_updates == 1
    assert [todo.title for todo in agent.get_todos_for_proactive()] == ["仍然有效"]
    await agent.shutdown()


@pytest.mark.asyncio
async def test_get_todos_for_proactive_sorts_before_context_limit(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id=f"todo_low_{index}",
            title=f"低优旧待办{index}",
            reason="unit",
            priority=1,
            scope="private:u1",
            created_at=float(index),
            expires_at=1000.0 + index,
        )
        for index in range(5)
    ]
    db.todos.append(
        Todo(
            id="todo_high_new",
            title="高优新待办",
            reason="unit",
            priority=10,
            scope="private:u1",
            created_at=999.0,
            expires_at=9999999999,
        )
    )
    agent = _agent(db)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 50.0)
    await agent.start()

    proactive_titles = [todo.title for todo in agent.get_todos_for_proactive()]
    context = agent.get_context_for_chat("private:u1")
    context_todos = context.split("- 待办: ", 1)[1].split("\n", 1)[0]

    assert proactive_titles[0] == "高优新待办"
    assert "高优新待办" in context_todos
    assert "低优旧待办4" not in context_todos
    await agent.shutdown()


@pytest.mark.asyncio
async def test_get_todos_for_proactive_keeps_unparseable_expires_at(monkeypatch):
    db = FakeDB(PersonaState())
    db.todos = [
        Todo(
            id="todo_unparseable",
            title="不可解析时间保守保留",
            reason="unit",
            priority=4,
            scope="private:u1",
            created_at=1.0,
            expires_at="not-a-time",  # type: ignore[arg-type]
        ),
        Todo(
            id="todo_expired",
            title="已过期待办",
            reason="unit",
            priority=9,
            scope="private:u1",
            created_at=2.0,
            expires_at=100.0,
        ),
    ]
    agent = _agent(db)

    monkeypatch.setattr(persona_agent_mod.time, "time", lambda: 150.0)
    await agent.start()

    assert [todo.title for todo in agent.get_todos_for_proactive()] == [
        "不可解析时间保守保留"
    ]
    assert "不可解析时间保守保留" in agent.get_context_for_chat("private:u1")
    await agent.shutdown()


@pytest.mark.asyncio
async def test_after_turn_malformed_retries_and_total_failure_falls_back():
    db = FakeDB(PersonaState(mood=50, social_need=50))
    provider = FakeProvider(["not json", '{"mood_delta": 5, "social_need_delta": -10}'])
    agent = _agent(db, provider=provider)

    await agent.start()
    await agent.after_turn("private:u1", [], "summary")

    assert len(provider.calls) == 2
    assert agent.get_state_snapshot().mood == 55
    assert agent.get_state_snapshot().social_need == 40

    provider.responses = ["not json", "{bad"]
    await agent.after_turn("private:u1", [], "summary")

    assert len(provider.calls) == 4
    assert db.logs[-1]["fallback"] is True
    assert agent.get_state_snapshot().mood == 55
    await agent.shutdown()



