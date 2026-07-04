"""main.py CLI 分支回归测试。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as app_main
from app_config.loader import load_config
from app_config.secrets import SecretsManager

_QT_APP = None


class _FakeHandle:
    def __init__(self, callback):
        self._callback = callback
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _run(self) -> None:
        self._callback()


def _make_patched_simple_timer():
    pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
    _qt_app()

    class FakeQasync:
        class _SimpleTimer:
            pass

    app_main._install_qasync_timer_multiplexer(FakeQasync)
    return FakeQasync._SimpleTimer()


def _qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    qt_widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    global _QT_APP
    _QT_APP = qt_widgets.QApplication.instance() or _QT_APP or qt_widgets.QApplication([])
    return _QT_APP


def _wait_for_qt(condition, timeout_ms: int = 500) -> None:
    qt_core = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
    app = _qt_app()
    deadline = time.monotonic() + timeout_ms / 1000

    while not condition() and time.monotonic() < deadline:
        loop = qt_core.QEventLoop()
        qt_core.QTimer.singleShot(5, loop.quit)
        loop.exec()
        app.processEvents()

    assert condition()


def test_gui_instance_lock_blocks_second_instance(tmp_path):
    pytest.importorskip("PySide6.QtCore", exc_type=ImportError)

    lock_path = tmp_path / "debata.gui.lock"
    first = app_main._acquire_gui_instance_lock(lock_path)
    try:
        assert first is not None
        second = app_main._acquire_gui_instance_lock(lock_path)
        assert second is None
    finally:
        if first is not None:
            first.unlock()

    third = app_main._acquire_gui_instance_lock(lock_path)
    try:
        assert third is not None
    finally:
        if third is not None:
            third.unlock()


def test_install_qasync_timer_multiplexer_is_idempotent():
    pytest.importorskip("PySide6.QtCore", exc_type=ImportError)

    class FakeQasync:
        class _SimpleTimer:
            pass

    app_main._install_qasync_timer_multiplexer(FakeQasync)
    first = FakeQasync._SimpleTimer
    app_main._install_qasync_timer_multiplexer(FakeQasync)

    assert FakeQasync._SimpleTimer is first
    assert FakeQasync._debata_timer_multiplexer_installed is True


def test_qasync_timer_multiplexer_runs_zero_and_short_delay_callbacks():
    timer = _make_patched_simple_timer()
    calls: list[str] = []

    try:
        assert timer.add_callback(_FakeHandle(lambda: calls.append("zero")), delay=0) is not None
        timer.add_callback(_FakeHandle(lambda: calls.append("short")), delay=0.01)

        _wait_for_qt(lambda: calls == ["zero", "short"])
    finally:
        timer.stop()


def test_qasync_timer_multiplexer_skips_cancelled_callbacks():
    timer = _make_patched_simple_timer()
    calls: list[str] = []
    cancelled = _FakeHandle(lambda: calls.append("cancelled"))

    try:
        timer.add_callback(cancelled, delay=0)
        cancelled.cancel()
        timer.add_callback(_FakeHandle(lambda: calls.append("active")), delay=0)

        _wait_for_qt(lambda: calls == ["active"])
    finally:
        timer.stop()


def test_qasync_timer_multiplexer_uses_single_qtimer_for_many_delays():
    qt_core = pytest.importorskip("PySide6.QtCore", exc_type=ImportError)
    timer = _make_patched_simple_timer()

    try:
        for _ in range(500):
            timer.add_callback(_FakeHandle(lambda: None), delay=60)

        assert len(timer.findChildren(qt_core.QTimer)) == 1
    finally:
        timer.stop()


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


def test_cli_wizard_writes_minimal_full_config(
    monkeypatch,
    tmp_paths,
    fake_keyring,
):
    tmp_paths.PROVIDER_PRESETS_DIR = Path(__file__).resolve().parent.parent / "providers" / "presets"
    answers = iter(
        [
            "",  # provider id
            "",  # provider preset
            "",  # chat model
            "",  # key id
            "",  # proactive enabled
            "",  # proactive model
            "",  # summary enabled
            "",  # summary model
            "",  # persona_gen disabled
            "",  # web_search enabled
            "",  # vision disabled
            "",  # weather disabled
            "",  # tts disabled
            "",  # rag disabled
            "",  # napcat mode
            "",  # napcat host
            "",  # napcat port
            "",  # napcat path
            "",  # napcat token id
            "",  # persona
            "",  # admin qq
            "",  # confirm
        ]
    )
    secrets = iter(["sk-main", ""])

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(app_main, "getpass", lambda _prompt="": next(secrets))

    app_main._run_cli_wizard(tmp_paths)

    cfg = load_config(tmp_paths, set_global=False)
    sm = SecretsManager(tmp_paths)
    sm.initialize()

    assert cfg.agents.chat.provider == "main_provider"
    assert cfg.agents.chat.model == "deepseek-v4-flash"
    assert cfg.agents.proactive is not None
    assert cfg.agents.summary is not None
    assert cfg.features.web_search.enabled is True
    assert cfg.features.long_term_memory.mode == "file"
    assert cfg.adapters["default"].mode == "client"
    assert sm.get("main_provider_key") == "sk-main"
