"""main.py CLI 分支回归测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import main as app_main


@pytest.mark.asyncio
async def test_test_adapter_uses_config_file_and_actual_adapter_name(
    monkeypatch, tmp_path: Path, capsys
):
    seen: dict[str, object] = {}

    class FakeAdapter:
        is_connected = True

    class FakeRuntime:
        def __init__(self, project_root: Path, config_file: Path | None = None) -> None:
            seen["project_root"] = project_root
            seen["config_file"] = config_file
            self.adapter = FakeAdapter()
            self.config = SimpleNamespace(
                adapters={
                    "custom_adapter": SimpleNamespace(
                        mode="client",
                        host="10.0.0.2",
                        port=4567,
                        path="/onebot",
                        access_token_id="custom_token",
                    )
                }
            )

        async def start(self) -> None:
            seen["started"] = True

        async def shutdown(self) -> None:
            seen["shutdown"] = True

    async def fake_sleep(_seconds: float) -> None:
        return None

    import core

    monkeypatch.setattr(core, "Runtime", FakeRuntime)
    monkeypatch.setattr(app_main.asyncio, "sleep", fake_sleep)

    config_file = tmp_path / "custom.yaml"
    await app_main._test_adapter(tmp_path, config_file=config_file)

    out = capsys.readouterr().out
    assert seen["project_root"] == tmp_path
    assert seen["config_file"] == config_file
    assert seen["started"] is True
    assert seen["shutdown"] is True
    assert "adapter  = custom_adapter" in out
    assert "endpoint = ws://10.0.0.2:4567/onebot" in out
