"""滚动会话摘要存储。

全局一份摘要，用来承载被压缩出活跃 history 的长期上下文。它不是按会话隔离的
记忆库，只是统一意识的一份压缩背景。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import JsonStore


class RollingSummaryStore:
    """全局滚动摘要。"""

    def __init__(self, path: Path) -> None:
        self._store = JsonStore(path)
        self._data: dict[str, Any] = {
            "summary_text": "",
            "archived_until": None,
            "updated_at": "",
        }

    async def load(self) -> dict[str, Any]:
        data = await self._store.read(default=dict(self._data))
        if not isinstance(data, dict):
            data = dict(self._data)
        self._data = {
            "summary_text": str(data.get("summary_text") or ""),
            "archived_until": data.get("archived_until"),
            "updated_at": str(data.get("updated_at") or ""),
        }
        return dict(self._data)

    def text(self) -> str:
        return str(self._data.get("summary_text") or "")

    async def update(
        self,
        summary_text: str,
        *,
        archived_until: Any = None,
        updated_at: str = "",
    ) -> None:
        self._data = {
            "summary_text": summary_text.strip(),
            "archived_until": archived_until,
            "updated_at": updated_at,
        }
        await self._store.write(self._data)
