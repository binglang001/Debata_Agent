"""测试 NapCatAdapter + api_call —— 用 FakeConnection 替代真实 WebSocket。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.base import AdapterAPIError, AdapterNotConnectedError
from adapters.napcat.adapter import NapCatAdapter
from adapters.napcat.api_call import NapCatApiCaller
from adapters.napcat.connection import (
    ConnectionStatus,
    ForwardWSConnection,
    NapCatConnection,
    ReverseWSConnection,
)
from adapters.napcat.process import NapCatProcessManager
from adapters.types import (
    FriendInfo,
    GroupInfo,
    IncomingMessage,
    Target,
)
from core.state import RateLimiter

# ============================================================
# FakeConnection：内存版连接，不开真实 WebSocket
# ============================================================


class FakeConnection(NapCatConnection):
    """模拟 NapCat 连接：sent 列表记录所有发出的消息，
    可以通过 simulate_receive() 注入 NapCat 上报的数据。"""

    def __init__(self) -> None:
        super().__init__()
        self._connected = False
        self.sent: list[dict] = []
        # 自动响应器：根据请求的 action 返回固定数据
        self.responders: dict[str, dict[str, Any]] = {}
        self._connected_event = asyncio.Event()
        self._status = ConnectionStatus(
            state="idle",
            connected=False,
            attempt=0,
            last_connected_at=None,
            last_disconnected_at=None,
            last_error=None,
            endpoint="fake",
        )

    async def start(self) -> None:
        self._connected = True
        self._connected_event.set()
        self._status = ConnectionStatus(
            state="connected",
            connected=True,
            attempt=1,
            last_connected_at=None,
            last_disconnected_at=None,
            last_error=None,
            endpoint="fake",
        )

    async def stop(self) -> None:
        self._connected = False
        self._connected_event.clear()
        self._notify_connection_lost()

    async def send(self, data: dict) -> None:
        self.sent.append(data)
        # 如果有自动响应器，模拟 NapCat 回包
        action = data.get("action")
        echo = data.get("echo")
        if action in self.responders and echo:
            response = {
                "status": "ok",
                "retcode": 0,
                "data": self.responders[action],
                "echo": echo,
            }
            # 异步投递（模拟网络延迟）
            asyncio.create_task(self._dispatch(response))

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    async def wait_connected(self, timeout: float | None = None) -> bool:
        if self._connected:
            return True
        if timeout is not None and timeout <= 0:
            return self._connected
        try:
            if timeout is None:
                await self._connected_event.wait()
            else:
                await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self._connected
        return self._connected

    async def simulate_receive(self, data: dict) -> None:
        """模拟从 NapCat 收到一条消息。"""
        await self._dispatch(data)


def test_process_manager_uses_cmd_for_windows_bat(tmp_path, monkeypatch):
    script = tmp_path / "start.bat"
    script.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr("adapters.napcat.process.sys.platform", "win32")
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\System32\\cmd.exe")

    manager = NapCatProcessManager(script, args=["--demo"])

    assert manager._build_command() == [
        "C:\\Windows\\System32\\cmd.exe",
        "/c",
        str(script),
        "--demo",
    ]


@pytest.mark.asyncio
async def test_process_manager_uses_taskkill_tree_on_windows(tmp_path, monkeypatch):
    script = tmp_path / "start.bat"
    script.write_text("@echo off\n", encoding="utf-8")
    manager = NapCatProcessManager(script)
    manager._process = SimpleNamespace(pid=4321)  # type: ignore[assignment]
    calls: list[list[str]] = []

    class FakeTaskkillProcess:
        returncode = 0

        async def communicate(self):
            return b"ok", None

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["stderr"] is asyncio.subprocess.STDOUT
        return FakeTaskkillProcess()

    monkeypatch.setattr(
        "adapters.napcat.process.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    await manager._terminate_windows_tree(force=False)
    await manager._terminate_windows_tree(force=True)

    assert calls == [
        ["taskkill", "/PID", "4321", "/T"],
        ["taskkill", "/F", "/PID", "4321", "/T"],
    ]


# ============================================================
# API 调用层测试
# ============================================================


def _bridge_caller_to_conn(api: NapCatApiCaller, conn: FakeConnection) -> None:
    """让 FakeConnection 的响应能流到 NapCatApiCaller（单独测试 caller 时用）。"""

    async def _bridge(data: dict) -> None:
        api.handle_response(data)

    conn.on_message(_bridge)


@pytest.mark.asyncio
async def test_api_call_roundtrip():
    conn = FakeConnection()
    await conn.start()
    api = NapCatApiCaller(conn)
    _bridge_caller_to_conn(api, conn)

    conn.responders["test_action"] = {"result": 42}
    result = await api.call("test_action", {"x": 1})

    assert result == {"result": 42}
    assert len(conn.sent) == 1
    assert conn.sent[0]["action"] == "test_action"
    assert conn.sent[0]["params"] == {"x": 1}
    assert "echo" in conn.sent[0]


@pytest.mark.asyncio
async def test_api_call_not_connected_raises():
    conn = FakeConnection()
    # 不调 start()
    api = NapCatApiCaller(conn, wait_connected_timeout=0)

    with pytest.raises(AdapterNotConnectedError):
        await api.call("send_msg", {})


@pytest.mark.asyncio
async def test_api_call_waits_for_connection_before_send():
    conn = FakeConnection()
    api = NapCatApiCaller(conn, wait_connected_timeout=0.2)
    _bridge_caller_to_conn(api, conn)

    async def connect_later():
        await asyncio.sleep(0.02)
        await conn.start()
        conn.responders["send_msg"] = {"message_id": 123}

    asyncio.create_task(connect_later())
    result = await api.call("send_msg", {"message": "hi"}, timeout=1.0)

    assert result == {"message_id": 123}
    assert conn.sent[-1]["action"] == "send_msg"


@pytest.mark.asyncio
async def test_api_call_wait_connected_timeout_is_clear():
    conn = FakeConnection()
    api = NapCatApiCaller(conn, wait_connected_timeout=0.01)

    with pytest.raises(AdapterNotConnectedError, match="等待 0.0s 后仍无法调用"):
        await api.call("send_msg", {})


@pytest.mark.asyncio
async def test_api_call_timeout():
    conn = FakeConnection()
    await conn.start()
    api = NapCatApiCaller(conn, default_timeout=0.1)
    # 不注册响应器，让请求超时

    with pytest.raises(asyncio.TimeoutError):
        await api.call("never_responds", {})


@pytest.mark.asyncio
async def test_api_call_returns_error_status():
    conn = FakeConnection()
    await conn.start()
    api = NapCatApiCaller(conn, default_timeout=1.0)
    _bridge_caller_to_conn(api, conn)

    # 手动构造一个失败响应
    async def respond_failed():
        await asyncio.sleep(0.01)
        # 找到最新的 echo
        await conn._dispatch({
            "status": "failed",
            "retcode": 100,
            "message": "test error",
            "echo": conn.sent[-1]["echo"],
        })

    asyncio.create_task(respond_failed())
    with pytest.raises(AdapterAPIError, match="test error"):
        await api.call("bad_action", {})


@pytest.mark.asyncio
async def test_api_concurrent_calls():
    """多个并发调用 echo 不应混淆。"""
    conn = FakeConnection()
    await conn.start()
    api = NapCatApiCaller(conn, default_timeout=1.0)
    _bridge_caller_to_conn(api, conn)

    conn.responders["a"] = {"v": "a"}
    conn.responders["b"] = {"v": "b"}
    conn.responders["c"] = {"v": "c"}

    results = await asyncio.gather(
        api.call("a"), api.call("b"), api.call("c")
    )
    assert results == [{"v": "a"}, {"v": "b"}, {"v": "c"}]


@pytest.mark.asyncio
async def test_discard_pending_on_disconnect():
    conn = FakeConnection()
    await conn.start()
    api = NapCatApiCaller(conn, default_timeout=10.0)

    # 发起请求但不响应
    task = asyncio.create_task(api.call("hangs"))
    await asyncio.sleep(0.01)
    # 模拟断线
    api.discard_pending()

    with pytest.raises(AdapterNotConnectedError):
        await task


@pytest.mark.asyncio
async def test_adapter_discards_pending_when_connection_lost():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    task = asyncio.create_task(adapter.call_api("hangs"))
    while not conn.sent:
        await asyncio.sleep(0.001)
    await conn.stop()

    with pytest.raises(AdapterNotConnectedError):
        await task


def test_adapter_from_config_uses_startup_connect_timeout():
    class FakeSecrets:
        def get(self, _key: str) -> None:
            return None

    from app_config.schema import NapCatAdapterConfig

    cfg = NapCatAdapterConfig(
        mode="client",
        initial_connect_timeout_seconds=10.0,
        startup_connect_timeout_seconds=2.5,
        api_wait_connected_timeout_seconds=1.25,
        fast_reconnect_attempts=7,
        fast_reconnect_interval_seconds=0.1,
        reconnect_jitter_seconds=0.0,
    )

    adapter = NapCatAdapter.from_config("napcat_test", cfg, FakeSecrets())  # type: ignore[arg-type]

    conn = adapter._connection  # type: ignore[attr-defined]
    assert isinstance(conn, ReverseWSConnection)
    assert conn.initial_connect_timeout == 2.5
    assert conn.fast_reconnect_attempts == 7
    assert conn.fast_reconnect_interval == 0.1
    assert conn.reconnect_jitter == 0.0
    assert adapter._api.wait_connected_timeout == 1.25  # type: ignore[attr-defined]


def test_adapter_from_config_server_loopback_binds_all_interfaces():
    class FakeSecrets:
        def get(self, _key: str) -> None:
            return None

    from app_config.schema import NapCatAdapterConfig

    cfg = NapCatAdapterConfig(mode="server", host="localhost", port=8082, path="/")

    adapter = NapCatAdapter.from_config("napcat_test", cfg, FakeSecrets())  # type: ignore[arg-type]

    conn = adapter._connection  # type: ignore[attr-defined]
    assert isinstance(conn, ForwardWSConnection)
    assert conn.host == "0.0.0.0"
    assert conn.port == 8082
    assert conn.path == "/"


# ============================================================
# NapCatAdapter 测试
# ============================================================


@pytest.mark.asyncio
async def test_adapter_send_text_private():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    conn.responders["send_msg"] = {"message_id": 12345}
    target = Target(adapter="napcat_test", scope="private", target_id="1001")

    msg_id = await adapter.send_text(target, "你好")
    assert msg_id == "12345"

    assert conn.sent[-1]["action"] == "send_msg"
    assert conn.sent[-1]["params"]["message_type"] == "private"
    assert conn.sent[-1]["params"]["user_id"] == 1001
    assert conn.sent[-1]["params"]["message"] == "你好"


@pytest.mark.asyncio
async def test_adapter_send_text_waits_for_connection():
    conn = FakeConnection()
    adapter = NapCatAdapter(
        "napcat_test",
        conn,
        api_wait_connected_timeout_seconds=0.2,
    )
    target = Target(adapter="napcat_test", scope="private", target_id="1001")

    async def connect_later():
        await asyncio.sleep(0.02)
        await conn.start()
        conn.responders["send_msg"] = {"message_id": 456}

    asyncio.create_task(connect_later())
    msg_id = await adapter.send_text(target, "启动后第一条")

    assert msg_id == "456"
    assert conn.sent[-1]["params"]["message"] == "启动后第一条"


@pytest.mark.asyncio
async def test_adapter_send_text_group():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    conn.responders["send_msg"] = {"message_id": 7777}
    target = Target(adapter="napcat_test", scope="group", target_id="200")

    msg_id = await adapter.send_text(target, "大家好")
    assert msg_id == "7777"
    assert conn.sent[-1]["params"]["message_type"] == "group"
    assert conn.sent[-1]["params"]["group_id"] == 200


@pytest.mark.asyncio
async def test_adapter_send_image_url():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    conn.responders["send_msg"] = {"message_id": 1}
    target = Target(adapter="napcat_test", scope="private", target_id="1001")

    await adapter.send_image(target, image_url="http://x/y.jpg")
    assert "[CQ:image,file=http://x/y.jpg]" in conn.sent[-1]["params"]["message"]


@pytest.mark.asyncio
async def test_adapter_recall():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    conn.responders["delete_msg"] = {}

    ok = await adapter.recall("99999")
    assert ok is True
    assert conn.sent[-1]["params"]["message_id"] == 99999


@pytest.mark.asyncio
async def test_adapter_list_friends():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    # NapCat 返回直接是 list
    async def respond_list():
        await asyncio.sleep(0.01)
        await conn._dispatch({
            "status": "ok",
            "retcode": 0,
            "data": [
                {"user_id": 1001, "nickname": "Alice", "remark": "A"},
                {"user_id": 1002, "nickname": "Bob", "remark": ""},
            ],
            "echo": conn.sent[-1]["echo"] if conn.sent else "",
        })

    # 简化：用 responders 但是 list 形式
    # 这里我们要发起请求并响应
    task = asyncio.create_task(adapter.list_friends())
    await asyncio.sleep(0.005)
    # 等 send 已经发生
    while not conn.sent:
        await asyncio.sleep(0.001)
    await conn._dispatch({
        "status": "ok",
        "retcode": 0,
        "data": [
            {"user_id": 1001, "nickname": "Alice", "remark": "A"},
            {"user_id": 1002, "nickname": "Bob", "remark": ""},
        ],
        "echo": conn.sent[-1]["echo"],
    })
    friends = await task
    assert len(friends) == 2
    assert friends[0] == FriendInfo(user_id="1001", nickname="Alice", remark="A")


@pytest.mark.asyncio
async def test_adapter_list_groups():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    task = asyncio.create_task(adapter.list_groups())
    while not conn.sent:
        await asyncio.sleep(0.001)
    await conn._dispatch({
        "status": "ok",
        "retcode": 0,
        "data": [{"group_id": 200, "group_name": "Test", "member_count": 5}],
        "echo": conn.sent[-1]["echo"],
    })
    groups = await task
    assert len(groups) == 1
    assert groups[0] == GroupInfo(group_id="200", group_name="Test", member_count=5)


@pytest.mark.asyncio
async def test_adapter_list_group_members():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    task = asyncio.create_task(adapter.list_group_members("200"))
    while not conn.sent:
        await asyncio.sleep(0.001)
    await conn._dispatch({
        "status": "ok",
        "retcode": 0,
        "data": [
            {"user_id": 1001, "nickname": "Alice", "card": "Cap", "role": "admin"},
        ],
        "echo": conn.sent[-1]["echo"],
    })
    members = await task
    assert len(members) == 1
    assert members[0].role == "admin"
    assert members[0].card == "Cap"


@pytest.mark.asyncio
async def test_adapter_get_user_info():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    conn.responders["get_stranger_info"] = {
        "user_id": 1001,
        "nickname": "Alice",
        "sex": "female",
        "age": 25,
        "level": "5",
    }
    info = await adapter.get_user_info("1001")
    assert info.nickname == "Alice"
    assert info.sex == "female"
    assert info.age == 25
    assert info.extra.get("level") == "5"


@pytest.mark.asyncio
async def test_adapter_handle_friend_request():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    conn.responders["set_friend_add_request"] = {}

    result = await adapter.handle_friend_request("flag123", True, "好友")
    assert result["status"] == "done"
    assert conn.sent[-1]["params"] == {
        "flag": "flag123",
        "approve": True,
        "remark": "好友",
    }


@pytest.mark.asyncio
async def test_adapter_drops_friend_request_event_when_user_is_already_friend():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    limiter = RateLimiter(window_seconds=60, max_messages=0)
    adapter.set_friend_confirmed_callback(limiter.remember_friend)
    conn.responders["get_friend_list"] = [
        {"user_id": 1001, "nickname": "Alice", "remark": ""},
    ]
    received: list[Any] = []

    async def handler(event):
        received.append(event)

    adapter.subscribe(handler)
    await conn.simulate_receive(
        {
            "post_type": "request",
            "request_type": "friend",
            "user_id": 1001,
            "comment": "想加你",
            "flag": "flag123",
            "self_id": 9999,
            "time": 1,
        }
    )

    assert received == []
    assert conn.sent[-1]["action"] == "get_friend_list"
    assert await limiter.check_and_log("1001") is False


@pytest.mark.asyncio
async def test_adapter_friend_request_missing_but_already_friend_returns_success():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    adapter._friend_request_users_by_flag["flag123"] = "1001"

    task = asyncio.create_task(adapter.handle_friend_request("flag123", True, "好友"))
    while len(conn.sent) < 1:
        await asyncio.sleep(0.001)
    await conn._dispatch(
        {
            "status": "failed",
            "retcode": 1404,
            "message": "好友请求不存在",
            "echo": conn.sent[-1]["echo"],
        }
    )
    while len(conn.sent) < 2:
        await asyncio.sleep(0.001)
    assert conn.sent[-1]["action"] == "get_friend_list"
    await conn._dispatch(
        {
            "status": "ok",
            "retcode": 0,
            "data": [{"user_id": 1001, "nickname": "Alice", "remark": ""}],
            "echo": conn.sent[-1]["echo"],
        }
    )

    result = await task
    assert result["status"] == "already_friend"
    assert result["already_handled"] is True
    assert result["user_id"] == "1001"


@pytest.mark.asyncio
async def test_adapter_friend_request_missing_and_not_friend_stays_failed():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    adapter._friend_request_users_by_flag["flag123"] = "1001"

    task = asyncio.create_task(adapter.handle_friend_request("flag123", True, "好友"))
    while len(conn.sent) < 1:
        await asyncio.sleep(0.001)
    await conn._dispatch(
        {
            "status": "failed",
            "retcode": 1404,
            "message": "好友请求不存在",
            "echo": conn.sent[-1]["echo"],
        }
    )
    while len(conn.sent) < 2:
        await asyncio.sleep(0.001)
    await conn._dispatch(
        {
            "status": "ok",
            "retcode": 0,
            "data": [{"user_id": 2002, "nickname": "Bob", "remark": ""}],
            "echo": conn.sent[-1]["echo"],
        }
    )

    with pytest.raises(AdapterAPIError):
        await task


@pytest.mark.asyncio
async def test_adapter_handle_group_request():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    conn.responders["set_group_add_request"] = {}

    await adapter.handle_group_request("flag", "add", False, "不通过")
    p = conn.sent[-1]["params"]
    assert p["sub_type"] == "add"
    assert p["approve"] is False
    assert p["reason"] == "不通过"


@pytest.mark.asyncio
async def test_adapter_event_emission():
    """模拟 NapCat 上报消息事件 → 业务回调应被调用。"""
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    received: list = []

    async def handler(event):
        received.append(event)

    adapter.subscribe(handler)

    # 模拟收到一条私聊消息
    await conn.simulate_receive(
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 1,
            "user_id": 1001,
            "self_id": 9999,
            "time": 1,
            "raw_message": "你好",
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "sender": {"user_id": 1001, "nickname": "Alice"},
        }
    )
    await asyncio.sleep(0.01)  # 让 task 跑完

    assert len(received) == 1
    assert isinstance(received[0], IncomingMessage)
    assert received[0].text == "你好"


@pytest.mark.asyncio
async def test_adapter_api_response_not_emitted_as_event():
    """API 响应（含 echo）不应作为事件投递给业务层。"""
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    received: list = []
    adapter.subscribe(lambda e: received.append(e))  # type: ignore

    await conn.simulate_receive(
        {"status": "ok", "retcode": 0, "data": {}, "echo": "abc"}
    )
    await asyncio.sleep(0.01)

    assert len(received) == 0


@pytest.mark.asyncio
async def test_adapter_call_api_passthrough():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    conn.responders["any_custom"] = {"foo": "bar"}

    result = await adapter.call_api("any_custom", k="v")
    assert result == {"foo": "bar"}
    assert conn.sent[-1]["action"] == "any_custom"
    assert conn.sent[-1]["params"] == {"k": "v"}


@pytest.mark.asyncio
async def test_adapter_get_file_url_success():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    conn.responders["get_file"] = {"url": "http://files/abc"}

    url = await adapter.get_file_url("fid")
    assert url == "http://files/abc"


@pytest.mark.asyncio
async def test_adapter_get_file_url_failure_returns_none():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()

    async def respond_failed():
        while not conn.sent:
            await asyncio.sleep(0.001)
        await conn._dispatch({
            "status": "failed",
            "retcode": 1,
            "message": "no such file",
            "echo": conn.sent[-1]["echo"],
        })

    asyncio.create_task(respond_failed())
    url = await adapter.get_file_url("missing")
    assert url is None


@pytest.mark.asyncio
async def test_adapter_get_image_url_success():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    conn.responders["get_image"] = {"file": "D:\\QQ\\NapCat\\temp\\abc.jpg"}

    url = await adapter.get_image_url("abc.jpg")

    assert url == "D:\\QQ\\NapCat\\temp\\abc.jpg"
    assert conn.sent[-1]["action"] == "get_image"
    assert conn.sent[-1]["params"] == {"file": "abc.jpg"}


@pytest.mark.asyncio
async def test_fetch_voice_text_retries_pending_result():
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn, voice_fetch_delay_seconds=0)
    await adapter.start()

    async def respond():
        while len(conn.sent) < 1:
            await asyncio.sleep(0.001)
        await conn._dispatch({
            "status": "failed",
            "retcode": 1200,
            "message": "获取语音转文字结果失败",
            "echo": conn.sent[-1]["echo"],
        })
        while len(conn.sent) < 2:
            await asyncio.sleep(0.001)
        await conn._dispatch({
            "status": "ok",
            "retcode": 0,
            "data": {"text": "识别好了"},
            "echo": conn.sent[-1]["echo"],
        })

    asyncio.create_task(respond())
    text = await adapter.fetch_voice_text("123")

    assert text == "识别好了"
    assert [c["action"] for c in conn.sent] == ["fetch_ptt_text", "fetch_ptt_text"]


@pytest.mark.asyncio
async def test_send_voice_timeout_is_treated_as_possible_success(tmp_path):
    conn = FakeConnection()
    adapter = NapCatAdapter("napcat_test", conn)
    await adapter.start()
    wav = tmp_path / "voice.wav"
    wav.write_bytes(b"RIFF")

    async def respond_timeout():
        while not conn.sent:
            await asyncio.sleep(0.001)
        await conn._dispatch({
            "status": "failed",
            "retcode": 1200,
            "message": "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg",
            "echo": conn.sent[-1]["echo"],
        })

    asyncio.create_task(respond_timeout())
    mid = await adapter.send_voice(
        Target(adapter="napcat_test", scope="private", target_id="10001"),
        wav,
    )

    assert mid is None
