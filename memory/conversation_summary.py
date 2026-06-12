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
        archived_until = data.get("archived_until")
        legacy_active_start = self._coerce_active_start(data.get("active_start_index"))
        if legacy_active_start is not None:
            if isinstance(archived_until, dict):
                archived_until = dict(archived_until)
            elif archived_until is None:
                archived_until = {}
            else:
                archived_until = {"legacy_archived_until": archived_until}
            archived_until.setdefault("active_start_index", legacy_active_start)
        self._data = {
            "summary_text": str(data.get("summary_text") or ""),
            "archived_until": archived_until,
            "updated_at": str(data.get("updated_at") or ""),
        }
        return dict(self._data)

    def text(self) -> str:
        return str(self._data.get("summary_text") or "")

    def active_start_index(self) -> int:
        """当前活跃窗口在完整 history 中的起点。"""
        archived_until = self._data.get("archived_until")
        value: Any = None
        if isinstance(archived_until, dict):
            value = archived_until.get("active_start_index")
        if value is None:
            value = self._data.get("active_start_index")
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    async def update(
        self,
        summary_text: str,
        *,
        archived_until: Any = None,
        updated_at: str = "",
        active_start_index: int | None = None,
    ) -> None:
        archived_payload = archived_until
        if active_start_index is not None:
            active_start_index = max(0, int(active_start_index))
            if isinstance(archived_payload, dict):
                archived_payload = dict(archived_payload)
            elif archived_payload is None:
                archived_payload = {}
            else:
                archived_payload = {"legacy_archived_until": archived_payload}
            archived_payload["active_start_index"] = active_start_index
        new_data = {
            "summary_text": summary_text.strip(),
            "archived_until": archived_payload,
            "updated_at": updated_at,
        }
        await self._store.write(new_data)
        self._data = new_data

    @staticmethod
    def _coerce_active_start(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None
