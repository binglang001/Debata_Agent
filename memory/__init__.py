"""Debata_Agent 记忆系统。

公开 API：
    HistoryManager            —— 对话历史（JSONL 增量追加）
    ImportantMemoryManager    —— 重要记忆（整体 JSON）
    ArchiveStore              —— 被压缩出活跃区的原始历史归档
    EventStore                —— 磁盘 append-only 全量事件库
    EventJournal              —— 单 worker 顺序写入事件库
    RollingSummaryStore       —— 全局滚动会话摘要
    DebataDB                   —— debata.db 基础 schema / 版本 / 备份入口
    DebataArchiveStore         —— debata.db archive_messages 轻量归档仓储
    DebataEventStore            —— debata.db event_log 轻量事件仓储
    DebataImportantStore        —— debata.db important_memories 轻量仓储
    DebataPersonaDB             —— debata.db persona_* legacy domains 仓储
    DebataRollingSummaryStore   —— debata.db rolling_summary 轻量仓储
    DebataUsageStatsStore       —— debata.db usage_records 轻量仓储
    import_legacy_memory_files —— 旧 memory/logs 文件到 debata.db 的同步导入入口
    import_legacy_memory_files_async —— 旧 memory/logs 文件到 debata.db 的异步导入入口
    RagMemoryService          —— RAG 模式的会话向量检索服务
    *StoreLike                —— 业务门面依赖的仓储协议
    JsonStore / JsonlStore    —— 底层存储实现（如有特殊需求可直接用）

per-persona 实例化：
    paths = AppPaths(project_root)
    history = HistoryManager(paths.memory_dir_for("yuexi") / "history.jsonl")
    important = ImportantMemoryManager(paths.memory_dir_for("yuexi") / "important.json")
    await history.load()
    await important.load()
"""

from .archive import ArchiveStore
from .conversation_summary import RollingSummaryStore
from .debata_db import (
    DEBATA_DB_SCHEMA_VERSION,
    DebataDB,
    DebataDBSchemaVersion,
    DebataDBVersionError,
    backup_existing_database,
)
from .debata_importers import (
    LegacyImportDomainResult,
    LegacyMemoryImportResult,
    import_legacy_memory_files,
    import_legacy_memory_files_async,
)
from .debata_stores import (
    DebataArchiveStore,
    DebataEventStore,
    DebataHistoryStore,
    DebataImportantStore,
    DebataPersonaDB,
    DebataRollingSummaryStore,
    DebataUsageStatsStore,
)
from .event_journal import EventJournal
from .event_store import EventStore
from .history import HistoryManager
from .important import ImportantMemoryManager
from .rag_memory import RagMemoryService, SqliteVectorStore
from .store import (
    ArchiveStoreLike,
    EventAppenderLike,
    EventStoreLike,
    JsonlStore,
    JsonlStoreLike,
    JsonStore,
    JsonStoreLike,
    RollingSummaryStoreLike,
    StoreError,
    UsageStatsStoreLike,
)

__all__ = [
    "ArchiveStore",
    "DEBATA_DB_SCHEMA_VERSION",
    "DebataDB",
    "DebataArchiveStore",
    "DebataEventStore",
    "DebataHistoryStore",
    "DebataImportantStore",
    "DebataPersonaDB",
    "DebataRollingSummaryStore",
    "DebataUsageStatsStore",
    "DebataDBSchemaVersion",
    "DebataDBVersionError",
    "EventJournal",
    "EventStore",
    "HistoryManager",
    "ImportantMemoryManager",
    "ArchiveStoreLike",
    "EventAppenderLike",
    "EventStoreLike",
    "JsonStore",
    "JsonStoreLike",
    "JsonlStore",
    "JsonlStoreLike",
    "LegacyImportDomainResult",
    "LegacyMemoryImportResult",
    "RagMemoryService",
    "RollingSummaryStore",
    "RollingSummaryStoreLike",
    "SqliteVectorStore",
    "StoreError",
    "UsageStatsStoreLike",
    "backup_existing_database",
    "import_legacy_memory_files",
    "import_legacy_memory_files_async",
]
