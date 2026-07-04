"""Feature tool execution tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from features.vision.vision_service import VisionService
from providers.base import ProviderError
from tests.tools.helpers import FakeSendAdapter, _make_config
from tools import ToolContext, build_default_registry
from tools.feature_tools import send_voice_message
from tools.schemas import SendVoiceMessageArgs


@pytest.mark.asyncio
async def test_describe_image_without_vision_service():
    """未注入 vision 时，describe_image 返回未启用错误。"""
    cfg = _make_config(vision_enabled=True)  # registry 启用，但 ctx 没塞 service
    reg = build_default_registry(cfg)
    ctx = ToolContext()  # 没注入 vision
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image", {"image_url": "https://x.com/a.png"}
    )
    assert result["ok"] is False
    assert "未启用" in result["error"]


@pytest.mark.asyncio
async def test_describe_image_accepts_workspace_path(tmp_path):
    class FakeVision:
        def __init__(self) -> None:
            self.image_url = ""

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.image_url = image_url
            return "ok"

    workspace = tmp_path / "workspace"
    incoming = workspace / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    )

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision, workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "incoming/a.png"})

    assert result["ok"] is True
    assert vision.image_url.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_describe_image_missing_workspace_path_does_not_call_vision(tmp_path):
    class FakeVision:
        def __init__(self) -> None:
            self.calls = 0

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.calls += 1
            raise AssertionError("缺失 workspace 图片不应调用视觉 provider")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision, workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "incoming/missing.jpg"})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["brief"] == "图片引用不可用。"
    assert "workspace 图片不存在" in result["error"]
    assert "真实出现的 [图片 url=...]" in result["error"]
    assert "不要根据 msg_id 猜 incoming/img_*.jpg" in result["error"]
    assert result["data"]["image_ref"] == "incoming/missing.jpg"
    assert result["data"]["retry_hint"]
    assert vision.calls == 0


@pytest.mark.asyncio
async def test_describe_image_relative_path_without_workspace_does_not_call_vision():
    class FakeVision:
        def __init__(self) -> None:
            self.calls = 0

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.calls += 1
            raise AssertionError("未配置 workspace 时相对图片路径不应调用视觉 provider")

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision)
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "incoming/a.jpg"})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "workspace 未配置" in result["error"]
    assert "真实出现的 [图片 url=...]" in result["error"]
    assert result["data"]["image_ref"] == "incoming/a.jpg"
    assert vision.calls == 0


@pytest.mark.asyncio
async def test_describe_image_prefers_workspace_over_remote_url(tmp_path, monkeypatch):
    class FakeVision:
        def __init__(self) -> None:
            self.image_url = ""

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.image_url = image_url
            return "ok"

    async def fail_download(image_url: str) -> str:
        raise AssertionError("workspace 已存在时不应下载远程图片")

    monkeypatch.setattr("tools.feature_tools._download_image_as_data_url", fail_download)

    workspace = tmp_path / "workspace"
    incoming = workspace / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision, workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image",
        {
            "image_url": (
                "[图片 url=https://multimedia.nt.qq.com.cn/download?"
                "appid=1407&amp;fileid=x&amp;rkey=y workspace=incoming/a.png]"
            )
        },
    )

    assert result["ok"] is True
    assert vision.image_url.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_describe_image_missing_workspace_marker_fails_before_provider(tmp_path):
    class FakeVision:
        def __init__(self) -> None:
            self.calls = 0

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.calls += 1
            raise AssertionError("缺失 workspace 标记不应回退透传给视觉 provider")

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision, workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image",
        {
            "image_url": (
                "[图片 url=https://example.com/a.jpg "
                "workspace=incoming/missing.jpg]"
            )
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "workspace 图片不存在" in result["error"]
    assert "优先使用 workspace=" in result["error"]
    assert "改为直接传消息中真实出现的 url= 图片 URL" in result["error"]
    assert result["data"]["image_ref"] == "incoming/missing.jpg"
    assert result["data"]["message_url"] == "https://example.com/a.jpg"
    assert result["data"]["workspace_path"] == "incoming/missing.jpg"
    assert vision.calls == 0


@pytest.mark.asyncio
async def test_describe_image_localizes_qq_image_url(monkeypatch):
    class FakeVision:
        def __init__(self) -> None:
            self.image_url = ""

        async def describe(self, image_url: str, prompt: str = "") -> str:
            self.image_url = image_url
            return "ok"

    async def fake_download(image_url: str) -> str:
        assert image_url.startswith("https://multimedia.nt.qq.com.cn/download?")
        return "data:image/png;base64,ZmFrZQ=="

    monkeypatch.setattr("tools.feature_tools._download_image_as_data_url", fake_download)

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    vision = FakeVision()
    ctx = ToolContext(vision=vision)
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image",
        {"image_url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"},
    )

    assert result["ok"] is True
    assert vision.image_url == "data:image/png;base64,ZmFrZQ=="


@pytest.mark.asyncio
async def test_describe_image_qq_download_failure_does_not_call_vision(monkeypatch):
    class FakeVision:
        async def describe(self, image_url: str, prompt: str = "") -> str:
            raise AssertionError("QQ 临时图片本地化失败后不应继续调用视觉 provider")

    async def fake_download(image_url: str) -> None:
        assert image_url == (
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"
        )
        return None

    monkeypatch.setattr("tools.feature_tools._download_image_as_data_url", fake_download)

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(vision=FakeVision())
    executor = reg.get_executor(ctx)
    result = await executor(
        "describe_image",
        {
            "image_url": (
                "https://multimedia.nt.qq.com.cn/download?"
                "appid=1407&amp;fileid=x&amp;rkey=y"
            )
        },
    )

    assert result["ok"] is False
    assert result["error"] == "图片链接下载失败"
    assert result["data"]["image_ref"] == "multimedia.nt.qq.com.cn/..."


@pytest.mark.asyncio
async def test_describe_image_failure_result_is_compact():
    class FailingVision:
        async def describe(self, image_url: str, prompt: str = "") -> dict:
            raise RuntimeError(
                "vision_volcengine: API 错误: Error code: 400 - "
                "{'error': {'code': 'InvalidParameter', 'message': "
                "'Error while downloading: https://multimedia.nt.qq.com.cn/download?"
                "appid=1407&fileid=SECRET&rkey=SECRET, status code: 400 "
                "Request id: abc', 'param': 'image_url', 'type': 'BadRequest'}}"
            )

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(vision=FailingVision())
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "https://example.com/a.png"})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["brief"] == "图片理解失败。"
    assert result["summary"] == "图片识别失败"
    assert result["error"] == "视觉服务无法读取图片链接"
    dumped = json.dumps(result, ensure_ascii=False)
    assert "SECRET" not in dumped
    assert "Request id" not in dumped
    assert "vision_volcengine" not in dumped
    assert "InvalidParameter" not in dumped


@pytest.mark.asyncio
async def test_vision_service_propagates_provider_error():
    class FailingProvider:
        name = "vision"

        async def chat_completion(self, *args, **kwargs):
            raise ProviderError("raw provider failure")

    service = VisionService(FailingProvider(), model="vision-model")

    with pytest.raises(ProviderError):
        await service.describe("data:image/png;base64,ZmFrZQ==")


@pytest.mark.asyncio
async def test_describe_image_long_description_saved_once(tmp_path):
    class FakeVision:
        async def describe(self, image_url: str, prompt: str = "") -> dict:
            return {
                "summary": "胡桃和魈表情包",
                "description": "完整描述 " * 500,
            }

    workspace = tmp_path / "workspace"
    incoming = workspace / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.png").write_bytes(b"fake")

    cfg = _make_config(vision_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        vision=FakeVision(),
        workspace_dir=workspace,
        tool_result_budgets={
            "describe_image": {
                "inline_budget_tokens": 256,
                "artifact_threshold_tokens": 256,
                "hard_cap_tokens": 512,
            }
        },
    )
    executor = reg.get_executor(ctx)
    result = await executor("describe_image", {"image_url": "incoming/a.png"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert result["summary"] == "胡桃和魈表情包"
    assert "description" not in result
    assert result["full_saved"] == "incoming/a.desc.md"
    assert result["artifact"]["path"] == "incoming/a.desc.md"
    assert (workspace / "incoming" / "a.desc.md").read_text(encoding="utf-8").startswith("完整描述")
    first = dict(result)
    second = await executor("describe_image", {"image_url": "incoming/a.png"})
    assert second == first


@pytest.mark.asyncio
async def test_web_search_without_service():
    cfg = _make_config(web_search_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("web_search", {"query": "x"})
    assert result["ok"] is False
    assert "未启用" in result["error"]


@pytest.mark.asyncio
async def test_web_search_condenses_long_results():
    class FakeSearch:
        async def search(self, query: str) -> str:
            return "\n\n".join(
                f"{i}. 标题 {i}\n" + ("摘要内容 " * 80) + f"\nhttps://example.com/{i}"
                for i in range(1, 8)
            )

    cfg = _make_config(web_search_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext(
        web_search=FakeSearch(),
        tool_result_budgets={},
        tool_result_soft_limit_tokens=80,
        tool_result_hard_cap_tokens=500,
    )
    executor = reg.get_executor(ctx)
    result = await executor("web_search", {"query": "测试"})

    assert result["ok"] is True
    assert "preview" not in result
    assert result["_condensed"]["reason"] == "搜索结果过长已保留高位结果摘要"
    assert "1. 标题 1" in result["result"]
    assert "https://example.com/1" in result["result"]
    assert "7. 标题 7" not in result["result"]


@pytest.mark.asyncio
async def test_weather_without_service():
    cfg = _make_config(weather_enabled=True)
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("get_weather", {"city": "北京"})
    assert result["ok"] is False
    assert "未启用" in result["error"]


def test_send_voice_message_requires_style_prompt():
    with pytest.raises(ValidationError):
        SendVoiceMessageArgs.model_validate(
            {"target_type": "private", "target_id": 1, "text": "你好"}
        )


@pytest.mark.asyncio
async def test_send_voice_message_sends_immediately(tmp_path):
    class FakeTTS:
        async def synthesize(self, text, *, prompt):
            path = tmp_path / "voice.wav"
            path.write_bytes(b"RIFF")
            return path

    adapter = FakeSendAdapter()
    ctx = ToolContext(tts=FakeTTS(), adapter=adapter)
    args = SendVoiceMessageArgs(
        target_type="private",
        target_id=123,
        text="语音测试",
        prompt="年轻女性，自然口语",
    )

    result = await send_voice_message(args, ctx)

    assert result["ok"] is True
    assert result["sent"]["msg_id"] == "100"
    assert ctx.collected == []
    assert len(adapter.voice_sent) == 1
    target, audio_path = adapter.voice_sent[0]
    assert target.scope == "private"
    assert target.target_id == "123"
    assert audio_path.name == "voice.wav"
