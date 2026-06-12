"""NapCat（OneBot V11 实现）适配器。

模块组成：
    connection.py  —— WebSocket 连接（反向/正向 WS）
    events.py      —— NapCat JSON → 统一事件类型
    api_call.py    —— API 调用（echo 配对）
    process.py     —— 可选的 NapCat 进程托管
    adapter.py     —— IAdapter 实现（NapCatAdapter）

导入本模块时自动把 'napcat' 注册到全局适配器类型注册表，
后续可通过 build_adapter(name, type_name='napcat', ...) 构造实例。
"""

from __future__ import annotations

from adapters.registry import known_adapter_types, register_adapter_type

from .adapter import NapCatAdapter
from .api_call import NapCatApiCaller
from .connection import (
    ForwardWSConnection,
    NapCatConnection,
    ReverseWSConnection,
)
from .events import parse_napcat_event
from .process import NapCatProcessManager

# 注册类型（幂等）
if "napcat" not in known_adapter_types():
    register_adapter_type("napcat", NapCatAdapter.from_config)


__all__ = [
    "NapCatAdapter",
    "NapCatApiCaller",
    "NapCatConnection",
    "ReverseWSConnection",
    "ForwardWSConnection",
    "NapCatProcessManager",
    "parse_napcat_event",
]
