"""RAG conversation memory backed by a local vector database.

The RAG mode indexes conversation records, not important-memory items. Indexing
is queued in the background so appending a new message never waits for an
embedding request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson

from features.embedding import IEmbeddingService
from utils.token_budget import TokenEstimator

from .rag_store import cosine_similarity

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RagDocument:
    id: str
    text: str
    vector: list[float]
    conversation_id: str | None = None
    role: str = ""
    timestamp: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RagHit:
    document: RagDocument
    score: float


class SqliteVectorStore:
    """Small persistent vector store.

    SQLite is used as the durable vector database. Entries are also mirrored in
    memory for fast cosine top-k without adding a heavyweight dependency.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, RagDocument] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            entries = await asyncio.to_thread(self._load_sync)
            self._entries = {entry.id: entry for entry in entries}
            self._loaded = True
            logger.debug("RAG 向量库加载 %s 条: %s", len(self._entries), self.path)

    async def missing_ids(self, ids: Iterable[str]) -> set[str]:
        async with self._lock:
            return {entry_id for entry_id in ids if entry_id and entry_id not in self._entries}

    async def upsert_many(self, documents: list[RagDocument]) -> None:
        if not documents:
            return
        async with self._lock:
            await asyncio.to_thread(self._upsert_many_sync, documents)
            for document in documents:
                self._entries[document.id] = document

    async def remove_ids(self, ids: Iterable[str]) -> int:
        ids = {entry_id for entry_id in ids if entry_id}
        if not ids:
            return 0
        async with self._lock:
            await asyncio.to_thread(self._remove_ids_sync, ids)
            removed = 0
            for entry_id in ids:
                if self._entries.pop(entry_id, None) is not None:
                    removed += 1
            return removed

    def top_k(
        self,
        query_vec: list[float],
        *,
        k: int,
        conversation_id: str | None = None,
        before_ts: str | None = None,
    ) -> list[RagHit]:
        if not query_vec or not self._entries:
            return []
        candidates = self._entries.values()
        if conversation_id:
            candidates = [
                entry for entry in candidates if entry.conversation_id == conversation_id
            ]
        if before_ts:
            candidates = [
                entry
                for entry in candidates
                if not entry.timestamp or entry.timestamp < before_ts
            ]
        scored = [
            RagHit(document=entry, score=cosine_similarity(query_vec, entry.vector))
            for entry in candidates
        ]
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[: max(1, k)]

    def __len__(self) -> int:
        return len(self._entries)

    def all_entries(self) -> list[RagDocument]:
        return list(self._entries.values())

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_documents (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                role TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                vector BLOB NOT NULL,
                meta BLOB NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_documents_conversation "
            "ON rag_documents(conversation_id)"
        )
        conn.commit()

    def _load_sync(self) -> list[RagDocument]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, conversation_id, role, timestamp, text, vector, meta "
                "FROM rag_documents"
            ).fetchall()
        entries: list[RagDocument] = []
        for row in rows:
            try:
                vector = list(orjson.loads(row[5]))
                meta = orjson.loads(row[6]) if row[6] else {}
                entries.append(
                    RagDocument(
                        id=str(row[0]),
                        conversation_id=str(row[1]) if row[1] else None,
                        role=str(row[2] or ""),
                        timestamp=str(row[3] or ""),
                        text=str(row[4] or ""),
                        vector=[float(v) for v in vector],
                        meta=meta if isinstance(meta, dict) else {},
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("RAG 向量库跳过损坏条目 id=%s: %s", row[0], e)
        return entries

    def _upsert_many_sync(self, documents: list[RagDocument]) -> None:
        now = time.time()
        rows = [
            (
                doc.id,
                doc.conversation_id,
                doc.role,
                doc.timestamp,
                doc.text,
                orjson.dumps(doc.vector),
                orjson.dumps(doc.meta),
                now,
            )
            for doc in documents
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO rag_documents
                    (id, conversation_id, role, timestamp, text, vector, meta, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    conversation_id=excluded.conversation_id,
                    role=excluded.role,
                    timestamp=excluded.timestamp,
                    text=excluded.text,
                    vector=excluded.vector,
                    meta=excluded.meta,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            conn.commit()

    def _remove_ids_sync(self, ids: set[str]) -> None:
        if not ids:
            return
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM rag_documents WHERE id = ?",
                [(entry_id,) for entry_id in ids],
            )
            conn.commit()


class RagMemoryService:
    """Background indexer and query-time retriever for conversation RAG."""

    def __init__(
        self,
        *,
        embedding: IEmbeddingService,
        store: SqliteVectorStore,
        top_k: int = 5,
        max_text_chars: int = 1800,
        batch_size: int = 32,
    ) -> None:
        self.embedding = embedding
        self.store = store
        self.top_k = max(1, top_k)
        self.max_text_chars = max(200, max_text_chars)
        self.batch_size = max(1, batch_size)
        self._queue: asyncio.Queue[list[dict[str, Any]]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._side_tasks: set[asyncio.Task] = set()
        self._closed = False

    async def load(self) -> None:
        await self.store.load()
        removed = await self.store.remove_ids(
            entry.id for entry in self.store.all_entries() if _document_is_runtime_context(entry)
        )
        if removed:
            logger.info("RAG 向量库已清理 %s 条运行时上下文记录", removed)

    def start(self) -> None:
        if self._closed:
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker(),
                name="rag-memory-indexer",
            )

    async def shutdown(self) -> None:
        self._closed = True
        tasks = [task for task in [self._worker_task, *self._side_tasks] if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.debug("RAG 后台任务关闭异常: %s", e, exc_info=True)

    async def enqueue_records(self, records: list[dict]) -> None:
        """Queue records for background indexing.

        This method intentionally does not call the embedding service before
        returning. It is safe to use as HistoryManager.on_append callback.
        """
        if self._closed or not records:
            return
        self.start()
        self._queue.put_nowait([dict(record) for record in records if isinstance(record, dict)])

    def schedule_bootstrap(self, records: list[dict]) -> None:
        if not records or self._closed:
            return
        self.start()
        task = asyncio.create_task(
            self.enqueue_records(records),
            name="rag-memory-bootstrap",
        )
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    async def retrieve_for_query(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        before_ts: str | None = None,
        top_k: int | None = None,
        token_budget: int | None = None,
        estimator: TokenEstimator | None = None,
    ) -> str:
        query = (query or "").strip()
        if not query or len(self.store) == 0:
            return ""
        try:
            qvec = await self.embedding.embed_one(query[: self.max_text_chars])
        except Exception as e:  # noqa: BLE001
            logger.warning("RAG query embedding 失败，跳过本轮召回: %s", e)
            return ""

        hits = self.store.top_k(
            qvec,
            k=top_k or self.top_k,
            conversation_id=conversation_id,
            before_ts=before_ts,
        )
        if not hits:
            return ""
        lines = [f"[相关历史 · RAG 召回 top-{len(hits)}]"]
        for hit in hits:
            lines.append(_format_hit_line(hit))
        return _fit_lines_to_budget(lines, token_budget, estimator)

    async def _worker(self) -> None:
        while True:
            records = await self._queue.get()
            try:
                await self._index_records(records)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("RAG 后台索引失败: %s", e)
            finally:
                self._queue.task_done()

    async def _index_records(self, records: list[dict[str, Any]]) -> None:
        candidates = [
            candidate for record in records
            if (candidate := _record_to_candidate(record, self.max_text_chars)) is not None
        ]
        if not candidates:
            return
        missing = await self.store.missing_ids(candidate["id"] for candidate in candidates)
        candidates = [candidate for candidate in candidates if candidate["id"] in missing]
        if not candidates:
            return

        indexed = 0
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start:start + self.batch_size]
            texts = [candidate["text"] for candidate in batch]
            vectors = await self.embedding.embed_batch(texts)
            documents = [
                RagDocument(
                    id=candidate["id"],
                    text=candidate["text"],
                    vector=vector,
                    conversation_id=candidate["conversation_id"],
                    role=candidate["role"],
                    timestamp=candidate["timestamp"],
                    meta=candidate["meta"],
                )
                for candidate, vector in zip(batch, vectors, strict=False)
                if vector
            ]
            await self.store.upsert_many(documents)
            indexed += len(documents)
        if indexed:
            logger.debug("RAG 后台索引新增 %s 条，当前索引 %s 条", indexed, len(self.store))


def _record_to_candidate(
    record: dict[str, Any],
    max_text_chars: int,
) -> dict[str, Any] | None:
    role = str(record.get("role") or "")
    if role not in {"user", "assistant"}:
        return None
    conversation_id = str(record.get("conversation_id") or "") or None
    if conversation_id and conversation_id.startswith("system:"):
        return None
    if role == "assistant" and record.get("tool_calls"):
        return None
    meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    if _metadata_is_runtime_context(meta):
        return None
    text = _record_content_text(record.get("content"))
    if not text:
        return None
    text = _compact_text(text)[:max_text_chars]
    if not text:
        return None
    if _text_is_runtime_context(text):
        return None
    timestamp = str(meta.get("timestamp") or record.get("timestamp") or "")
    stable_payload = {
        "role": role,
        "content": text,
        "conversation_id": conversation_id,
        "metadata": meta,
    }
    entry_id = "hist:" + hashlib.sha256(
        json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    return {
        "id": entry_id,
        "text": text,
        "conversation_id": conversation_id,
        "role": role,
        "timestamp": timestamp,
        "meta": {
            "source": "history",
            "metadata": meta,
        },
    }


_RUNTIME_CONTEXT_KINDS = frozenset(
    {
        "task_context_snapshot",
        "send_done_snapshot",
    }
)
_RUNTIME_CONTEXT_MARKERS = (
    "<task_context",
    "</task_context>",
    "<send_status",
    "</send_status>",
    "<send_receipt",
    "</send_receipt>",
    "<send_receipt_task",
    "</send_receipt_task>",
)


def _metadata_is_runtime_context(meta: dict[str, Any]) -> bool:
    return meta.get("kind") in _RUNTIME_CONTEXT_KINDS


def _text_is_runtime_context(text: str) -> bool:
    return any(marker in text for marker in _RUNTIME_CONTEXT_MARKERS)


def _document_is_runtime_context(document: RagDocument) -> bool:
    metadata = document.meta.get("metadata")
    meta = metadata if isinstance(metadata, dict) else {}
    return _metadata_is_runtime_context(meta) or _text_is_runtime_context(document.text)


def _record_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False, default=str).strip()
    except TypeError:
        return str(content).strip()


def _compact_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _format_hit_line(hit: RagHit) -> str:
    doc = hit.document
    parts = []
    if doc.timestamp:
        parts.append(doc.timestamp)
    if doc.conversation_id:
        parts.append(doc.conversation_id)
    if doc.role:
        parts.append(_role_label(doc.role))
    prefix = "".join(f"[{part}]" for part in parts)
    text = doc.text.replace("\n", " / ")
    return f"- ({hit.score:.2f}) {prefix} {text}".rstrip()


def _role_label(role: str) -> str:
    return {
        "user": "用户",
        "assistant": "AI",
    }.get(role, role)


def _fit_lines_to_budget(
    lines: list[str],
    token_budget: int | None,
    estimator: TokenEstimator | None,
) -> str:
    if not lines:
        return ""
    if not token_budget or token_budget <= 0:
        return "\n".join(lines)
    estimator = estimator or TokenEstimator()
    selected = [lines[0]]
    used = estimator.estimate_text(lines[0])
    for line in lines[1:]:
        cost = estimator.estimate_text(line)
        if used + cost > token_budget:
            continue
        selected.append(line)
        used += cost
    return "\n".join(selected)


__all__ = [
    "RagDocument",
    "RagHit",
    "RagMemoryService",
    "SqliteVectorStore",
]
