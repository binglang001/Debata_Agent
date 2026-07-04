"""测试工具系统：schema 派生 / Registry 启用禁用 / 工具执行。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from tests.tools.helpers import _assert_tool_result_envelope, _FakeAdapter, _make_config
from tools import (
    ToolContext,
    ToolRegistry,
    build_default_registry,
    get_default_specs,
)
from tools.base import tool
from tools.result_shrink import tool_budget


def test_tool_budget_uses_default_per_tool_values():
    ctx = ToolContext()

    budget = tool_budget("read_file", ctx)

    assert budget.inline == 2500
    assert budget.artifact_threshold == 2500
    assert budget.hard_cap >= 2500


def test_tool_budget_falls_back_to_global_default_for_unknown_tool():
    ctx = ToolContext()

    budget = tool_budget("unknown_tool", ctx)

    assert budget.inline == 800
    assert budget.artifact_threshold == 800
    assert budget.hard_cap == 3000


def test_tool_context_empty_constructs_with_persona_fields():
    ctx = ToolContext()

    assert ctx.persona_agent is None
    assert ctx.subconscious_agent is None
    assert ctx.persona_db is None


def test_tool_budget_keeps_legacy_override_when_no_new_budget_exists():
    ctx = ToolContext(
        tool_result_budgets={},
        tool_result_soft_limit_tokens=700,
        tool_result_hard_cap_tokens=1600,
        tool_result_soft_overrides={"read_file": 900},
    )

    budget = tool_budget("read_file", ctx)

    assert budget.inline == 900
    assert budget.artifact_threshold == 900
    assert budget.hard_cap == 1600


# ============================================================
# Schema 自动派生
# ============================================================


# ============================================================
# 所有工具注册了
# ============================================================


# ============================================================
# build_default_registry: 按配置筛选
# ============================================================


@pytest.mark.asyncio
async def test_stub_tool_requires_tool_search_before_execution(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "x.txt"
    file_path.write_text("x", encoding="utf-8")
    adapter = _FakeAdapter()
    ctx = ToolContext(adapter=adapter, workspace_dir=workspace)
    executor = reg.get_executor(ctx)

    blocked = await executor(
        "upload_file",
        {"target_type": "group", "target_id": 1, "file_path": "x.txt"},
    )
    assert blocked["ok"] is False
    assert blocked["status"] == "need_tool_search"
    _assert_tool_result_envelope(blocked, "upload_file")

    details = await executor("tool_search", {"tool_name": "upload_file", "intent": "发送文件"})
    assert details["ok"] is True
    assert details["status"] == "found"
    _assert_tool_result_envelope(details, "tool_search")
    assert details["result_type"] == "tool_metadata"
    assert "工具元数据" in details["brief"]
    assert details["tool_name"] == "upload_file"
    assert "parameters_schema" not in details
    assert "parameters" in details
    assert "parameter_summary" in details
    assert {"constraints", "examples", "risk_level", "next"}.issubset(details)
    assert "file_path" in details["required_fields"]
    parameter_by_name = {item["name"]: item for item in details["parameters"]}
    assert "file_path" in parameter_by_name
    assert "target_type" in parameter_by_name
    assert "target_id" in parameter_by_name
    assert parameter_by_name["target_type"]["enum"] == ["private", "group"]
    assert "完整 JSON schema" in details["next"]

    archive_details = await executor("tool_search", {"tool_name": "filter_archive_records"})
    _assert_tool_result_envelope(archive_details, "tool_search")
    assert archive_details["result_type"] == "tool_metadata"
    assert "parameters_schema" not in archive_details
    archive_parameter_by_name = {
        item["name"]: item for item in archive_details["parameters"]
    }
    time_ranges = archive_parameter_by_name["time_ranges"]
    assert time_ranges["type"] == "array"
    assert time_ranges["items"]["type"] == "object"
    time_range_fields = {
        item["name"]: item for item in time_ranges["items"]["fields"]
    }
    assert {"start", "end"}.issubset(time_range_fields)

    full_details = await executor(
        "tool_search",
        {"tool_name": "upload_file", "detail": "full", "intent": "发送文件"},
    )
    _assert_tool_result_envelope(full_details, "tool_search")
    assert full_details["result_type"] == "tool_metadata"
    assert "file_path" in full_details["parameters_schema"]["properties"]

    sent = await executor(
        "upload_file",
        {"target_type": "group", "target_id": 1, "file_path": "x.txt"},
    )
    assert sent["ok"] is True
    assert sent["status"] == "done"
    _assert_tool_result_envelope(sent, "upload_file")
    assert len(adapter.uploaded) == 1


@pytest.mark.asyncio
async def test_tool_search_reports_unknown_tool_candidates():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    executor = reg.get_executor(ToolContext())

    result = await executor("tool_search", {"tool_name": "send"})

    assert result["ok"] is False
    assert result["status"] == "not_found"
    _assert_tool_result_envelope(result, "tool_search")
    assert "send_private_messages" in result["candidates"]


# ============================================================
# Executor：参数校验与工具执行
# ============================================================


@pytest.mark.asyncio
async def test_executor_unknown_tool():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("does_not_exist", {})
    assert result["ok"] is False
    _assert_tool_result_envelope(result, "does_not_exist")
    assert "unknown" in result["error"].lower()


@pytest.mark.asyncio
async def test_executor_invalid_args():
    """参数校验失败应返回 ok=False，不抛异常。"""
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    # save_important_memory 要求 memory_text 非空
    result = await executor("save_important_memory", {})
    assert result["ok"] is False
    _assert_tool_result_envelope(result, "save_important_memory")
    assert "无效" in result["error"] or "memory_text" in result["error"]


@pytest.mark.asyncio
async def test_executor_finish_after_success_is_control_arg():
    class _Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int

    @tool(name="_finish_after_success_test", description="finish", args_model=_Args)
    async def _finish_after_success_test(args, ctx):
        return {"ok": True, "status": "done", "value": args.value}

    new_spec = next(
        s for s in get_default_specs() if s.name == "_finish_after_success_test"
    )
    try:
        reg = ToolRegistry([new_spec])
        executor = reg.get_executor(ToolContext())
        result = await executor(
            "_finish_after_success_test",
            {"value": 1, "finish_after_success": True},
        )
        assert result["ok"] is True
        assert result["value"] == 1
        assert result["turn_completion"]["allowed"] is True
        assert result["turn_completion"]["reason"] == "finish_after_success"
    finally:
        from tools.base import _DEFAULT_REGISTRY

        _DEFAULT_REGISTRY[:] = [
            s for s in _DEFAULT_REGISTRY if s.name != "_finish_after_success_test"
        ]


@pytest.mark.asyncio
async def test_executor_no_action_works():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor("no_action", {})
    assert result["ok"] is True
    assert result["status"] == "done"
    _assert_tool_result_envelope(result, "no_action")
    assert result["brief"] == "本轮不执行操作。"
    assert result.get("no_action") is True


@pytest.mark.asyncio
async def test_executor_exception_returns_error():
    """工具内部抛异常应被捕获并转成 ok=False。"""
    # 用一个故意抛异常的临时工具

    class _Args(BaseModel):
        pass

    @tool(name="_boom", description="boom", args_model=_Args)
    async def _boom(args, ctx):
        raise RuntimeError("boom!")

    # 拿到新注册的 spec
    new_spec = next(s for s in get_default_specs() if s.name == "_boom")
    try:
        reg = ToolRegistry([new_spec])
        ctx = ToolContext()
        executor = reg.get_executor(ctx)
        result = await executor("_boom", {})
        assert result["ok"] is False
        assert "boom" in result["error"]
    finally:
        # 清理：把 _boom 从全局列表移除避免污染其它测试
        from tools.base import _DEFAULT_REGISTRY
        _DEFAULT_REGISTRY[:] = [s for s in _DEFAULT_REGISTRY if s.name != "_boom"]


# ============================================================
# Feature 工具：未启用兜底
# ============================================================


# ============================================================
# Messaging 工具：即时发送
# ============================================================


# ============================================================
# message_builder 辅助
# ============================================================


# ============================================================
# upload_file 安全检查
# ============================================================


def _simple_pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            b"endobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream\nendobj\n"
        ),
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


@pytest.mark.asyncio
async def test_read_file_extracts_simple_pdf_text(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pdf = workspace / "simple.pdf"
    pdf.write_bytes(_simple_pdf_bytes("Hello PDF from workspace"))

    ctx = ToolContext(workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    result = await executor("read_file", {"path": "simple.pdf"})

    assert result["ok"] is True
    assert "Hello PDF from workspace" in result["content"]


@pytest.mark.asyncio
async def test_read_file_large_text_is_paginated(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "long.txt").write_text(
        "\n".join(f"line {i}" for i in range(300)),
        encoding="utf-8",
    )

    ctx = ToolContext(workspace_dir=workspace)
    executor = reg.get_executor(ctx)
    first = await executor("read_file", {"path": "long.txt", "max_lines": 20})

    assert first["ok"] is True
    assert first["offset"] == 0
    assert first["next_offset"] == 20
    assert "line 0" in first["content"]
    assert "line 25" not in first["content"]

    second = await executor(
        "read_file",
        {"path": "long.txt", "offset": first["next_offset"], "max_lines": 20},
    )
    assert second["offset"] == 20
    assert "line 20" in second["content"]


@pytest.mark.asyncio
async def test_read_file_writes_complete_artifact_for_large_page(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "long.txt").write_text(
        "\n".join(f"line {i} " + ("内容 " * 30) for i in range(300)),
        encoding="utf-8",
    )

    ctx = ToolContext(
        workspace_dir=workspace,
        tool_result_budgets={},
        tool_result_soft_limit_tokens=80,
        tool_result_hard_cap_tokens=500,
    )
    executor = reg.get_executor(ctx)
    result = await executor("read_file", {"path": "long.txt"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert result["offset"] == 0
    assert result["next_offset"] > 0
    assert result["total_lines"] == 300
    assert "preview" not in result
    assert "content" not in result
    assert result["artifact"]["type"] == "markdown"
    artifact = workspace / result["artifact"]["path"]
    assert artifact.exists()
    text = artifact.read_text(encoding="utf-8")
    assert "line 0" in text
    assert f"line {result['next_offset'] - 1}" in text
    assert "...[已按 token 预算截断]..." not in text


@pytest.mark.asyncio
async def test_list_files_returns_explicit_pages(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for i in range(60):
        (workspace / f"{i:02d}.txt").write_text("x", encoding="utf-8")

    executor = reg.get_executor(ToolContext(workspace_dir=workspace))
    result = await executor("list_files", {"path": ".", "pattern": "*.txt", "limit": 50})

    assert result["ok"] is True
    assert result["count"] == 60
    assert len(result["entries"]) == 50
    assert result["next_offset"] == 50
    assert "preview" not in result

    second = await executor(
        "list_files",
        {
            "path": ".",
            "pattern": "*.txt",
            "limit": 50,
            "offset": result["next_offset"],
        },
    )
    assert len(second["entries"]) == 10
    assert "next_offset" not in second


@pytest.mark.asyncio
async def test_executor_hard_cap_is_creation_time_stable():
    class _Args(BaseModel):
        pass

    @tool(name="_huge_result", description="huge", args_model=_Args)
    async def _huge_result(args, ctx):
        return {
            "ok": True,
            "status": "done",
            "payload": "x" * 10000,
            "legacy_flag": True,
            "count": 20,
            "data": {"rows": [{"id": idx, "text": "y" * 1000} for idx in range(20)]},
        }

    new_spec = next(s for s in get_default_specs() if s.name == "_huge_result")
    try:
        reg = ToolRegistry([new_spec])
        ctx = ToolContext(
            tool_result_budgets={},
            tool_result_soft_limit_tokens=100,
            tool_result_hard_cap_tokens=120,
        )
        executor = reg.get_executor(ctx)
        first = await executor("_huge_result", {})
        second = await executor("_huge_result", {})
    finally:
        from tools.base import _DEFAULT_REGISTRY
        _DEFAULT_REGISTRY[:] = [s for s in _DEFAULT_REGISTRY if s.name != "_huge_result"]

    assert first == second
    _assert_tool_result_envelope(first, "_huge_result")
    assert first["_condensed"]["reason"].startswith("工具结果超过中央 hard cap")
    assert {"payload", "legacy_flag", "count", "data"}.issubset(first)
    assert first["legacy_flag"] is True
    assert first["count"] == 20
    assert first["payload"]["_truncated"] is True
    assert first["payload"]["original_type"] == "string"
    assert first["payload"]["characters"] == 10000
    assert first["data"]["_truncated"] is True
    assert first["data"]["keys"] == ["rows"]


@pytest.mark.asyncio
async def test_run_python_single_long_line_keeps_stdout_field(tmp_path):
    cfg = _make_config()
    reg = build_default_registry(cfg)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = ToolContext(
        workspace_dir=workspace,
        tool_result_budgets={},
        tool_result_soft_limit_tokens=80,
        tool_result_hard_cap_tokens=800,
    )
    executor = reg.get_executor(ctx)

    result = await executor("run_python", {"code": "print('x' * 5000)"})

    assert result["ok"] is True
    assert result["status"] == "artifact"
    assert "stdout" in result
    assert "preview" not in result
    assert len(result["stdout"]) < 5000
    assert result["stdout_truncated"] is True
    artifact = workspace / result["artifact"]["path"]
    text = artifact.read_text(encoding="utf-8")
    assert "x" * 5000 in text
    assert "...[已按 token 预算截断]..." not in text


# ============================================================
# QQ group admin tools
# ============================================================


# ============================================================
# control: schedule_wakeup
# ============================================================


@pytest.mark.asyncio
async def test_schedule_wakeup_no_callback():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    ctx = ToolContext()
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup", {"delay_seconds": 10, "reminder": "test"}
    )
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "未注册唤醒回调" in result["brief"]


@pytest.mark.asyncio
async def test_schedule_wakeup_with_callback():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(wakeup_cb=cb)
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup", {"delay_seconds": 10, "reminder": "test"}
    )
    assert result["ok"] is True
    assert result["status"] == "done"
    assert result["data"]["delay_seconds"] == 10
    assert result["data"]["mode"] == "wakeup"
    assert received == [(10, "test", None, "wakeup", None)]


@pytest.mark.asyncio
async def test_schedule_wakeup_uses_default_reply_target():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(
        wakeup_cb=cb,
        extras={"default_reply_target": {"target_type": "private", "target_id": 123}},
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup", {"delay_seconds": 10, "reminder": "test"}
    )

    assert result["ok"] is True
    assert result["data"]["target"] == {"target_type": "private", "target_id": 123}
    delay, reminder, target, mode, message_text = received[0]
    assert delay == 10
    assert target == {"target_type": "private", "target_id": 123}
    assert mode == "wakeup"
    assert message_text is None
    assert "任务说明：test" in reminder
    assert "提醒目标：private:123" in reminder
    assert "不要把历史中已经完成" in reminder


@pytest.mark.asyncio
async def test_schedule_wakeup_includes_latest_user_message():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(
        wakeup_cb=cb,
        extras={
            "default_reply_target": {"target_type": "private", "target_id": 123},
            "latest_user_message": "30秒后单独发个消息，发个“到点了”就行",
        },
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup",
        {"delay_seconds": 30, "reminder": "30秒后发送“到点了”"},
    )

    assert result["ok"] is True
    reminder = received[0][1]
    assert "设置时用户原话：30秒后单独发个消息" in reminder
    assert "到点了" in reminder


@pytest.mark.asyncio
async def test_schedule_wakeup_send_message_mode_uses_default_target():
    cfg = _make_config()
    reg = build_default_registry(cfg)
    received: list[tuple[int, str, dict | None, str, str | None]] = []

    async def cb(delay, reminder, target=None, mode="wakeup", message_text=None):
        received.append((delay, reminder, target, mode, message_text))

    ctx = ToolContext(
        wakeup_cb=cb,
        extras={"default_reply_target": {"target_type": "private", "target_id": 123}},
    )
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup",
        {
            "delay_seconds": 30,
            "mode": "send_message",
            "message_text": "到点了",
        },
    )

    assert result["ok"] is True
    assert result["data"]["message_text"] == "到点了"
    delay, reminder, target, mode, message_text = received[0]
    assert delay == 30
    assert target == {"target_type": "private", "target_id": 123}
    assert mode == "send_message"
    assert message_text == "到点了"
    assert "消息内容：到点了" in reminder


@pytest.mark.asyncio
async def test_schedule_wakeup_send_message_requires_target():
    cfg = _make_config()
    reg = build_default_registry(cfg)

    async def cb(*_args):
        raise AssertionError("缺少目标时不应注册定时任务")

    ctx = ToolContext(wakeup_cb=cb)
    executor = reg.get_executor(ctx)
    result = await executor(
        "schedule_wakeup",
        {
            "delay_seconds": 30,
            "mode": "send_message",
            "message_text": "到点了",
        },
    )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "缺少发送目标" in result["brief"]
    assert "mode=send_message 需要" in result["error"]


# ============================================================
# memory 工具：依赖注入
# ============================================================


# ============================================================
# platform: list_contacts 兜底
# ============================================================
