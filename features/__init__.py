"""可选功能 service 模块。

每个子模块封装一个具体功能（vision/web_search/weather）。Runtime 按
features.*.enabled 实例化对应 service 并注入 ToolContext，
供 tools/feature_tools.py 中的工具调用。

未实装（P3 占位）：
    - asr：本地 Whisper / 远程 ASR API
    - tts：本地 VoxCPM2 / 远程 TTS API
    - embedding：本地 sentence-transformers / 远程 embedding API（仅 long_term_memory.mode=rag 才需要）
"""

from .vision import VisionService
from .weather import WeatherService
from .web_search import WebSearchService

__all__ = ["VisionService", "WeatherService", "WebSearchService"]
