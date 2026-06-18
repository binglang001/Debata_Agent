"""Debata_Agent 记忆系统。

公开 API：
    HistoryManager            —— 对话历史（JSONL 增量追加）
    ImportantMemoryManager    —— 重要记忆（整体 JSON）
    ArchiveStore              —— 被压缩出活跃区的原始历史归档
    EventStore                —— 磁盘 append-only 全量事件库
    EventJournal              —— 单 worker 顺序写入事件库
    RollingSummaryStore       —— 全局滚动会话摘要
    DianaDB                   —— diana.db 基础 schema / 版本 / 备份入口
    DianaArchiveStore         —— diana.db archive_messages 轻量归档仓储
    DianaEventStore            —— diana.db event_log 轻量事件仓储
    DianaImportantStore        —— diana.db important_memories 轻量仓储
    DianaRollingSummaryStore   —— diana.db rolling_summary 轻量仓储
    DianaUsageStatsStore       —— diana.db usage_records 轻量仓储
    RagMemoryService          —— RAG 模式的会话向量检索服务
    JsonStore / JsonlStore    —— 底层存储基类（如有特殊需求可直接用）

per-persona 实例化：
    paths = AppPaths(project_root)
    history = HistoryManager(paths.memory_dir_for("yuexi") / "history.jsonl")
    important = ImportantMemoryManager(paths.memory_dir_for("yuexi") / "important.json")
    await history.load()
    await important.load()
"""

from .archive import ArchiveStore
from .conversation_summary import RollingSummaryStore
from .diana_db import (
    DIANA_DB_SCHEMA_VERSION,
    DianaDB,
    DianaDBSchemaVersion,
    DianaDBVersionError,
    backup_existing_database,
)
from .diana_stores import (
    DianaArchiveStore,
    DianaEventStore,
    DianaHistoryStore,
    DianaImportantStore,
    DianaRollingSummaryStore,
    DianaUsageStatsStore,
)
from .event_journal import EventJournal
from .event_store import EventStore
from .history import HistoryManager
from .important import ImportantMemoryManager
from .rag_memory import RagMemoryService, SqliteVectorStore
from .store import JsonlStore, JsonStore, StoreError

__all__ = [
    "ArchiveStore",
    "DIANA_DB_SCHEMA_VERSION",
    "DianaDB",
    "DianaArchiveStore",
    "DianaEventStore",
    "DianaHistoryStore",
    "DianaImportantStore",
    "DianaRollingSummaryStore",
    "DianaUsageStatsStore",
    "DianaDBSchemaVersion",
    "DianaDBVersionError",
    "EventJournal",
    "EventStore",
    "HistoryManager",
    "ImportantMemoryManager",
    "JsonStore",
    "JsonlStore",
    "RagMemoryService",
    "RollingSummaryStore",
    "SqliteVectorStore",
    "StoreError",
    "backup_existing_database",
]
