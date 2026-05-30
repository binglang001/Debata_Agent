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
