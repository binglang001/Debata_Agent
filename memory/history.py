"""对话历史管理 —— 每个人格独立的会话上下文。

历史记录格式（OpenAI 兼容）：
    {"role": "user|assistant|system|tool", "content": "...", "tool_calls": [...], ...}

存储为 JSONL（每行一条 dict），便于增量追加。

订阅机制（为 P2 的 RAG 预留）：
    history.on_append(callback)  # callback: async (records: list[dict]) -> None
    每次有新记录写入时，所有订阅者都会被并发通知（不阻塞主流程）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from .store import JsonlStore

logger = logging.getLogger(__name__)


AppendCallback = Callable[[list[dict]], Awaitable[None]]


class HistoryManager:
    """对话历史管理器。一个 persona 对应一个实例。"""

    def __init__(self, history_path: Path) -> None:
        self._store = JsonlStore(history_path)
        self._append_callbacks: list[AppendCallback] = []
        # 保留 notify task 引用避免 GC（订阅者执行完后自动 discard）
        self._notify_tasks: set[asyncio.Task] = set()

    def on_append(self, callback: AppendCallback) -> None:
        """订阅新记录写入事件。用于 RAG 等下游模块异步消费。"""
        self._append_callbacks.append(callback)

    async def _notify(self, records: list[dict]) -> None:
        """并发通知所有订阅者；不阻塞主流程，错误隔离。"""
        if not self._append_callbacks:
            return
        for cb in self._append_callbacks:
            try:
                task = asyncio.create_task(cb(records))
            except Exception as e:
                logger.exception(f"创建 history.on_append 通知任务失败: {e}")
                continue
            # 保留引用避免 GC；完成后自动从集合移除
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

    async def load(self, force_reload: bool = False) -> list[dict]:
        """加载所有历史。"""
        return await self._store.load(force_reload=force_reload)

    async def records(self) -> list[dict]:
        """获取当前所有历史记录（缓存命中时不访问磁盘）。

        语义上等价于 load() —— load() 名字暗示"从磁盘加载"，
        records() 更适合"取当前完整记录"这种调用场景，
        在 message_pipeline 拼装 messages 时用 records() 表达意图更清晰。
        """
        return await self._store.load()

    async def add_user_message(self, content: str) -> None:
        record = {"role": "user", "content": content}
        await self._store.append(record)
        await self._notify([record])

    async def add_assistant_message(
        self,
        content: str,
        tool_calls: list[dict] | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        record: dict = {"role": "assistant", "content": content or ""}
        if tool_calls:
            record["tool_calls"] = tool_calls
        if reasoning_content:
            record["reasoning_content"] = reasoning_content
        await self._store.append(record)
        await self._notify([record])

    async def add_tool_result(self, tool_call_id: str, content: str) -> None:
        record = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        await self._store.append(record)
        await self._notify([record])

    async def add_system_note(self, content: str) -> None:
        """记录一条系统注解（如发送结果、撤回事件等）。"""
        if not content:
            return
        record = {"role": "system", "content": content}
        await self._store.append(record)
        await self._notify([record])

    async def add_records(self, records: list[dict]) -> None:
        """批量追加（如 agent 一轮工具循环后的所有 records）。"""
        if not records:
            return
        await self._store.append_many(records)
        await self._notify(records)

    async def length(self) -> int:
        return await self._store.length()

    async def get_slice(self, start: int = 0, end: int | None = None) -> list[dict]:
        return await self._store.get_slice(start, end)

    async def truncate_head(self, cut_point: int) -> int:
        """删除最早的 cut_point 条记录。返回剩余长度。"""
        return await self._store.truncate_head(cut_point)

    async def clear(self) -> None:
        """清空所有历史（慎用）。"""
        await self._store.clear()
