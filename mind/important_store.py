"""重要记忆 SQLite store 适配器。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import PersonaDB


class SqliteImportantStore:
    """ImportantMemoryManager 可注入的 SQLite store。"""

    def __init__(self, path_or_db: str | Path | PersonaDB) -> None:
        if isinstance(path_or_db, PersonaDB):
            self._db = path_or_db
        else:
            self._db = PersonaDB(path_or_db)

    @property
    def db(self) -> PersonaDB:
        return self._db

    async def read(self, default: Any = None) -> Any:
        return await self._db.read_important(default=default)

    async def write(self, data: Any) -> None:
        await self._db.write_important(data)

    async def close(self) -> None:
        await self._db.close()
