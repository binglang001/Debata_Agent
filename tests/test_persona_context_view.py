from __future__ import annotations

from dataclasses import dataclass, field

from agents.persona_context_view import (
    PersonaContextView,
    PersonaEpisodeBuffer,
    build_persona_context_text,
)


@dataclass(slots=True)
class ObjectProfile:
    user_id: str
    display_name: str
    affinity: float
    summary: str
    traits: list[str] = field(default_factory=list)
    interaction_count: int = 0
    last_interaction_at: float = 0.0


def test_context_text_contains_sections_and_preserves_full_ids() -> None:
    buffer = PersonaEpisodeBuffer(max_fragments=4, max_chars=1000, idle_seconds=60)
    view = PersonaContextView(buffer)
    context = {
        "event": {
            "trigger_type": "message",
            "conversation_id": "private:user-full-id-0000000001",
            "new_messages": [
                {
                    "id": "message-full-id-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "sender_id": "user-full-id-0000000001",
                    "text": "今天想吃面",
                    "created_at": 10,
                }
            ],
            "summary": "用户在问候后提到晚饭",
            "participants": [
                {
                    "user_id": "user-full-id-0000000001",
                    "display_name": "小林",
                    "role": "friend",
                }
            ],
            "eat_event": {
                "id": "eat-event-full-id-11111111111111111111",
                "food": "面包",
                "status": "finished",
            },
            "tools": [
                {
                    "id": "tool-call-full-id-22222222222222222222",
                    "name": "eat",
                    "status": "ok",
                    "internal_payload": {"ignored": True},
                }
            ],
            "actions": [
                {
                    "id": "action-full-id-33333333333333333333",
                    "name": "reply",
                    "status": "planned",
                }
            ],
        },
        "state": {
            "mood": 68,
            "social_need": 45,
            "energy": 77,
            "satiety": 52,
            "current_action": "awake",
            "action_until": 999,
            "latest_monologue": "可以轻松一点回应。",
            "last_eat_at": 100,
            "last_sleep_at": 200,
            "last_interaction_at": 300,
        },
        "profile": ObjectProfile(
            user_id="profile-user-full-id-44444444444444444444",
            display_name="小林",
            affinity=36,
            summary="喜欢短句反馈",
            traits=["会主动关心", "偏好轻松语气"],
            interaction_count=7,
            last_interaction_at=300,
        ),
        "profile_audits": [
            {
                "id": "profile-audit-full-id-55555555555555555555",
                "created_at": 20,
                "user_id": "profile-user-full-id-44444444444444444444",
                "field": "summary",
                "new_value": "喜欢短句反馈",
                "reason": "用户明确表达偏好",
            }
        ],
        "effects": [
            {
                "id": "effect-full-id-66666666666666666666",
                "created_at": 30,
                "name": "被关心",
                "effect_type": "mood",
                "intensity": 15,
                "prompt_hint": "语气更柔和",
                "source_detail": "用户主动问候",
                "expires_at": 90,
                "debug_only": "should-not-render",
            }
        ],
        "cues": [
            {
                "id": "cue-full-id-77777777777777777777",
                "created_at": 40,
                "cue_type": "conversation",
                "summary": "晚饭话题可继续",
                "conversation_id": "private:user-full-id-0000000001",
                "expires_at": 80,
            }
        ],
        "todos": [
            {
                "id": "todo-full-id-88888888888888888888",
                "created_at": 50,
                "title": "稍后确认是否吃饭",
                "reason": "用户提到晚饭",
                "priority": 3,
                "scope": "persona",
                "status": "closed",
                "completed": True,
            }
        ],
        "long_term_memory_text": "用户偏好自然短句，不喜欢长篇解释。",
    }

    text = view.build_text(context, now=60)

    for tag in (
        "事件",
        "聊天现场",
        "当前状态",
        "当前对象画像",
        "短期影响",
        "线索",
        "待办",
        "相关长期记忆",
    ):
        assert f"<{tag}>" in text
        assert f"</{tag}>" in text
    for full_id in (
        "private:user-full-id-0000000001",
        "message-full-id-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "tool-call-full-id-22222222222222222222",
        "profile-user-full-id-44444444444444444444",
        "profile-audit-full-id-55555555555555555555",
        "effect-full-id-66666666666666666666",
        "cue-full-id-77777777777777777777",
        "todo-full-id-88888888888888888888",
    ):
        assert full_id in text
    assert "debug_only" not in text
    assert "internal_payload" not in text
    assert '"effect-full-id' not in text


def test_single_dict_event_blocks_use_named_fields() -> None:
    text = build_persona_context_text(
        {
            "event": {
                "new_messages": {
                    "id": "message-single-dict-id",
                    "sender_id": "user-1",
                    "description": "这段不应作为消息内容",
                    "status": "ignored",
                    "text": "单条消息内容",
                },
                "eat_event": {
                    "id": "eat-single-dict-id",
                    "food": "番茄鸡蛋面",
                    "description": "晚饭已经吃完",
                    "status": "finished",
                },
            }
        },
        now=1,
    )

    assert "本轮新增: ID: message-single-dict-id；发送者: user-1；内容: 单条消息内容" in text
    assert "description" not in text
    assert "进食事件: ID: eat-single-dict-id；食物: 番茄鸡蛋面；状态: finished；摘要: 晚饭已经吃完" in text


def test_episode_buffer_is_append_only_within_active_episode() -> None:
    buffer = PersonaEpisodeBuffer(max_fragments=3, max_chars=1000, idle_seconds=60)

    first = buffer.append_fragment("第一句", conversation_id="c1", current_action="awake", now=1)
    second = buffer.append_fragment("第二句", conversation_id="c1", current_action="awake", now=2)

    assert first.episode_id == second.episode_id
    assert [fragment.text for fragment in second.fragments] == ["第一句", "第二句"]


def test_episode_buffer_reopens_on_count_and_stays_bounded() -> None:
    buffer = PersonaEpisodeBuffer(max_fragments=2, max_chars=1000, idle_seconds=60)

    first = buffer.append_fragment("a", conversation_id="c1", current_action="awake", now=1)
    buffer.append_fragment("b", conversation_id="c1", current_action="awake", now=2)
    reopened = buffer.append_fragment("c", conversation_id="c1", current_action="awake", now=3)

    assert reopened.episode_id != first.episode_id
    assert [fragment.text for fragment in reopened.fragments] == ["c"]
    assert len(reopened.fragments) <= 2


def test_episode_buffer_reopens_on_char_idle_conversation_and_rest_action_boundaries() -> None:
    char_buffer = PersonaEpisodeBuffer(max_fragments=5, max_chars=8, idle_seconds=60)
    first = char_buffer.append_fragment("1234567", conversation_id="c1", current_action="awake", now=1)
    reopened_by_chars = char_buffer.append_fragment("xx", conversation_id="c1", current_action="awake", now=2)
    assert reopened_by_chars.episode_id != first.episode_id
    assert [fragment.text for fragment in reopened_by_chars.fragments] == ["xx"]
    assert reopened_by_chars.char_count <= 8

    idle_buffer = PersonaEpisodeBuffer(max_fragments=5, max_chars=1000, idle_seconds=10)
    first = idle_buffer.append_fragment("before idle", conversation_id="c1", current_action="awake", now=1)
    reopened_by_idle = idle_buffer.append_fragment("after idle", conversation_id="c1", current_action="awake", now=20)
    assert reopened_by_idle.episode_id != first.episode_id
    assert [fragment.text for fragment in reopened_by_idle.fragments] == ["after idle"]

    conversation_buffer = PersonaEpisodeBuffer(max_fragments=5, max_chars=1000, idle_seconds=60)
    first = conversation_buffer.append_fragment("c1 text", conversation_id="c1", current_action="awake", now=1)
    reopened_by_conversation = conversation_buffer.append_fragment(
        "c2 text",
        conversation_id="c2",
        current_action="awake",
        now=2,
    )
    assert reopened_by_conversation.episode_id != first.episode_id
    assert [fragment.text for fragment in reopened_by_conversation.fragments] == ["c2 text"]

    action_buffer = PersonaEpisodeBuffer(max_fragments=5, max_chars=1000, idle_seconds=60)
    first = action_buffer.append_fragment("awake text", conversation_id="c1", current_action="awake", now=1)
    reopened_by_action = action_buffer.append_fragment(
        "eat text",
        conversation_id="c1",
        current_action="eating",
        now=2,
    )
    assert reopened_by_action.episode_id != first.episode_id
    assert [fragment.text for fragment in reopened_by_action.fragments] == ["eat text"]


def test_missing_state_fields_are_skipped_without_error() -> None:
    text = build_persona_context_text(
        {
            "event": {
                "trigger_type": "tick",
                "conversation_id": "conversation-full-id-99999999999999999999",
                "text": "只有部分状态字段",
            },
            "state": {
                "mood": 70,
            },
        },
        now=1,
    )

    assert "<当前状态>" in text
    assert "心情: 70" in text
    assert "社交需求" not in text
    assert "None" not in text
