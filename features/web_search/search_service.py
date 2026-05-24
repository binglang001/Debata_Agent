"""DuckDuckGo 搜索 service。

duckduckgo_search 包提供同步 API；在异步上下文里用 asyncio.to_thread 包一层。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)


class WebSearchService:
    """DuckDuckGo 网页搜索。"""

    def __init__(
        self,
        *,
        max_results: int = 5,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.max_results = max_results
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str) -> str:
        """返回搜索结果的文本拼接（编号 + 标题 + 摘要 + URL）。"""
        if not query.strip():
            return "（搜索词为空）"

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._sync_search, query),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return f"（搜索超时 > {self.timeout_seconds}s）"
        except Exception as e:
            logger.warning(f"web_search 失败 query={query!r}: {e}")
            return f"（搜索失败：{type(e).__name__}: {e}）"

        if not results:
            return f"无相关结果：{query}"

        lines: list[str] = []
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "").strip()
            body = (r.get("body") or "").strip()
            href = (r.get("href") or "").strip()
            lines.append(f"{i}. {title}\n   {body}\n   {href}")
        return "\n\n".join(lines)

    def _sync_search(self, query: str) -> list[dict[str, Any]]:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=self.max_results))
