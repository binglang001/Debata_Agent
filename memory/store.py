"""异步 JSON / JSONL 存储基础设施。

设计：
    - JsonStore：整体 JSON 文件（重要记忆等小数据）
    - JsonlStore：JSONL 格式（对话历史，append 友好）
    - 所有 IO 通过 aiofiles 异步，避免阻塞事件循环
    - 写入用 .tmp + replace 实现原子替换
    - 内置缓存：load 一次后保留内存副本，append 同步更新缓存
    - 异步锁保护并发写入
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiofiles
import orjson

logger = logging.getLogger(__name__)


class StoreError(Exception):
    """存储层异常。"""


@runtime_checkable
class JsonStoreLike(Protocol):
    """整体 JSON 仓储协议。"""

    async def read(self, default: Any = None) -> Any: ...

    async def write(self, data: Any) -> None: ...


@runtime_checkable
class JsonlStoreLike(Protocol):
    """JSONL/历史流仓储协议。"""

    async def load(self, force_reload: bool = False) -> list[dict]: ...

    async def append(self, record: dict) -> None: ...

    async def append_many(self, records: list[dict]) -> None: ...

    async def length(self) -> int: ...

    async def get_slice(self, start: int = 0, end: int | None = None) -> list[dict]: ...

    async def truncate_head(self, cut_point: int) -> int: ...

    async def replace_all(self, records: list[dict]) -> None: ...

    async def clear(self) -> None: ...


@runtime_checkable
class ArchiveStoreLike(Protocol):
    """归档仓储协议。"""

    async def load(self, force_reload: bool = False) -> list[dict]: ...

    async def append_many(self, records: list[dict[str, Any]]) -> None: ...

    async def records(self) -> list[dict]: ...

    async def search(
        self,
        *,
        conversation_id: str | None = None,
        keyword: str | None = None,
        time_range: str | None = None,
        limit: int = 20,
    ) -> list[dict]: ...

    async def filter_records(self, query: Any) -> dict[str, Any]: ...

    async def get_by_ids(self, archive_ids: list[str]) -> list[dict]: ...

    async def context_around(
        self,
        archive_id: str,
        before: int,
        after: int,
    ) -> list[dict]: ...

    async def rag_records(self) -> list[dict]: ...

    async def media_records(self, archive_id: str | None = None) -> list[dict[str, Any]]: ...


@runtime_checkable
class RollingSummaryStoreLike(Protocol):
    """滚动摘要仓储协议。"""

    async def load(self) -> dict[str, Any]: ...

    def text(self) -> str: ...

    def active_start_index(self) -> int: ...

    async def update(
        self,
        summary_text: str,
        *,
        archived_until: Any = None,
        updated_at: str = "",
        active_start_index: int | None = None,
    ) -> None: ...


@runtime_checkable
class EventAppenderLike(Protocol):
    """事件追加协议，供历史镜像等轻量调用方使用。"""

    async def append_event(
        self,
        *,
        event_type: str,
        payload: Any,
        event_uuid: str | None = None,
        conversation_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | int | None = None,
        source: str | None = None,
        external_id: str | None = None,
        tool_call_id: str | None = None,
        parent_event_id: int | None = None,
        idempotency_key: str | None = None,
        timestamp_unix: float | int | None = None,
        created_at_unix: float | int | None = None,
        schema_version: int = 1,
    ) -> int: ...

    async def append_events(self, events: list[Mapping[str, Any]]) -> list[int]: ...


@runtime_checkable
class EventStoreLike(EventAppenderLike, Protocol):
    """事件仓储协议。"""

    async def start_projection(self) -> None: ...

    async def shutdown(self, *, timeout: float | None = 5.0) -> bool: ...

    async def wait_projected(
        self,
        event_id: int | None = None,
        *,
        timeout: float | None = None,
    ) -> bool: ...

    async def stats(self) -> dict[str, Any]: ...

    async def get_event(self, event_id: int) -> dict[str, Any] | None: ...

    async def get_events(self, event_ids: list[int]) -> list[dict[str, Any] | None]: ...

    async def iter_events(
        self,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]: ...

    async def events_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        before_event_id: int | None = None,
    ) -> list[dict[str, Any]]: ...

    async def events_by_type(
        self,
        event_type: str,
        *,
        limit: int = 100,
        after_event_id: int | None = None,
        before_event_id: int | None = None,
        order: str = "asc",
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class UsageStatsStoreLike(Protocol):
    """用量统计仓储协议。"""

    async def load(self) -> None: ...

    async def record(
        self,
        usage: Any,
        *,
        provider: str = "",
        model: str = "",
        agent: str = "",
        operation: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None: ...

    def summarize(self, range_name: Any = "today") -> Any: ...

    @property
    def count(self) -> int: ...


class JsonStore:
    """整体 JSON 文件存储（小数据）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def read(self, default: Any = None) -> Any:
        async with self._lock:
            return await self._read_locked(default)

    async def _read_locked(self, default: Any) -> Any:
        if not self.path.exists():
            return default if default is not None else {}
        try:
            async with aiofiles.open(self.path, "rb") as f:
                content = await f.read()
            if not content:
                return default if default is not None else {}
            return orjson.loads(content)
        except orjson.JSONDecodeError as e:
            logger.error(f"JSON 解析失败 {self.path}: {e}")
            raise StoreError(f"{self.path} 格式损坏") from e
        except OSError as e:
            logger.error(f"读取失败 {self.path}: {e}")
            raise StoreError(f"读取 {self.path} 失败: {e}") from e

    async def write(self, data: Any) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                async with aiofiles.open(tmp, "wb") as f:
                    await f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))
                tmp.replace(self.path)
            except OSError as e:
                logger.error(f"写入失败 {self.path}: {e}")
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                raise StoreError(f"写入 {self.path} 失败: {e}") from e


class JsonlStore:
    """JSONL 格式存储（append-heavy 数据，如对话历史）。

    每行一条 JSON 对象。append 性能 O(1)。
    重写（truncate）会全量重写文件。

    内存副本与磁盘同步更新；首次 load 会读取所有行。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._cache: list[dict] | None = None

    async def load(self, force_reload: bool = False) -> list[dict]:
        """加载所有记录。返回内部缓存的拷贝。"""
        async with self._lock:
            if self._cache is None or force_reload:
                await self._reload_from_disk_locked()
            return list(self._cache or [])

    async def _reload_from_disk_locked(self) -> None:
        if not self.path.exists():
            self._cache = []
            return
        records: list[dict] = []
        try:
            async with aiofiles.open(self.path, "rb") as f:
                async for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        records.append(orjson.loads(line))
                    except orjson.JSONDecodeError:
                        logger.warning(
                            f"跳过损坏的历史记录 {self.path}: {line[:80]!r}"
                        )
            self._cache = records
        except OSError as e:
            logger.error(f"读取失败 {self.path}: {e}")
            raise StoreError(f"读取 {self.path} 失败: {e}") from e

    async def append(self, record: dict) -> None:
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            self._cache.append(record)  # type: ignore[union-attr]
            await self._append_to_disk_locked([record])

    async def append_many(self, records: list[dict]) -> None:
        if not records:
            return
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            self._cache.extend(records)  # type: ignore[union-attr]
            await self._append_to_disk_locked(records)

    async def _append_to_disk_locked(self, records: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = b"".join(orjson.dumps(r) + b"\n" for r in records)
        try:
            async with aiofiles.open(self.path, "ab") as f:
                await f.write(data)
        except OSError as e:
            logger.error(f"追加失败 {self.path}: {e}")
            raise StoreError(f"追加到 {self.path} 失败: {e}") from e

    async def length(self) -> int:
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            return len(self._cache or [])

    async def get_slice(self, start: int = 0, end: int | None = None) -> list[dict]:
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            cache = self._cache or []
            if end is None:
                return list(cache[start:])
            return list(cache[start:end])

    async def truncate_head(self, cut_point: int) -> int:
        """删除前 cut_point 条记录，全量重写文件。返回新长度。"""
        async with self._lock:
            await self._ensure_cache_loaded_locked()
            cache = self._cache or []
            if cut_point <= 0:
                return len(cache)
            self._cache = cache[cut_point:]
            await self._rewrite_locked()
            return len(self._cache)

    async def replace_all(self, records: list[dict]) -> None:
        """整体替换。"""
        async with self._lock:
            self._cache = list(records)
            await self._rewrite_locked()

    async def _rewrite_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        cache = self._cache or []
        try:
            data = b"".join(orjson.dumps(r) + b"\n" for r in cache)
            async with aiofiles.open(tmp, "wb") as f:
                await f.write(data)
            tmp.replace(self.path)
        except OSError as e:
            logger.error(f"重写失败 {self.path}: {e}")
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise StoreError(f"重写 {self.path} 失败: {e}") from e

    async def clear(self) -> None:
        async with self._lock:
            self._cache = []
            if self.path.exists():
                try:
                    self.path.unlink()
                except OSError as e:
                    logger.warning(f"删除 {self.path} 失败: {e}")

    async def _ensure_cache_loaded_locked(self) -> None:
        if self._cache is None:
            await self._reload_from_disk_locked()
