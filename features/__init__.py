"""可选功能 service 模块。

vision / web_search / weather 由 Runtime 按 features.*.enabled 实例化并注入 ToolContext。
asr / tts / embedding 走插件系统（plugins/），由 PluginManager 管理启停 + 预热。
"""

from .vision import VisionService
from .weather import WeatherService
from .web_search import WebSearchService

__all__ = ["VisionService", "WeatherService", "WebSearchService"]
