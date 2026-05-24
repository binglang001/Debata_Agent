"""DuckDuckGo 联网搜索 —— 使用 ddgs 库"""

import asyncio

from nonebot.log import logger


async def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网，返回格式化摘要。"""

    def _search():
        from ddgs import DDGS
        return list(DDGS().text(query, max_results=max_results))

    try:
        results = await asyncio.to_thread(_search)
    except Exception as e:
        logger.error(f"DDG 搜索失败: {e}")
        return f"[搜索失败: {e}]"

    if not results:
        return f"未找到与「{query}」相关的结果。"

    parts = []
    for r in results:
        title = r.get("title", "")
        body = r.get("body", "")
        if title and body:
            parts.append(f"- **{title}**\n  {body[:200]}")
        elif body:
            parts.append(f"- {body[:200]}")
        elif title:
            parts.append(f"- **{title}**")

    if not parts:
        return f"未找到与「{query}」相关的结果。"

    return "\n".join(parts)
