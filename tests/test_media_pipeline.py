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
async def test_build_readable_text_falls_back_file_url_when_container_path_unreadable(tmp_path):
    src = tmp_path / "NapCat" / "temp" / "连炳宇.md"
    src.parent.mkdir(parents=True)
    src.write_text("# demo", encoding="utf-8")
    calls: list[str] = []

    class FakeAdapter:
        async def get_file_url(self, file_id):
            calls.append(file_id)
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
        raw_message="[CQ:file,file=file123,file_name=连炳宇.md,url=/app/.config/QQ/NapCat/temp/连炳宇.md]",
        media=[
            MediaSegment(
                type=MediaType.FILE,
                file_id="file123",
                url="/app/.config/QQ/NapCat/temp/连炳宇.md",
                name="连炳宇.md",
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert calls == ["file123"]
    assert "url=/app/.config/QQ/NapCat/temp/连炳宇.md" in text
    assert "workspace=incoming/连炳宇.md" in text
    assert (pipeline.workspace_dir / "incoming" / "连炳宇.md").read_text(encoding="utf-8") == "# demo"


@pytest.mark.asyncio
async def test_build_readable_text_falls_back_file_url_by_name_without_file_id(tmp_path):
    src = tmp_path / "NapCat" / "temp" / "AI 研究路线.md"
    src.parent.mkdir(parents=True)
    src.write_text("# route", encoding="utf-8")
    calls: list[str] = []

    class FakeAdapter:
        async def get_file_url(self, file_id):
            calls.append(file_id)
            if file_id == "AI 研究路线.md":
                return str(src)
            return None

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
        raw_message="[CQ:file,url=/app/.config/QQ/NapCat/temp/AI 研究路线.md]",
        media=[
            MediaSegment(
                type=MediaType.FILE,
                url="/app/.config/QQ/NapCat/temp/AI 研究路线.md",
                name="AI 研究路线.md",
                file_id=None,
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert calls == ["AI 研究路线.md"]
    assert "url=/app/.config/QQ/NapCat/temp/AI 研究路线.md" in text
    assert "workspace=incoming/AI_研究路线.md" in text
    assert (pipeline.workspace_dir / "incoming" / "AI_研究路线.md").read_text(encoding="utf-8") == "# route"


@pytest.mark.asyncio
async def test_build_readable_text_falls_back_file_url_by_name_after_file_id_path_unreadable(
    tmp_path,
):
    src = tmp_path / "NapCat" / "temp" / "good.md"
    src.parent.mkdir(parents=True)
    src.write_text("# good", encoding="utf-8")
    calls: list[str] = []

    class FakeAdapter:
        async def get_file_url(self, file_id):
            calls.append(file_id)
            if file_id == "file123":
                return "/app/.config/QQ/NapCat/temp/bad.md"
            if file_id == "good.md":
                return str(src)
            return None

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
        raw_message="[CQ:file,file=file123,file_name=good.md]",
        media=[
            MediaSegment(
                type=MediaType.FILE,
                file_id="file123",
                name="good.md",
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert calls == ["file123", "good.md"]
    assert "url=/app/.config/QQ/NapCat/temp/bad.md" in text
    assert "workspace=incoming/good.md" in text
    assert (pipeline.workspace_dir / "incoming" / "good.md").read_text(encoding="utf-8") == "# good"


@pytest.mark.asyncio
async def test_build_readable_text_does_not_fetch_file_url_when_local_path_readable(tmp_path):
    src = tmp_path / "NapCat" / "temp" / "问卷.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-demo")

    class FakeAdapter:
        async def get_file_url(self, file_id):
            raise AssertionError(f"不应获取文件 URL: {file_id}")

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
        raw_message="[CQ:file,file=file123,file_name=问卷.pdf]",
        media=[
            MediaSegment(
                type=MediaType.FILE,
                file_id="file123",
                url=str(src),
                name="问卷.pdf",
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert "workspace=incoming/问卷.pdf" in text
    assert (pipeline.workspace_dir / "incoming" / "问卷.pdf").read_bytes() == b"%PDF-demo"


@pytest.mark.asyncio
async def test_build_readable_text_does_not_fetch_file_url_when_http_file_saved(
    tmp_path,
    monkeypatch,
):
    remote_url = "https://example.test/files/report.md"
    seen: list[str] = []

    class FakeAdapter:
        async def get_file_url(self, file_id):
            raise AssertionError(f"不应获取文件 URL: {file_id}")

    class FakeResponse:
        content = b"# report"

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
        scope="private",
        user_id="10001",
        nickname="Alice",
        text="[文件]",
        raw_message="[CQ:file,file=file123,file_name=report.md,url=https://example.test/files/report.md]",
        media=[
            MediaSegment(
                type=MediaType.FILE,
                file_id="file123",
                url=remote_url,
                name="report.md",
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert seen == [remote_url]
    assert f"url={remote_url}" in text
    assert "workspace=incoming/report.md" in text
    assert (pipeline.workspace_dir / "incoming" / "report.md").read_bytes() == b"# report"


@pytest.mark.asyncio
async def test_build_readable_text_unreadable_file_path_without_file_id_tries_name_and_basename(
    tmp_path,
):
    calls: list[str] = []

    class FakeAdapter:
        async def get_file_url(self, file_id):
            calls.append(file_id)
            return None

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
        raw_message="[CQ:file,file_name=missing.md,url=/app/.config/QQ/NapCat/temp/missing.md]",
        media=[
            MediaSegment(
                type=MediaType.FILE,
                url="/app/.config/QQ/NapCat/temp/missing.md",
                name="missing.md",
            )
        ],
    )

    text = await MessagePipeline._build_readable_text(pipeline, event)

    assert calls == ["missing.md"]
    assert "url=/app/.config/QQ/NapCat/temp/missing.md" in text
    assert "workspace=" not in text
    assert not (pipeline.workspace_dir / "incoming" / "missing.md").exists()


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
