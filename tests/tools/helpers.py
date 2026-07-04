"""Shared helpers for tool tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from adapters.types import FriendInfo, GroupInfo, GroupMemberInfo, UserInfo
from core.chat_timeline import ChatTimelineMessage
from tools import ToolContext


def _assert_tool_result_envelope(result: dict[str, Any], tool_name: str) -> None:
    assert result["tool"] == tool_name
    assert result["result_format"] == "structured_json"
    assert "ok" in result
    assert "status" in result
    assert isinstance(result["brief"], str)
    assert result["brief"].strip()


def _make_config(
    *,
    memory_mode="file",
    vision_enabled=False,
    web_search_enabled=False,
    weather_enabled=False,
    persona_management_enabled=False,
    energy_mode="disabled",
    satiety_mode="disabled",
):
    """构造最小合法 RootConfig。"""
    from app_config.schema import (
        AgentConfig,
        AgentsConfig,
        FeaturesConfig,
        LongTermMemoryConfig,
        PersonaManagementConfig,
        ProviderConfig,
        RootConfig,
        VisionFeatureConfig,
        WeatherFeatureConfig,
        WebSearchFeatureConfig,
    )

    return RootConfig(
        providers={
            "deepseek": ProviderConfig(
                preset="deepseek", api_key_id="k1"
            )
        },
        agents=AgentsConfig(
            chat=AgentConfig(provider="deepseek", model="deepseek-chat"),
        ),
        features=FeaturesConfig(
            vision=VisionFeatureConfig(
                enabled=vision_enabled,
                provider="deepseek" if vision_enabled else None,
            ),
            web_search=WebSearchFeatureConfig(enabled=web_search_enabled),
            weather=WeatherFeatureConfig(
                enabled=weather_enabled,
                api_key_id="fake_qweather" if weather_enabled else None,
                host="devapi.qweather.com",
            ),
            long_term_memory=LongTermMemoryConfig(mode=memory_mode),
        ),
        persona_management=PersonaManagementConfig(
            enabled=persona_management_enabled,
            physiology={
                "energy": {"mode": energy_mode},
                "satiety": {"mode": satiety_mode},
            },
        ),
    )




def _timeline_message(message_id: str, text: str) -> ChatTimelineMessage:
    return ChatTimelineMessage(
        conversation_id="private:123",
        direction="inbound",
        timestamp=1_780_000_000.0,
        time_text="2026-05-30 00:00:00",
        sender_name="用户",
        sender_id="123",
        target_id="123",
        group_id=None,
        msg_id=message_id,
        text=text,
        raw_message=text,
    )


def _approve_stub_tools(ctx: ToolContext, *names: str) -> None:
    approved = ctx.extras.setdefault("tool_search_approved_tools", set())
    assert isinstance(approved, set)
    approved.update(names)


class FakeSendAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []
        self.sent_images: list[dict[str, object]] = []
        self.voice_sent: list[tuple[object, Path]] = []
        self.api_calls: list[tuple[str, dict]] = []
        self._next_msg_id = 100

    async def send_text(self, target, content: str) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        self.sent.append((target, content))
        return msg_id

    async def send_voice(self, target, audio_path: Path) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        self.voice_sent.append((target, audio_path))
        return msg_id

    async def send_image(self, target, *, image_path=None, image_url=None, image_b64=None) -> str:
        msg_id = str(self._next_msg_id)
        self._next_msg_id += 1
        self.sent_images.append(
            {
                "target": target,
                "image_path": image_path,
                "image_url": image_url,
                "image_b64": image_b64,
            }
        )
        return msg_id


class FullFakeAdapter(FakeSendAdapter):
    """覆盖所有平台工具会触达的适配器方法。"""

    async def list_friends(self):
        return [FriendInfo(user_id="1001", nickname="Alice")]

    async def list_groups(self):
        return [GroupInfo(group_id="2001", group_name="测试群", member_count=2)]

    async def list_group_members(self, group_id: str):
        return [
            GroupMemberInfo(user_id="1001", nickname="Alice", card="AliceCard"),
            GroupMemberInfo(user_id="1002", nickname="Bob"),
        ]

    async def get_user_info(self, user_id: str):
        return UserInfo(user_id=user_id, nickname="Alice", sex="unknown", age=18)

    async def get_forward_msg(self, forward_id: str):
        if forward_id == "root":
            return [
                {
                    "sender": {"nickname": "Alice", "user_id": "1001"},
                    "message_id": "f1",
                    "content": "第一条[CQ:image,summary=[图片],file=a.jpg,url=https://example.com/a.jpg]",
                },
                {
                    "sender": {"nickname": "Bob", "user_id": "1002"},
                    "message_id": "f2",
                    "content": "[CQ:forward,id=child]",
                },
            ]
        if forward_id == "child":
            return [
                {
                    "sender": {"nickname": "Carol", "user_id": "1003"},
                    "message_id": "f3",
                    "content": "内层消息",
                }
            ]
        return []

    async def handle_friend_request(self, flag: str, approve: bool, remark: str = "") -> None:
        self.friend_request = {"flag": flag, "approve": approve, "remark": remark}

    async def handle_group_request(
        self,
        flag: str,
        sub_type: str,
        approve: bool,
        reason: str = "",
    ) -> None:
        self.group_request = {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
            "reason": reason,
        }

    async def get_group_history(self, group_id: str, count: int = 100):
        return [{"message_id": "h1", "raw_message": "群历史", "sender": {"nickname": "A"}}]

    async def recall(self, message_id: str) -> bool:
        self.recalled = message_id
        return True

    async def upload_file(self, target, file_path: Path, *, display_name: str | None = None) -> None:
        self.uploaded = {"target": target, "file_path": file_path, "display_name": display_name}

    async def call_api(self, action: str, **params: Any) -> dict:
        self.api_calls.append((action, params))
        if action == "get_group_member_info":
            return {
                "group_id": params.get("group_id"),
                "user_id": params.get("user_id"),
                "role": "admin",
            }
        if action == "get_msg":
            return {
                "message_id": params.get("message_id"),
                "group_id": 456,
                "user_id": 1001,
                "raw_message": "单条消息内容",
            }
        return {"ok": True, "action": action}


class FakeVision:
    async def describe(self, image_url: str, prompt: str = ""):
        return {"summary": "一张测试图片", "description": f"图片={image_url}; 问题={prompt or '-'}"}


class FakeWebSearch:
    async def search(self, query: str) -> str:
        return f"1. 结果\n摘要\nhttps://example.com/search?q={query}"


class FakeWeather:
    async def query(self, city: str, days: int = 1) -> str:
        return f"{city} {days} 天天气晴"


class FakeTTS:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def synthesize(self, text: str, *, reference_audio=None, prompt: str = "") -> Path:
        path = self.workspace / "voice.wav"
        path.write_bytes(b"RIFFfake")
        return path


class _FakeAdapter:
    name = "fake"
    is_connected = True

    def __init__(self):
        self.uploaded: list = []
        self.recalled: list[str] = []
        self.friend_requests: list[tuple[str, bool, str]] = []
        self.group_requests: list[tuple[str, str, bool, str]] = []
        self.api_calls: list[tuple[str, dict]] = []
        self.member_role = "admin"
        self.friend_request_result: dict[str, Any] | None = None

    async def upload_file(self, target, file_path, *, display_name=None):
        self.uploaded.append((target, file_path, display_name))

    async def recall(self, message_id: str) -> bool:
        self.recalled.append(message_id)
        return message_id != "999"

    async def handle_friend_request(self, flag, approve, remark=""):
        self.friend_requests.append((flag, approve, remark))
        return self.friend_request_result

    async def handle_group_request(self, flag, sub_type, approve, reason=""):
        self.group_requests.append((flag, sub_type, approve, reason))

    async def call_api(self, action, **params):
        self.api_calls.append((action, params))
        if action == "get_group_member_info":
            return {
                "group_id": params.get("group_id"),
                "user_id": params.get("user_id"),
                "role": self.member_role,
            }
        if action == "get_msg":
            return {
                "message_id": params.get("message_id"),
                "user_id": 123,
                "message": [
                    {"type": "text", "data": {"text": "你好"}},
                    {"type": "face", "data": {"id": "76"}},
                ],
            }
        return {"ok": True, "action": action}


def _assert_no_title(node):
    if isinstance(node, dict):
        assert "title" not in node, f"残留 title: {node}"
        for v in node.values():
            _assert_no_title(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_title(v)
