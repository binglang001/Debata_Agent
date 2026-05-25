"""TTS（文字转语音）feature 模块。

接口在此定义，具体实现由插件提供：
    - features/tts/local_voxcpm.py（Phase 3 由 DeepSeek 完成）
        基于清华开源 VoxCPM2，模型存 F:/.models/VoxCPM2/

启用时 tools/feature_tools.py 会注册 send_voice_message 工具，
让 LLM 可主动调用合成并发送到 NapCat。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class TTSError(Exception):
    """TTS 调用相关异常。"""


class ITTSService(ABC):
    """文字转语音的抽象接口。

    实装约定：
        - 实例化时**不**加载模型（lazy load 到首次 synthesize）
        - synthesize 返回生成的音频文件路径（wav 优先，便于 NapCat 转码）
        - reference_audio 是 voice cloning 用的参考音频（VoxCPM2 必需）
        - prompt 可选，部分模型支持「这句话用悲伤的语气说」之类的引导
        - aclose 释放底层资源
    """

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        reference_audio: str | Path | None = None,
        prompt: str = "",
    ) -> Path:
        """合成语音，返回音频文件路径。失败 raise TTSError。"""

    @abstractmethod
    async def aclose(self) -> None:
        """释放底层资源。可幂等。"""


__all__ = ["TTSError", "ITTSService"]
