"""入站媒体落盘到 workspace 的回归测试。"""

from __future__ import annotations

import pytest

from adapters.types import IncomingMessage, MediaSegment, MediaType
from core.message_pipeline import MessagePipeline


@pytest.mark.asyncio
async def test_save_media_to_workspace_copies_local_napcat_temp_file(tmp_path):
    src = tmp_path / "NapCat" / "temp" / "问卷.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-demo")
    workspace = tmp_path / "workspace"

    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = workspace

    rel = await MessagePipeline._save_media_to_workspace(
        pipeline,
        str(src),
        suggested_name="问卷.pdf",
    )

    assert rel == "incoming/问卷.pdf"
    assert (workspace / rel).read_bytes() == b"%PDF-demo"


@pytest.mark.asyncio
async def test_save_media_to_workspace_unescapes_remote_image_url(tmp_path, monkeypatch):
    seen: list[str] = []

    class FakeResponse:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            seen.append(url)
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = tmp_path / "workspace"

    rel = await MessagePipeline._save_media_to_workspace(
        pipeline,
        "https://multimedia.nt.qq.com.cn/download?appid=1407&amp;fileid=x&amp;rkey=y",
        suggested_name="img_1.jpg",
    )

    assert seen == [
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"
    ]
    assert rel == "incoming/img_1.jpg"
    assert (pipeline.workspace_dir / rel).read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_build_readable_text_prefers_adapter_image_file_over_remote_url(
    tmp_path,
    monkeypatch,
):
    src = tmp_path / "NapCat" / "temp" / "abc.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"image-bytes")
    remote_url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"

    class FakeAdapter:
        async def get_image_url(self, file_id):
            assert file_id == "abc.jpg"
            return str(src)

    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str):
            raise AssertionError(f"不应下载远程图片链接: {url}")

    monkeypatch.setattr("httpx.AsyncClient", FailingClient)

    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = tmp_path / "workspace"
    pipeline.adapter = FakeAdapter()
    pipeline.asr = None

    event = IncomingMessage(
        adapter="napcat",
        timestamp=0,
        self_id="1",
        message_id="42",
        scope="group",
        user_id="10001",
        nickname="Alice",
        group_id="20002",
        text="[图片]",
        raw_message="[CQ:image,file=abc.jpg,url=https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y]",
        media=[
            MediaSegment(
                type=MediaType.IMAGE,
                file_id="abc.jpg",
                url=remote_url,
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert text.startswith("[图片 workspace=incoming/img_42.jpg url=https://multimedia.nt.qq.com.cn/")
    assert (pipeline.workspace_dir / "incoming" / "img_42.jpg").read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_build_readable_text_falls_back_to_remote_url_when_adapter_image_file_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    remote_url = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y"
    seen: list[str] = []

    class FakeAdapter:
        async def get_image_url(self, file_id):
            assert file_id == "abc.jpg"
            return "abc.jpg"

    class FakeResponse:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            seen.append(url)
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = tmp_path / "workspace"
    pipeline.adapter = FakeAdapter()
    pipeline.asr = None

    event = IncomingMessage(
        adapter="napcat",
        timestamp=0,
        self_id="1",
        message_id="42",
        scope="group",
        user_id="10001",
        nickname="Alice",
        group_id="20002",
        text="[图片]",
        raw_message="[CQ:image,file=abc.jpg,url=https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x&rkey=y]",
        media=[
            MediaSegment(
                type=MediaType.IMAGE,
                file_id="abc.jpg",
                url=remote_url,
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert seen == [remote_url]
    assert text.startswith("[图片 workspace=incoming/img_42.jpg url=https://multimedia.nt.qq.com.cn/")
    assert (pipeline.workspace_dir / "incoming" / "img_42.jpg").read_bytes() == b"image-bytes"


@pytest.mark.asyncio
async def test_build_readable_text_exposes_file_workspace_path(tmp_path):
    src = tmp_path / "NapCat" / "temp" / "问卷.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-demo")

    class FakeAdapter:
        async def get_file_url(self, file_id):
            return str(src)

    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = tmp_path / "workspace"
    pipeline.adapter = FakeAdapter()
    pipeline.asr = None

    event = IncomingMessage(
        adapter="napcat",
        timestamp=0,
        self_id="1",
        message_id="42",
        scope="private",
        user_id="10001",
        nickname="Alice",
        text="[文件]",
        raw_message="[CQ:file,file=abc,file_name=问卷.pdf]",
        media=[
            MediaSegment(
                type=MediaType.FILE,
                file_id="abc",
                name="问卷.pdf",
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert "workspace=incoming/问卷.pdf" in text
    assert (pipeline.workspace_dir / "incoming" / "问卷.pdf").read_bytes() == b"%PDF-demo"


@pytest.mark.asyncio
async def test_build_readable_text_appends_forward_placeholder_when_text_empty(tmp_path):
    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = tmp_path / "workspace"
    pipeline.asr = None
    pipeline.adapter = object()

    event = IncomingMessage(
        adapter="napcat",
        timestamp=0,
        self_id="1",
        message_id="42",
        scope="private",
        user_id="10001",
        nickname="Alice",
        text="",
        raw_message="",
        media=[MediaSegment(type=MediaType.FORWARD, file_id="fwd123")],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert "[合并转发 id=fwd123]" in text
    assert 'get_forward_msg(forward_id="fwd123")' in text


@pytest.mark.asyncio
async def test_build_readable_text_includes_forward_node_preview(tmp_path):
    class FakeAdapter:
        async def get_forward_msg(self, forward_id: str):
            assert forward_id == "fwd123"
            return [
                {
                    "sender": {"nickname": "Alice", "user_id": "10001"},
                    "message_id": "node-1",
                    "message": [
                        {"type": "text", "data": {"text": "转发里的第一条"}},
                    ],
                },
                {
                    "sender": {"nickname": "Bob", "user_id": "10002"},
                    "message_id": "node-2",
                    "raw_message": "转发里的第二条",
                },
            ]

    pipeline = MessagePipeline.__new__(MessagePipeline)
    pipeline.workspace_dir = tmp_path / "workspace"
    pipeline.adapter = FakeAdapter()
    pipeline.asr = None

    event = IncomingMessage(
        adapter="napcat",
        timestamp=0,
        self_id="1",
        message_id="42",
        scope="private",
        user_id="10001",
        nickname="Alice",
        text="[合并转发 id=fwd123 title=聊天记录]",
        raw_message="",
        media=[
            MediaSegment(
                type=MediaType.FORWARD,
                file_id="fwd123",
                extra={"title": "聊天记录"},
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert "[合并转发 id=fwd123 title=聊天记录]" in text
    assert "节点预览" in text
    assert "转发里的第一条" in text
    assert "转发里的第二条" in text
    assert 'get_forward_msg(forward_id="fwd123")' in text
