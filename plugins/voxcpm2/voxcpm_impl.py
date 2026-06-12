"""VoxCPM2 本地 TTS 实现。warmup 后台加载模型，音频输出到 workspace/.run/。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import warnings
from contextlib import contextmanager
from pathlib import Path

from features.tts import ITTSService, TTSError

logger = logging.getLogger(__name__)

_WEIGHT_NORM_WARNING = r".*torch\.nn\.utils\.weight_norm.*deprecated.*"
_DLL_DIRECTORY_HANDLES: list[object] = []
_REGISTERED_FFMPEG_DIRS: set[str] = set()


class VoxCPM2Service(ITTSService):
    """基于清华开源 VoxCPM2 的语音合成服务。"""

    def __init__(
        self,
        model_dir: str = "data/models/VoxCPM2/",
        reference_audio: str = "",
        default_prompt: str = "",
        device: str = "auto",
        load_denoiser: bool = False,
        cfg_value: float = 2.0,
        inference_timesteps: int = 10,
    ) -> None:
        self._model_dir = model_dir
        self._reference_audio = reference_audio
        self._default_prompt = default_prompt
        self._device = device
        self._load_denoiser = load_denoiser
        self._cfg_value = cfg_value
        self._inference_timesteps = inference_timesteps
        self._model = None
        self._ready = asyncio.Event()
        self._loading_lock = asyncio.Lock()
        self._closed = False

    async def warmup(self) -> None:
        """后台加载 VoxCPM2 模型。幂等。"""
        if self._ready.is_set():
            return

        async with self._loading_lock:
            if self._ready.is_set():
                return
            self._closed = False
            logger.info(
                "开始后台加载 VoxCPM2 模型: dir=%s device=%s denoiser=%s cfg=%.1f steps=%d",
                self._model_dir,
                self._device,
                self._load_denoiser,
                self._cfg_value,
                self._inference_timesteps,
            )

            t0 = asyncio.get_running_loop().time()
            device, model = await asyncio.to_thread(self._load_model_with_device_sync)
            if self._closed:
                return
            self._model = model
            self._ready.set()
            elapsed = asyncio.get_running_loop().time() - t0
            logger.info("TTS 预热完成（耗时 %.1fs，device=%s）", elapsed, device)

    def _select_device(self) -> str:
        device = (self._device or "auto").strip().lower()
        if device == "auto":
            try:
                import torch
                return "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                return "cpu"
        if device.startswith("cuda"):
            try:
                import torch
                if torch.cuda.is_available():
                    return device
            except ImportError:
                pass
            logger.warning("配置了 VoxCPM2 device=%s，但 CUDA 不可用，已回退到 CPU", device)
            return "cpu"
        return device

    def _load_model_with_device_sync(self) -> tuple[str, object]:
        """Resolve torch/CUDA device and load the model in the worker thread."""
        device = self._select_device()
        return device, self._load_model_sync(device)

    def _load_model_sync(self, device: str):
        if self._load_denoiser:
            _ensure_ffmpeg_for_denoiser()
        else:
            _prepare_project_ffmpeg()
        from voxcpm import VoxCPM

        model_dir = str(Path(self._model_dir))
        if not os.path.isdir(model_dir):
            raise TTSError(f"VoxCPM2 模型目录不存在或不可用：{self._model_dir}")
        with _suppress_voxcpm_warnings():
            model = VoxCPM.from_pretrained(
                model_dir,
                device=device,
                load_denoiser=self._load_denoiser,
                local_files_only=True,
                optimize=device.startswith("cuda"),
            )
        return model

    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        prompt: str = "",
    ) -> Path:
        """合成语音，返回 wav 文件路径。workspace/.run/voice_{ts}.wav"""
        if not self._ready.is_set():
            await self.warmup()
        if self._model is None:
            raise TTSError("VoxCPM2 模型加载失败")

        if prompt is None:
            prompt = ""
        if not prompt:
            prompt = self._default_prompt or ""
        ref = reference_audio or self._reference_audio

        # 输出到 workspace/.run/
        workspace_run = Path("data/workspace/.run")
        workspace_run.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        out_path = workspace_run / f"voice_{ts}.wav"

        def _run():
            kwargs = self._build_generate_kwargs(text, ref, prompt)
            with _suppress_voxcpm_warnings():
                wav = self._model.generate(**kwargs)
            sample_rate = getattr(
                getattr(self._model, "tts_model", None),
                "sample_rate",
                48000,
            )
            if isinstance(wav, tuple) and len(wav) >= 2:
                wav, sample_rate = wav[0], wav[1]
            import soundfile as sf
            sf.write(str(out_path), wav, int(sample_rate))

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            raise TTSError(f"语音合成失败：{e}") from e

        logger.info(f"TTS 合成完成: {out_path} ({text[:30]}...)")
        return out_path

    def _build_generate_kwargs(
        self,
        text: str,
        reference_audio: str | Path | None,
        prompt: str,
    ) -> dict[str, object]:
        """按已安装 VoxCPM 版本构造 generate 参数。

        新版 VoxCPM2 用 `reference_wav_path`，Voice Design 通过把描述放进
        文本开头的括号实现。旧版封装若仍提供 `reference_audio`/`prompt`，则
        保留兼容。
        """
        prompt = prompt.strip() or self._default_prompt.strip()
        ref = str(reference_audio).strip() if reference_audio else ""
        text_for_voice_design = _prepend_voice_design_prompt(text, prompt)

        try:
            params = inspect.signature(self._model.generate).parameters
        except (TypeError, ValueError):
            params = {}

        accepts_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

        if "reference_wav_path" in params or accepts_kwargs:
            kwargs: dict[str, object] = {
                "text": text_for_voice_design,
                "cfg_value": self._cfg_value,
                "inference_timesteps": self._inference_timesteps,
                "normalize": False,
                "denoise": self._load_denoiser,
            }
            if ref:
                kwargs["reference_wav_path"] = ref
            return kwargs

        if "reference_audio" in params:
            kwargs = {"text": text}
            if ref:
                kwargs["reference_audio"] = ref
            if "prompt" in params and prompt:
                kwargs["prompt"] = prompt
            elif prompt:
                kwargs["text"] = text_for_voice_design
            return kwargs

        return {"text": text_for_voice_design}

    async def aclose(self) -> None:
        self._closed = True
        self._model = None
        self._ready.clear()


def _prepend_voice_design_prompt(text: str, prompt: str) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return text
    stripped = text.lstrip()
    if stripped.startswith("(") or stripped.startswith("（"):
        return text
    return f"({prompt}){text}"


@contextmanager
def _suppress_voxcpm_warnings():
    """压掉 VoxCPM/torch 第三方加载时的重复 FutureWarning。"""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=_WEIGHT_NORM_WARNING,
            category=FutureWarning,
        )
        yield


def _ensure_ffmpeg_for_denoiser() -> Path:
    """启用 VoxCPM2 官方 ZipEnhancer 降噪前确认 FFmpeg shared DLL 可用。"""
    found = _prepare_project_ffmpeg()
    if found is not None:
        return found
    found = _prepare_path_ffmpeg()
    if found is not None:
        return found

    expected = Path.cwd() / "data" / "tools" / "ffmpeg" / "bin"
    raise TTSError(
        "VoxCPM2 降噪需要 Windows full-shared 版 FFmpeg DLL，"
        "否则官方 ZipEnhancer 的 torchaudio 响度归一化会加载 torchcodec 失败。\n\n"
        f"请把 full-shared 版 FFmpeg 的 bin 目录放到：{expected}\n"
        "确认其中包含 ffmpeg.exe、avutil-*.dll、avcodec-*.dll、avformat-*.dll；"
        "或设置环境变量 DEBATA_FFMPEG_DIR 指向该 bin 目录。"
    )


def _prepare_project_ffmpeg() -> Path | None:
    """优先启用项目内 FFmpeg shared 目录，供 torchcodec/音频库查找 DLL。

    约定放置方式：
    - data/tools/ffmpeg/bin/ffmpeg.exe
    - data/tools/ffmpeg/bin/avutil-*.dll 等 shared DLL
    """
    for bin_dir in _ffmpeg_bin_candidates():
        if not bin_dir.is_dir():
            continue
        if not _is_ffmpeg_shared_dir(bin_dir):
            continue
        resolved = _register_ffmpeg_dir(bin_dir)
        logger.info("已启用项目内 FFmpeg shared 目录：%s", resolved)
        return bin_dir
    return None


def _prepare_path_ffmpeg() -> Path | None:
    """识别并注册 PATH 中已有的 FFmpeg shared 目录。"""
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        bin_dir = Path(raw)
        if not bin_dir.is_dir():
            continue
        if not _is_ffmpeg_shared_dir(bin_dir):
            continue
        _register_ffmpeg_dir(bin_dir)
        logger.info("已启用 PATH 中的 FFmpeg shared 目录：%s", bin_dir.resolve())
        return bin_dir
    return None


def _register_ffmpeg_dir(bin_dir: Path) -> str:
    resolved = str(bin_dir.resolve())
    if resolved in _REGISTERED_FFMPEG_DIRS:
        return resolved
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if resolved not in path_parts:
        os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(resolved))
        except OSError as exc:
            logger.debug("FFmpeg DLL 目录注册失败：%s", exc)
    _REGISTERED_FFMPEG_DIRS.add(resolved)
    return resolved


def _is_ffmpeg_shared_dir(bin_dir: Path) -> bool:
    return (
        (bin_dir / "ffmpeg.exe").is_file()
        and any(bin_dir.glob("avutil*.dll"))
        and any(bin_dir.glob("avcodec*.dll"))
        and any(bin_dir.glob("avformat*.dll"))
    )


def _ffmpeg_bin_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.getenv("DEBATA_FFMPEG_DIR", "").strip()
    if configured:
        candidates.append(Path(configured))
    root = Path.cwd()
    candidates.extend(
        [
            root / "data" / "tools" / "ffmpeg" / "bin",
            root / "data" / "ffmpeg" / "bin",
            root / "tools" / "ffmpeg" / "bin",
        ]
    )
    normalized: list[Path] = []
    for candidate in candidates:
        normalized.append(candidate / "bin" if (candidate / "bin").is_dir() else candidate)
    return normalized
