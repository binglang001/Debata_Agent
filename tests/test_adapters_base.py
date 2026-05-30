"""测试适配器抽象层 —— 通过一个 mock 适配器验证契约和注册中心。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from adapters import (
    AdapterRegistry,
    EventType,
    FriendInfo,
    GroupInfo,
    GroupMemberInfo,
    IAdapter,
    IncomingMessage,
    IncomingNotice,
    IncomingRequest,
    MediaSegment,
    MediaType,
    NoticeType,
    RequestType,
    Target,
    UserInfo,
    build_adapter,
    known_adapter_types,
    register_adapter_type,
)


# ============================================================
# Mock 适配器：用于验证抽象层的契约
# ============================================================


class MockAdapter(IAdapter):
    """内存版适配器，记录所有调用以供断言。"""

    def __init__(self, name: str = "mock") -> None:
        super().__init__(name)
        self._connected = False
        self.sent_texts: list[tuple[Target, str]] = []
        self.sent_images: list[tuple[Target, dict]] = []
        self.recalled: list[str] = []
        self.api_calls: list[tuple[str, dict]] = []

    async def start(self) -> None:
        self._connected = True

    async def stop(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def send_text(self, target: Target, content: str) -> str | None:
        self.sent_texts.append((target, content))
        return f"msg_{len(self.sent_texts)}"

    async def send_image(
        self,
        target: Target,
        *,
        image_path: Path | None = None,
        image_url: str | None = None,
        image_b64: str | None = None,
    ) -> str | None:
        self.sent_images.append(
            (target, {"path": image_path, "url": image_url, "b64": image_b64})
        )
        return f"img_{len(self.sent_images)}"

    async def recall(self, message_id: str) -> bool:
        self.recalled.append(message_id)
        return True

    async def list_friends(self) -> list[FriendInfo]:
        return [FriendInfo(user_id="1001", nickname="Alice")]

    async def list_groups(self) -> list[GroupInfo]:
        return [GroupInfo(group_id="100", group_name="Test Group", member_count=3)]

    async def list_group_members(self, group_id: str) -> list[GroupMemberInfo]:
        return [GroupMemberInfo(user_id="1001", nickname="Alice", card="Cap")]

    async def get_user_info(self, user_id: str) -> UserInfo:
        return UserInfo(user_id=user_id, nickname=f"user_{user_id}")

    async def handle_friend_request(self, flag: str, approve: bool, remark: str = "") -> None:
        self.api_calls.append(("friend_request", {"flag": flag, "approve": approve}))

    async def handle_group_request(self, flag: str, sub_type, approve, reason="") -> None:
        self.api_calls.append(("group_request", {"flag": flag, "approve": approve}))

    async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
        self.api_calls.append((action, params))
        return {"ok": True}


# ============================================================
# Target 测试
# ============================================================


def test_target_str_repr():
    t = Target(adapter="napcat_default", scope="private", target_id="123")
    assert str(t) == "napcat_default:private:123"


def test_target_immutable():
    t = Target(adapter="a", scope="group", target_id="1")
    with pytest.raises(Exception):
        t.adapter = "b"  # frozen dataclass


# ============================================================
# 事件模型测试
# ============================================================


def test_incoming_message_event_type_default():
    msg = IncomingMessage(
        adapter="mock",
        timestamp=1.0,
        self_id="bot",
        message_id="m1",
        scope="private",
        user_id="100",
        nickname="Alice",
    )
    assert msg.event_type == EventType.MESSAGE
    assert msg.is_private()
    assert not msg.is_group()


def test_incoming_message_source_target_private():
    msg = IncomingMessage(
        adapter="mock",
        timestamp=1.0,
        self_id="bot",
        message_id="m1",
        scope="private",
        user_id="100",
        nickname="A",
    )
    t = msg.source_target
    assert t.scope == "private"
    assert t.target_id == "100"


def test_incoming_message_source_target_group():
    msg = IncomingMessage(
        adapter="mock",
        timestamp=1.0,
        self_id="bot",
        message_id="m1",
        scope="group",
        user_id="100",
        nickname="A",
        group_id="200",
    )
    t = msg.source_target
    assert t.scope == "group"
    assert t.target_id == "200"


def test_incoming_notice_defaults():
    n = IncomingNotice(
        adapter="mock",
        timestamp=1.0,
        self_id="bot",
        notice_type=NoticeType.GROUP_RECALL,
    )
    assert n.event_type == EventType.NOTICE


def test_incoming_request_defaults():
    r = IncomingRequest(
        adapter="mock",
        timestamp=1.0,
        self_id="bot",
        request_type=RequestType.FRIEND,
        flag="abc",
        user_id="100",
    )
    assert r.event_type == EventType.REQUEST


def test_media_segment_enum():
    seg = MediaSegment(type=MediaType.IMAGE, url="http://x/y.jpg")
    assert seg.type == MediaType.IMAGE


# ============================================================
# Registry 测试
# ============================================================


@pytest.mark.asyncio
async def test_register_and_get():
    reg = AdapterRegistry()
    a = MockAdapter(name="mock1")
    reg.register(a)

    assert reg.has("mock1")
    assert reg.get("mock1") is a
    assert reg.list_names() == ["mock1"]


@pytest.mark.asyncio
async def test_register_duplicate_raises():
    reg = AdapterRegistry()
    reg.register(MockAdapter(name="x"))
    with pytest.raises(ValueError, match="重复"):
        reg.register(MockAdapter(name="x"))


@pytest.mark.asyncio
async def test_get_missing_raises():
    reg = AdapterRegistry()
    with pytest.raises(KeyError):
        reg.get("nonexistent")


@pytest.mark.asyncio
async def test_default_when_single():
    reg = AdapterRegistry()
    a = MockAdapter(name="only_one")
    reg.register(a)
    assert reg.default() is a


@pytest.mark.asyncio
async def test_default_when_named_default():
    reg = AdapterRegistry()
    reg.register(MockAdapter(name="a"))
    d = MockAdapter(name="default")
    reg.register(d)
    assert reg.default() is d


@pytest.mark.asyncio
async def test_default_ambiguous_raises():
    reg = AdapterRegistry()
    reg.register(MockAdapter(name="a"))
    reg.register(MockAdapter(name="b"))
    with pytest.raises(RuntimeError, match="多个适配器"):
        reg.default()


@pytest.mark.asyncio
async def test_event_dispatch():
    reg = AdapterRegistry()
    a = MockAdapter(name="m")
    reg.register(a)

    received: list = []

    async def handler(event):
        received.append(event)

    reg.on_event(handler)

    msg = IncomingMessage(
        adapter="m",
        timestamp=1.0,
        self_id="bot",
        message_id="1",
        scope="private",
        user_id="100",
        nickname="A",
    )
    await a._emit(msg)
    assert len(received) == 1
    assert received[0] is msg


@pytest.mark.asyncio
async def test_event_without_callback_is_dropped(caplog):
    """没注册回调时事件应被记录但不崩溃。"""
    import logging
    caplog.set_level(logging.WARNING)
    reg = AdapterRegistry()
    a = MockAdapter(name="m")
    reg.register(a)

    msg = IncomingMessage(
        adapter="m",
        timestamp=1.0,
        self_id="bot",
        message_id="1",
        scope="private",
        user_id="100",
        nickname="A",
    )
    await a._emit(msg)
    assert "丢弃事件" in caplog.text


@pytest.mark.asyncio
async def test_event_callback_exception_isolated(caplog):
    """业务回调抛异常不应影响 registry。"""
    import logging
    caplog.set_level(logging.ERROR)
    reg = AdapterRegistry()
    a = MockAdapter(name="m")
    reg.register(a)

    async def bad_handler(event):
        raise RuntimeError("boom")

    reg.on_event(bad_handler)
    msg = IncomingMessage(
        adapter="m", timestamp=1.0, self_id="bot",
        message_id="1", scope="private", user_id="100", nickname="A",
    )
    # 不应抛出
    await a._emit(msg)
    assert "boom" in caplog.text or "回调处理事件失败" in caplog.text


@pytest.mark.asyncio
async def test_start_all_stop_all():
    reg = AdapterRegistry()
    a1, a2 = MockAdapter(name="x"), MockAdapter(name="y")
    reg.register(a1)
    reg.register(a2)

    assert not a1.is_connected
    await reg.start_all()
    assert a1.is_connected and a2.is_connected

    await reg.stop_all()
    assert not a1.is_connected and not a2.is_connected


# ============================================================
# 工厂注册测试
# ============================================================


def test_register_adapter_type():
    def factory(name, **kwargs):
        return MockAdapter(name=name)

    # 用唯一的类型名避免污染
    type_name = "mock_test_" + str(id(factory))
    register_adapter_type(type_name, factory)

    assert type_name in known_adapter_types()

    instance = build_adapter(name="instance1", type_name=type_name)
    assert isinstance(instance, MockAdapter)
    assert instance.name == "instance1"


def test_register_duplicate_type_raises():
    def factory(name, **kwargs):
        return MockAdapter(name=name)

    name = "dup_test_" + str(id(factory))
    register_adapter_type(name, factory)
    with pytest.raises(ValueError, match="已注册"):
        register_adapter_type(name, factory)


def test_build_unknown_type_raises():
    with pytest.raises(ValueError, match="未知适配器类型"):
        build_adapter(name="x", type_name="totally_nonexistent_type")


# ============================================================
# IAdapter 可选方法默认抛错
# ============================================================


class MinimalAdapter(IAdapter):
    """最小实现，不覆盖可选方法。"""
    async def start(self): pass
    async def stop(self): pass
    @property
    def is_connected(self): return True
    async def send_text(self, target, content): return None
    async def send_image(self, target, **kw): return None
    async def recall(self, mid): return False
    async def list_friends(self): return []
    async def list_groups(self): return []
    async def list_group_members(self, gid): return []
    async def get_user_info(self, uid): return UserInfo(user_id=uid, nickname="")
    async def handle_friend_request(self, flag, approve, remark=""): pass
    async def handle_group_request(self, flag, sub_type, approve, reason=""): pass
    async def call_api(self, action, **params): return {}


@pytest.mark.asyncio
async def test_optional_methods_default_raise():
    a = MinimalAdapter("min")
    with pytest.raises(NotImplementedError):
        await a.fetch_voice_text("1")
    with pytest.raises(NotImplementedError):
        await a.get_file_url("f")
    with pytest.raises(NotImplementedError):
        await a.upload_file(
            Target(adapter="min", scope="private", target_id="1"),
            Path("/tmp/x"),
        )
    with pytest.raises(NotImplementedError):
        await a.get_forward_msg("fwd")
    with pytest.raises(NotImplementedError):
        await a.get_group_history("100")


def test_abstract_cannot_instantiate():
    with pytest.raises(TypeError):
        IAdapter("x")  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_napcat_upload_file_always_sends_required_name(tmp_path):
    from adapters.napcat.adapter import NapCatAdapter

    class FakeApi:
        def __init__(self):
            self.calls = []

        async def call(self, action, params):
            self.calls.append((action, params))
            return {}

    adapter = object.__new__(NapCatAdapter)
    adapter.name = "napcat"
    adapter._api = FakeApi()
    file_path = tmp_path / "report.txt"
    file_path.write_text("x", encoding="utf-8")

    await NapCatAdapter.upload_file(
        adapter,
        Target(adapter="napcat", scope="private", target_id="10001"),
        file_path,
    )

    assert adapter._api.calls == [
        (
            "upload_private_file",
            {"user_id": 10001, "file": str(file_path), "name": "report.txt"},
        )
    ]
