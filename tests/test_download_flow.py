"""模型安装/历史下载入口回归测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from plugins import DownloadSource, PluginMeta, PluginRecord, PluginStatus
from plugins.downloader import _download_one

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QApplication = QtWidgets.QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _SlowResponse:
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self, _chunk_size: int):
        yield b"partial"
        await asyncio.sleep(60)


class _SlowClient:
    def stream(self, *_args, **_kwargs):
        return _SlowResponse()


@pytest.mark.asyncio
async def test_download_cancel_removes_partial_file(tmp_path: Path):
    src = DownloadSource(
        url="https://example.com/model.bin",
        dest_filename="model.bin",
        size_bytes=1024,
    )
    dest = tmp_path / src.dest_filename

    task = asyncio.create_task(_download_one(_SlowClient(), src, dest, None))
    for _ in range(50):
        if dest.exists() and dest.stat().st_size > 0:
            break
        await asyncio.sleep(0.01)
    assert dest.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not dest.exists()


def test_models_detail_opens_install_guide_for_error_status(qapp):
    from ui.dashboard.models_page import _ModelDetail

    record = PluginRecord(
        meta=PluginMeta(
            name="whisper",
            display_name="Whisper",
            kind="asr",
            model_dir="whisper",
            auto_download=True,
            download_sources=[
                DownloadSource(
                    url="https://example.com/model.bin",
                    dest_filename="model.bin",
                )
            ],
        ),
        status=PluginStatus.ERROR,
        error="network failed",
    )

    detail = _ModelDetail()
    detail.set_record(record)

    assert detail._btn_download.isEnabled()
    assert detail._btn_download.text() == "安装指引"


def test_install_guide_matches_nested_model_folder_and_preserves_dest_prefix(
    tmp_path: Path,
    monkeypatch,
):
    from ui.widgets.model_install_guide import (
        find_matching_record_for_folder,
        install_model_folder,
    )

    monkeypatch.setenv("DEBATA_MODELS_DIR", str(tmp_path / "models"))
    source = tmp_path / "downloads" / "large-v3"
    source.mkdir(parents=True)
    (source / "model.bin").write_bytes(b"model")
    (source / "config.json").write_text("{}", encoding="utf-8")
    (source / "extra.json").write_text("{}", encoding="utf-8")

    record = PluginRecord(
        meta=PluginMeta(
            name="whisper",
            display_name="Whisper",
            kind="asr",
            model_dir="faster-whisper",
            download_sources=[
                DownloadSource(
                    url="https://example.com/model.bin",
                    dest_filename="large-v3/model.bin",
                ),
                DownloadSource(
                    url="https://example.com/config.json",
                    dest_filename="large-v3/config.json",
                ),
            ],
        ),
        status=PluginStatus.NOT_INSTALLED,
    )

    match = find_matching_record_for_folder(tmp_path / "downloads", [record])
    assert match == (record, source)

    install_model_folder(source, record)

    target = tmp_path / "models" / "faster-whisper" / "large-v3"
    assert (target / "model.bin").read_bytes() == b"model"
    assert (target / "config.json").read_text(encoding="utf-8") == "{}"
    assert (target / "extra.json").read_text(encoding="utf-8") == "{}"


def test_install_guide_markdown_contains_manual_steps(tmp_path: Path, monkeypatch):
    from ui.widgets.model_install_guide import build_model_install_markdown

    monkeypatch.setenv("DEBATA_MODELS_DIR", str(tmp_path / "models"))
    record = PluginRecord(
        meta=PluginMeta(
            name="demo",
            display_name="Demo Model",
            kind="tts",
            model_dir="demo",
            download_url="https://example.com/demo",
            download_sources=[
                DownloadSource(
                    url="https://example.com/model.bin",
                    dest_filename="model.bin",
                )
            ],
        ),
        status=PluginStatus.NOT_INSTALLED,
    )

    text = build_model_install_markdown(record)

    assert "Demo Model 安装指引" in text
    assert "https://example.com/demo" in text
    assert str(tmp_path / "models" / "demo" / "model.bin") in text
    assert "拖进模型管理页" in text


def test_voxcpm2_install_guide_mentions_project_ffmpeg_shared_dir():
    from ui.widgets.model_install_guide import build_model_install_markdown

    record = PluginRecord(
        meta=PluginMeta(
            name="voxcpm2",
            display_name="VoxCPM2",
            kind="tts",
            model_dir="VoxCPM2",
            download_url="https://huggingface.co/OpenBMB/VoxCPM2",
        ),
        status=PluginStatus.NOT_INSTALLED,
    )

    text = build_model_install_markdown(record)

    assert "full-shared" in text
    assert "data" in text
    assert "tools" in text
    assert "ffmpeg" in text
    assert "https://ffmpeg.org/download.html" in text
    assert "https://www.gyan.dev/ffmpeg/builds/" in text
    assert "avutil-*.dll" in text


def test_missing_python_deps_uses_import_name_mapping(monkeypatch):
    from ui.widgets import model_install_guide

    record = PluginRecord(
        meta=PluginMeta(
            name="demo",
            display_name="Demo",
            kind="asr",
            model_dir="demo",
            python_deps=[
                "sentence-transformers>=2.7.0",
                "soundfile>=0.12.1",
            ],
        ),
        status=PluginStatus.NOT_INSTALLED,
    )

    def fake_find_spec(name: str):
        return object() if name == "soundfile" else None

    monkeypatch.setattr(model_install_guide.importlib.util, "find_spec", fake_find_spec)

    assert model_install_guide.missing_python_deps(record) == [
        "sentence-transformers>=2.7.0",
    ]


def test_pip_install_args_use_selected_index():
    from ui.widgets import model_install_guide

    deps = ["voxcpm>=2.0.0"]

    assert model_install_guide._pip_install_args(deps, "tsinghua") == [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-i",
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "voxcpm>=2.0.0",
    ]
    assert "-i" not in model_install_guide._pip_install_args(deps, "official")


def test_dependency_install_env_removes_proxy(monkeypatch):
    from ui.widgets import model_install_guide

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example")

    env = model_install_guide._dependency_install_env()

    assert not env.contains("HTTPS_PROXY")
    assert not env.contains("HTTP_PROXY")
    assert env.value("PIP_NO_INPUT") == "1"


def test_install_guide_shows_dependency_progress_panel(qapp, monkeypatch):
    from ui.widgets import model_install_guide
    from ui.widgets.model_install_guide import ModelInstallGuideDialog

    record = PluginRecord(
        meta=PluginMeta(
            name="demo",
            display_name="Demo",
            kind="tts",
            model_dir="demo",
            python_deps=["definitely-missing-package>=1.0"],
        ),
        status=PluginStatus.NOT_INSTALLED,
    )
    monkeypatch.setattr(
        model_install_guide,
        "missing_python_deps",
        lambda _record: ["definitely-missing-package>=1.0"],
    )

    dlg = ModelInstallGuideDialog(record)
    try:
        assert not dlg._dep_panel.isHidden()
        assert dlg._pip_source.count() == 3
        assert dlg._dep_install_btn.text() == "后台安装依赖"
    finally:
        dlg.close()
        dlg.deleteLater()


def test_install_guide_dialog_is_independent_non_modal(qapp):
    from PySide6.QtCore import Qt
    from ui.widgets.model_install_guide import ModelInstallGuideDialog

    parent = QtWidgets.QWidget()
    record = PluginRecord(
        meta=PluginMeta(
            name="demo",
            display_name="Demo",
            kind="tts",
            model_dir="demo",
        ),
        status=PluginStatus.NOT_INSTALLED,
    )

    dlg = ModelInstallGuideDialog(record, parent)
    try:
        assert dlg.parent() is None
        assert dlg.windowModality() == Qt.WindowModality.NonModal
        assert dlg.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    finally:
        dlg.close()
        dlg.deleteLater()
        parent.deleteLater()


def test_plugin_install_status_requires_download_sources(tmp_path: Path):
    from plugins.base import PluginManager

    manager = PluginManager(tmp_path / "plugins")
    meta = PluginMeta(
        name="demo",
        display_name="Demo",
        kind="asr",
        model_dir=str(tmp_path / "model"),
        auto_download=True,
        download_sources=[
            DownloadSource(
                url="https://example.com/model.bin",
                dest_filename="large-v3/model.bin",
            ),
            DownloadSource(
                url="https://example.com/config.json",
                dest_filename="large-v3/config.json",
            ),
        ],
    )
    (tmp_path / "model" / "large-v3").mkdir(parents=True)
    (tmp_path / "model" / "large-v3" / "model.bin").write_bytes(b"x")

    assert manager._detect_install_status(meta) == PluginStatus.NOT_INSTALLED

    (tmp_path / "model" / "large-v3" / "config.json").write_text("{}", encoding="utf-8")
    assert manager._detect_install_status(meta) == PluginStatus.INSTALLED


def test_downloader_uses_only_configured_proxy(monkeypatch):
    from plugins import downloader

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)

    assert downloader._build_proxy_url() is None

    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example")

    assert downloader._build_proxy_url() == "https://proxy.example"
