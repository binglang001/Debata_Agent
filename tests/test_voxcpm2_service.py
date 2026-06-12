"""VoxCPM2 本地 TTS 参数构造回归测试。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

from features.tts import TTSError
from plugins.voxcpm2 import voxcpm_impl
from plugins.voxcpm2.voxcpm_impl import VoxCPM2Service


class _NewVoxCPM:
    def generate(self, text, reference_wav_path=None):
        return None


class _LegacyVoxCPM:
    def generate(self, text, reference_audio=None, prompt=""):
        return None


class _KwargsVoxCPM:
    def generate(self, *args, **kwargs):
        return None


def test_voxcpm2_prompt_only_uses_voice_design_text():
    svc = VoxCPM2Service(default_prompt="年轻女性，温柔语气")
    svc._model = _NewVoxCPM()

    kwargs = svc._build_generate_kwargs("你好呀", None, "")

    assert kwargs == {
        "text": "(年轻女性，温柔语气)你好呀",
        "cfg_value": 2.0,
        "inference_timesteps": 10,
        "normalize": False,
        "denoise": False,
    }


def test_voxcpm2_reference_audio_is_optional_but_passed_when_present():
    svc = VoxCPM2Service()
    svc._model = _NewVoxCPM()

    kwargs = svc._build_generate_kwargs("你好呀", "ref.wav", "冷静")

    assert kwargs == {
        "text": "(冷静)你好呀",
        "cfg_value": 2.0,
        "inference_timesteps": 10,
        "normalize": False,
        "denoise": False,
        "reference_wav_path": "ref.wav",
    }


def test_voxcpm2_var_kwargs_generate_uses_official_voxcpm2_params():
    svc = VoxCPM2Service(cfg_value=2.5, inference_timesteps=12)
    svc._model = _KwargsVoxCPM()

    kwargs = svc._build_generate_kwargs("你好呀", "ref.wav", "冷静")

    assert kwargs == {
        "text": "(冷静)你好呀",
        "cfg_value": 2.5,
        "inference_timesteps": 12,
        "normalize": False,
        "denoise": False,
        "reference_wav_path": "ref.wav",
    }


def test_voxcpm2_legacy_generate_signature_keeps_old_prompt_field():
    svc = VoxCPM2Service()
    svc._model = _LegacyVoxCPM()

    kwargs = svc._build_generate_kwargs("你好呀", "ref.wav", "冷静")

    assert kwargs == {
        "text": "你好呀",
        "reference_audio": "ref.wav",
        "prompt": "冷静",
    }


@pytest.mark.asyncio
async def test_voxcpm2_warmup_is_concurrency_safe(monkeypatch):
    svc = VoxCPM2Service(device="cpu")
    calls = 0
    model = object()

    def fake_load(device):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        assert device == "cpu"
        return model

    monkeypatch.setattr(svc, "_load_model_sync", fake_load)

    await asyncio.gather(svc.warmup(), svc.warmup(), svc.warmup())

    assert calls == 1
    assert svc._model is model
    assert svc._ready.is_set()


def test_voxcpm2_cuda_device_falls_back_when_unavailable(monkeypatch):
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    svc = VoxCPM2Service(device="cuda")

    assert svc._select_device() == "cpu"


def test_voxcpm2_project_ffmpeg_shared_dir_added_to_path(tmp_path, monkeypatch):
    root = tmp_path / "project"
    bin_dir = root / "data" / "tools" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "ffmpeg.exe").write_bytes(b"")
    (bin_dir / "avutil-59.dll").write_bytes(b"")
    (bin_dir / "avcodec-61.dll").write_bytes(b"")
    (bin_dir / "avformat-61.dll").write_bytes(b"")

    monkeypatch.chdir(root)
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("DEBATA_FFMPEG_DIR", raising=False)
    voxcpm_impl._REGISTERED_FFMPEG_DIRS.clear()

    found = voxcpm_impl._prepare_project_ffmpeg()

    assert found == bin_dir
    assert str(bin_dir.resolve()) in os.environ["PATH"]


def test_voxcpm2_denoiser_requires_shared_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("DEBATA_FFMPEG_DIR", raising=False)
    voxcpm_impl._REGISTERED_FFMPEG_DIRS.clear()

    with pytest.raises(TTSError, match="full-shared"):
        voxcpm_impl._ensure_ffmpeg_for_denoiser()
