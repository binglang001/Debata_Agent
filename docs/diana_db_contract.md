# diana.db v1 契约

本文档定义阶段 2 的 `diana.db` 基础契约：单库位置、版本记录、备份策略、v1 空表结构与索引。当前实现包含建库和版本/备份能力，以及 `DianaHistoryStore` 对 `history_records`、`DianaImportantStore` 对 `important_memories`、`DianaRollingSummaryStore` 对 `rolling_summary`、`DianaUsageStatsStore` 对 `usage_records`、`DianaEventStore` 对 `event_log`、`DianaArchiveStore` 对 `archive_messages` / `archive_message_media`、`DianaPersonaDB` 对 `persona_*` legacy domains 的最小读写；旧文件导入器覆盖 `history.jsonl`、`important.json`、`rolling_summary.json`、`model_usage.jsonl`、`events.sqlite3`、`events.sqlite3.append.jsonl`、`archive.sqlite3` 与 `persona.db`；runtime 已接线到每人格 `memory/<persona>/diana.db`。向量库/RAG 不并入 `diana.db`，运行时使用实例级 `vector/<persona>/rag_memory.sqlite3` 独立文件，随实例目录搬迁。

## 文件与版本

- 默认目标文件：`DATA_DIR/db/diana.db`。本模块只接收显式路径，不负责解析 `DATA_DIR`。
- 当前 schema 版本：`1`，由 `memory.diana_db.DIANA_DB_SCHEMA_VERSION` 暴露。
- SQLite `PRAGMA user_version` 必须等于当前 schema 版本。
- 内部迁移表：`schema_migrations(version, migration_id, applied_at)`。
- v1 初始化迁移：`version = 1`，`migration_id = v1_initial_schema`。
- 初始化必须幂等：重复 `load()` 不重复插入迁移记录。
- 打开已有库时，如果 `PRAGMA user_version` 或 `schema_migrations` 最大 `version` 高于当前代码支持版本，必须拒绝加载并抛出版本异常；禁止写入 schema，也禁止把版本号降级。

## 连接 PRAGMA

所有由 `DianaDB.connect()` 创建的连接必须设置：

- `journal_mode=WAL`
- `synchronous=NORMAL`
- `foreign_keys=ON`
- `busy_timeout=30000`

## 备份策略

`backup_existing_database()` 在迁移前备份整个 `diana.db` 到同级 `backups/` 目录：

- 源库不存在时返回 `None`，且不创建空备份。
- 文件名格式：`diana-v<schema_version>-<UTC timestamp>.db`。
- 时间戳默认格式：`YYYYMMDDTHHMMSSZ`。
- 如果目标文件已存在，追加 `-1`、`-2` 等后缀，禁止覆盖已有备份。
- 选定备份目标后必须用独占创建占位文件，避免并发选择同一路径；备份失败时只删除本次函数调用创建的占位/目标。
- 备份使用 SQLite backup API 复制整库，产物必须能被 sqlite 打开。

## JSON 保真要求

以下列是原始记录保真列，导入器和后续仓储必须原样保留，不得只保存提取字段：

- `history_records.record_json`
- `important_memories.item_json`
- `rolling_summary.summary_json`
- `event_log.payload_json`
- `archive_messages.metadata_json`
- `archive_messages.record_json`
- `archive_message_media.metadata_json`
- `usage_records.record_json`
- `persona_state.state_json`
- `persona_state_log.state_json`
- `persona_update_audits.audit_json`
- `persona_effects.effect_json`
- `persona_todos.todo_json`
- `persona_cues.cue_json`
- `persona_inner_monologues.monologue_json`
- `persona_user_profiles.profile_json`
- `persona_important_state_legacy.memories_json`
- `persona_daily_trajectories.trajectory_json`
- `persona_arc.event_json`
- `persona_sleep_records.record_json`
- `persona_eat_records.record_json`

## Runtime 接线约定

- Runtime 对当前人格使用 `paths.memory_dir_for(persona.name) / "diana.db"`，不使用全局 `DATA_DIR/db/diana.db` 默认路径。
- `Runtime.start()` 在记忆阶段先调用异步导入入口，把旧人格 memory 目录中的 `history.jsonl`、`important.json`、`rolling_summary.json`、`events.sqlite3`、`events.sqlite3.append.jsonl`、`archive.sqlite3`、`persona.db` 和日志目录中的 `model_usage.jsonl` 导入当前人格的 `diana.db`。旧文件保留，不删除。
- Runtime 只在旧源存在且对应 diana 目标域为空时触发旧文件导入；已初始化并已有数据的目标域不会再从旧文件整体覆盖。无实际导入需求时不调用 importer，因此不会在每次启动时生成备份。
- Runtime 保留现有 Manager/门面调用面：`HistoryManager` 注入 `DianaHistoryStore`，`ImportantMemoryManager` 注入 `DianaImportantStore`，`ArchiveStore` 注入 `DianaArchiveStore`，`EventJournal` 包装 `DianaEventStore`，滚动摘要和 usage 分别直接使用 `DianaRollingSummaryStore` 与 `DianaUsageStatsStore`。
- persona management 启用时，runtime 使用 `DianaPersonaDB(diana_db, persona_id)`，不再新建 `persona.db`，重要记忆仍通过 `DianaImportantStore` 作为运行时主后端。
- `UsageStatsStore` 的运行时后端是 `DianaUsageStatsStore(diana_db, persona_id)`；旧 usage 导入源由 runtime 显式传入 `paths.LOGS_DIR / "model_usage.jsonl"`。
- RAG/vector 相关存储由实例级 `paths.vector_dir_for(persona.name) / "rag_memory.sqlite3"` 负责，不在本阶段迁入 `diana.db`，也不由 diana 导入器处理。若旧 `memory/<persona>/rag_memory.sqlite3` 存在且新 vector 文件不存在，runtime 启动 RAG 前会复制旧主库及存在的 `-wal` / `-shm` sidecar 到新目录；旧文件保留，目标已存在时不覆盖。

## 第一批旧文件导入器

`memory.diana_importers.import_legacy_memory_files(db, source_dir, persona_id, *, backup=True, usage_persona_id=None, usage_source_path=None, skip_existing_domains=False)` 是同步导入入口，用于把旧 `memory/` 与 `logs/` 中第一批文件导入已存在的 `diana.db` stores。`memory.diana_importers.import_legacy_memory_files_async(...)` 使用同一参数签名，是等价异步入口，供 `Runtime.start()` 等 running event loop 内调用；同步入口在 running event loop 中仍抛错。参数约定：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。入口会主动 `load()` 并确保 schema 可用。
- `source_dir`：旧 `history.jsonl`、`important.json`、`rolling_summary.json`、`events.sqlite3`、`events.sqlite3.append.jsonl`、`archive.sqlite3`、`persona.db` 所在目录。默认 usage 源仍兼容为 `source_dir / "model_usage.jsonl"`。
- `persona_id`：写入 `history_records`、`important_memories`、`rolling_summary`、`event_log`、`event_projection_state`、`archive_messages`、`archive_message_media` 与 `persona_*` 目标表的目标 persona。
- `backup=True`：导入前如果目标 `diana.db` 已存在，调用 `backup_existing_database()`；目标库不存在时不创建备份。
- `usage_persona_id=None`：默认跟随 `persona_id` 写入 `usage_records.persona_id`。显式传入空字符串表示全局 usage，写库时 `persona_id` 为 `NULL`。
- `usage_source_path=None`：默认保持旧行为，从 `source_dir / "model_usage.jsonl"` 读取；runtime 应显式传入 `paths.LOGS_DIR / "model_usage.jsonl"`，因为 usage 旧文件位于 logs 目录而非人格 memory 目录。
- `skip_existing_domains=False`：默认保持旧导入器行为；runtime 传 `True`，对已有数据的 history/important/rolling/usage/events/archive/persona 域跳过导入，避免旧文件覆盖运行期写入的 diana 数据。

返回值是 `LegacyMemoryImportResult`，包含 `backup_path` 以及 `history`、`important`、`rolling_summary`、`usage`、`events`、`archive`、`persona` 七个 `LegacyImportDomainResult(imported, skipped)` 统计，供 runtime 后续展示。`important.imported` 统计最终写入新 `important_memories` 域的 item 数；`important.skipped` 统计损坏/非列表合并源与双源去重丢弃的 item 数。`archive` 统计按归档消息行与归档媒体行合计，`persona` 统计按旧 `persona.db` 导入到 `persona_*` 目标表的行合计。

导入映射：

- `history.jsonl` -> `DianaHistoryStore.replace_all(...)`。
- `important.json` 与 `persona.db important_memories.memories_json` -> 双源合并后 `DianaImportantStore.write(...)`。`persona.db` 是权威源，输出顺序为 persona 聚合列表项在前、`important.json` 中未被同 ID 覆盖的项在后；缺少 `id` 的 item 使用基于规范化 JSON 的稳定 fallback ID 做跨源去重，但不改写 item 本身。
- `rolling_summary.json` -> 直写 `rolling_summary` 单行；查询列按 store 读取契约抽取，`summary_json` 完整保留旧 JSON 对象。
- `model_usage.jsonl` -> 直接写入 `usage_records`，保留完整 `record_json`，并抽取 usage 常用列；源文件路径由 `usage_source_path` 决定，未传时兼容 `source_dir / "model_usage.jsonl"`。
- `events.sqlite3` 与 `events.sqlite3.append.jsonl` -> 直接写入 `event_log`；先导入 SQLite 已投影行，再导入 append log 行；保留旧事件自带 `event_id`、`event_uuid`、`schema_version`、`payload_json`、`payload_hash` 与其他事件列，不调用会重新分配 ID 的事件追加接口。导入完成后写入 `event_projection_state(persona_id, "last_projected_event_id")` 为当前 persona 最大 `event_id`。
- `archive.sqlite3` -> 直接写入 `archive_messages` 与 `archive_message_media`；先导入消息，再导入媒体；保留旧 `rowid`、`archive_id`、媒体 `id`、`metadata_json`、`record_json` 与所有抽取列，不调用会重新归一化并重新分配 ID 的归档追加接口。早期旧库缺少 `archive_messages.record_json` 时写入 `NULL`，不视为损坏行。

幂等与容错：

- 缺失文件视为该域 `imported=0, skipped=0`，不写入该域。
- `history`、`important`、`rolling_summary` 采用整体替换或 upsert 语义；重复导入不会追加重复记录。`important` 重跑时继续整体替换当前 persona 的新 `important_memories` 域。
- `important` 双源合并按 item ID 去重；同 ID 冲突时保留 `persona.db important_memories.memories_json` 中的 persona 项，跳过 `important.json` 中的同 ID 项并计入 `important.skipped`。无 `id` item 使用与 `DianaImportantStore` 相同的稳定 fallback ID 判断跨源重复，避免双源完全相同的无 ID item 重复写入。
- `usage` 按同一 `usage_persona_id` 下的 `record_json` 去重；重复导入同一旧记录时计入 skipped，不新增行。
- `events` 按同一 `persona_id` 下的 `event_id` 与 `idempotency_key` 去重。相同 `event_id` 且除 `persona_id` 外的事件保真列完整一致视为重复并计入 skipped；相同 `event_id` 但任一事件保真列冲突，或相同 `idempotency_key` 指向不同事件时，记录 warning 并计入 skipped，不覆盖已有行。
- `archive` 按同一 `persona_id` 下的消息 `rowid`、消息 `archive_id` 与媒体 `id` 去重。相同消息 `rowid` 或 `archive_id` 且除 `persona_id` 外的完整归档消息列一致视为重复并计入 skipped；任一列冲突时记录 warning 并计入 skipped，不覆盖已有行。相同媒体 `id` 且除 `persona_id` 外的完整媒体列一致视为重复并计入 skipped；任一列冲突时记录 warning 并计入 skipped，不覆盖已有行。同一 `archive_id` 可以有多条不同媒体 `id`。
- `persona` 读取 `source_dir/persona.db`，在 `target_db.load()` 后用直接 SQLite 导入到 `persona_*` 表，不调用 `DianaPersonaDB` 写方法生成新 ID。缺少 `persona.db` 时 `persona.imported=0` 且 `persona.skipped=0`。缺表记录 warning 并跳过该表；源表缺关键主键/必要列时记录 warning，并按该表可数行计入 skipped；单行关键字段缺失或目标 NOT NULL 列无法满足时记录 warning 并跳过该行。
- `persona` 按同一 `persona_id` 下的目标主键去重。相同主键且除 `persona_id` 外的完整目标列一致视为重复并计入 skipped；相同主键但任一目标列冲突时记录 warning 并计入 skipped，不覆盖已有行。旧 `eat_records` 兼容缺少 `record_id`、`ended_at`、`status` 的库；缺 `record_id` 时按 `record_json.record_id/eat_id/id` 推导，仍无值则使用 `eat_<id>` fallback。
- JSONL 中空行、损坏行、非对象行会跳过并记录 warning；损坏 JSON 文件会跳过该文件来源并记录 warning，不让整体导入崩溃。`important.json` 损坏时仍保留 warning/skipped；如果 `persona.db important_memories.memories_json` 有有效列表，仍会把 persona 来源项写入新 `important_memories`。
- `persona.db important_memories.memories_json` 只在解析为 list 时参与新 `important_memories` 合并；非 list、空值或 JSON 损坏时记录 warning 并计入 `important.skipped`，不吸收该来源。无论合并源是否有效，旧 `persona.db important_memories` 到 `persona_important_state_legacy` 的保真复制与 `persona` 统计仍按 persona 导入规则独立处理。
- 事件 append log 中损坏行、非对象行、缺失关键字段行、损坏 `payload_json` 行会跳过并记录 warning，不让整体导入崩溃。`events.sqlite3` 缺失或缺少 `event_log` 表时按该域缺失处理。
- `archive.sqlite3` 缺失时 `archive` 统计为 `imported=0, skipped=0`，不写库。缺少 `archive_messages` 表时按缺失域处理并记录 warning；缺少 `archive_message_media` 表时只导入消息并记录 warning。媒体导入只接受当前 persona 下已存在的 `archive_id`，孤儿媒体行跳过并记录 warning。损坏或缺少关键字段的归档消息/媒体单行跳过并记录 warning，不让整体导入崩溃。
- 只有指定 persona 的 `history`、`important`、`rolling_summary` 会被替换；其他 persona 不受影响。`usage` 去重和写入按 `usage_persona_id` 隔离。`events` 的 `event_id` 与 `idempotency_key` 唯一性按 `persona_id` 隔离，同一旧事件可导入多个 persona。`archive` 的 `rowid`、`archive_id` 与媒体 `id` 也按 `persona_id` 隔离，同一旧归档库可导入多个 persona。`persona` 的旧主键也按 `persona_id` 隔离，同一旧 `persona.db` 可导入多个 persona。

## 表结构

### schema_migrations

记录 diana.db 内部迁移状态。

| 列 | 类型 | 约束 |
|---|---|---|
| version | INTEGER | PRIMARY KEY |
| migration_id | TEXT | UNIQUE NOT NULL |
| applied_at | TEXT | NOT NULL |

### history_records

保留 `history.jsonl` 的逐条记录，并提取常用查询列。

| 列 | 类型 | 约束 |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| persona_id | TEXT | NOT NULL |
| history_index | INTEGER | NOT NULL |
| conversation_id | TEXT |  |
| role | TEXT |  |
| content_hash | TEXT |  |
| content_length | INTEGER | NOT NULL DEFAULT 0 |
| record_json | TEXT | NOT NULL |
| timestamp | TEXT |  |
| created_at | TEXT | NOT NULL DEFAULT current UTC text |
| updated_at | TEXT | NOT NULL DEFAULT current UTC text |

约束：`UNIQUE(persona_id, history_index)`。

索引：

- `idx_history_persona_index(persona_id, history_index)`
- `idx_history_conversation(persona_id, conversation_id, history_index)`
- `idx_history_role(persona_id, role)`
- `idx_history_content_hash(content_hash)`

#### DianaHistoryStore 接口

`memory.diana_stores.DianaHistoryStore(db, persona_id)` 是 `history_records` 的轻量仓储，构造参数：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。
- `persona_id`：人格 ID；同一 persona 的历史是一条全局时间线，不按 `conversation_id` 拆分。

异步接口与 `JsonlStore` 对齐：

- `load(force_reload=False) -> list[dict]`：按 `history_index` 升序返回当前 persona 的完整历史；默认使用缓存，`force_reload=True` 强制从数据库读取。
- `append(record: dict)`：追加单条记录。
- `append_many(records: list[dict])`：追加多条记录；空列表不写库。
- `length() -> int`：返回当前 persona 的记录数。
- `get_slice(start=0, end=None) -> list[dict]`：按当前全局流位置切片，不按会话筛选。
- `truncate_head(cut_point) -> int`：删除最早的 `cut_point` 条记录，返回剩余长度；剩余记录重新写入为连续 `history_index`。
- `replace_all(records: list[dict]) -> None`：用传入记录整体替换当前 persona 的历史流，`history_index` 从 0 连续写入。
- `clear() -> None`：删除当前 persona 的全部历史记录。

写入语义：

- 每条输入记录完整保存到 `record_json`，不得只保存提取字段。
- 读取时遇到损坏的 `record_json` 或非对象 JSON 会跳过该行并记录 warning；仓储不在读取阶段回写修复数据。
- 同一 persona 下 `history_index` 按当前全局流连续递增；不同 `conversation_id` 仍共享同一序列。
- 同步提取 `conversation_id`、`role`、`content_hash`、`content_length`、`timestamp` 供后续查询使用。
- 所有数据库操作由仓储锁保护，并通过后台线程执行，避免阻塞事件循环。

### important_memories

保留旧 `important.json` 或后续重要记忆的一条一行结构。

| 列 | 类型 | 约束 |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| persona_id | TEXT | NOT NULL |
| memory_id | TEXT | NOT NULL |
| timestamp | TEXT |  |
| scope | TEXT |  |
| pinned | INTEGER | NOT NULL DEFAULT 0 |
| content | TEXT |  |
| item_json | TEXT | NOT NULL |
| created_at | TEXT | NOT NULL DEFAULT current UTC text |
| updated_at | TEXT | NOT NULL |

约束：`UNIQUE(persona_id, memory_id)`。

索引：

- `idx_important_persona_time(persona_id, timestamp)`
- `idx_important_persona_scope(persona_id, scope)`
- `idx_important_pinned(persona_id, pinned)`

#### DianaImportantStore 接口

`memory.diana_stores.DianaImportantStore(db, persona_id)` 是 `important_memories` 的轻量仓储，构造参数：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。
- `persona_id`：人格 ID；同一 persona 的重要记忆是一份整体 JSON 列表。

异步接口与 `JsonStore` 对齐：

- `read(default=None) -> Any`：按写入顺序读取当前 persona 的重要记忆。没有记录时返回传入的 `default`；如果 `default is None`，与 `JsonStore` 一致返回 `{}`。
- `write(data: Any) -> None`：整体替换当前 persona 的重要记忆；不同 persona 的记录不受影响。

写入语义：

- 主路径是 `list[dict]`：列表中每个 item 写入 `important_memories` 一行，读取时按写入顺序返回 list。
- 每个 item 完整保存到 `item_json`，不得只保存提取字段；仓储不会为了生成缺失 ID 而改写原 item。
- 同步提取 `id` 到 `memory_id`，提取 `timestamp`、`scope`、`pinned`、`content`、`updated_at` 到同名列供后续查询使用。
- item 缺少 `id` 时，`memory_id` 使用基于 item 规范化 JSON 的稳定 fallback；同一次写入中若出现重复 fallback 或重复 id，会在后续重复项后追加序号，避免破坏唯一约束。
- `write()` 使用删除再插入实现整体替换，同一 persona 幂等替换，不影响其他 persona。
- 非 list 数据按 legacy/raw 单行保存：`memory_id = "__diana_important_raw__"`，`scope = "__legacy_raw__"`，`item_json` 保存原始 JSON；读取该行时返回原始非 list 数据。`ImportantMemoryManager` 主路径会以 `read(default=[])` 读取空表，因此新数据应保持 list。
- 读取时遇到损坏的 `item_json` 会跳过该行并记录 warning；仓储不在读取阶段回写修复数据。
- 所有数据库写入和读取由仓储锁保护，并通过后台线程执行，避免阻塞事件循环。

### rolling_summary

按 persona 单行保存滚动摘要。

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | PRIMARY KEY |
| summary_text | TEXT | NOT NULL DEFAULT '' |
| archived_until_json | TEXT | NOT NULL DEFAULT '{}' |
| active_start_index | INTEGER |  |
| summary_json | TEXT | NOT NULL DEFAULT '{}' |
| updated_at | TEXT | NOT NULL |

#### DianaRollingSummaryStore 接口

`memory.diana_stores.DianaRollingSummaryStore(db, persona_id)` 是 `rolling_summary` 的轻量仓储，构造参数：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。
- `persona_id`：人格 ID；同一 persona 只保存一行滚动摘要。

异步接口与旧 `RollingSummaryStore` 对齐：

- `load() -> dict[str, Any]`：读取当前 persona 摘要。没有记录时返回 `summary_text=""`、`archived_until=None`、`updated_at=""`，并刷新内存缓存但不写库。
- `text() -> str`：返回内存缓存中的摘要文本。
- `active_start_index() -> int`：先读取 `archived_until.active_start_index`，否则兼容旧顶层 `active_start_index`；非法值返回 `0`。
- `update(summary_text, *, archived_until=None, updated_at="", active_start_index=None) -> None`：整体替换当前 persona 摘要。

写入语义：

- `summary_text` 列保存去除首尾空白后的摘要文本。
- `archived_until_json` 保存 `archived_until` 的 JSON 表示；`None` 保存为 JSON `null`，读取时还原为 `None`。
- `active_start_index` 列保存归一化后的非负整数或 `NULL`。当 `active_start_index` 参数不为 `None` 时，同时写入 `archived_until.active_start_index`；如果原 `archived_until` 不是对象，按旧类语义封装为 `{"legacy_archived_until": old, "active_start_index": n}`。
- `summary_json` 保存完整新数据对象：`{"summary_text": ..., "archived_until": ..., "updated_at": ...}`，用于未来导出和审计。
- `updated_at` 列保存调用方传入值，允许空字符串。
- 读取时先从 `summary_text`、`archived_until_json`、`active_start_index`、`updated_at` 列构造基线数据，再解析 `summary_json`。如果 `summary_json` 是对象，只用其中实际存在的 `summary_text`、`archived_until`、`updated_at` 关键字段覆盖基线数据；合法但缺关键字段的对象（例如 schema 默认 `{}`）不得吞掉列字段。`summary_json` 顶层 `active_start_index` 仅在实际存在时参与旧数据兼容归一化；不存在时继续使用列中的 `active_start_index`。
- 如果 `summary_json` 损坏或不是对象，记录 warning，并只使用列字段回退构造。
- 仓储只管理当前 persona 单行，不影响其他 persona。
- 所有数据库写入和读取由仓储锁保护，并通过后台线程执行，避免阻塞事件循环。

### event_log

复制现 `memory/event_store.py` 的 `event_log` 字段，并新增 `persona_id`。

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| event_id | INTEGER | NOT NULL |
| event_type | TEXT | NOT NULL |
| event_uuid | TEXT | NOT NULL |
| conversation_id | TEXT |  |
| session_id | TEXT |  |
| turn_id | TEXT |  |
| source | TEXT |  |
| external_id | TEXT |  |
| tool_call_id | TEXT |  |
| parent_event_id | INTEGER |  |
| idempotency_key | TEXT |  |
| timestamp_unix | REAL | NOT NULL |
| created_at_unix | REAL | NOT NULL |
| payload_json | TEXT | NOT NULL |
| payload_hash | TEXT | NOT NULL |
| schema_version | INTEGER | NOT NULL |

约束：`PRIMARY KEY(persona_id, event_id)`；`idempotency_key` 在同一 persona 内唯一。

索引：

- `idx_event_log_persona_idempotency(persona_id, idempotency_key) WHERE idempotency_key IS NOT NULL`
- `idx_event_log_conversation_event(persona_id, conversation_id, event_id)`
- `idx_event_log_type_event(persona_id, event_type, event_id)`
- `idx_event_log_session_event(persona_id, session_id, event_id)`
- `idx_event_log_external(persona_id, source, external_id)`
- `idx_event_log_parent(persona_id, parent_event_id)`

### event_projection_state

复制现 `memory/event_store.py` 的投影状态字段，并新增 `persona_id`。

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| name | TEXT | NOT NULL |
| value | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, name)`。

#### DianaEventStore 接口

`memory.diana_stores.DianaEventStore(db, persona_id)` 是 `event_log` / `event_projection_state` 的轻量事件仓储，面向 `EventJournal` 的当前调用面实现。构造参数：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。
- `persona_id`：人格 ID；去除首尾空白后不能为空。事件 ID、幂等键、读取查询都按 persona 隔离。

异步接口与 `EventJournal` 依赖的 `EventStore` 调用面对齐：

- `start_projection() -> None`：确保 schema 与当前状态可用；不启动后台 worker。
- `append_event(...) -> int`：追加单条事件，关键字参数形状与旧 `EventStore.append_event()` 一致。
- `append_events(events) -> list[int]`：追加一批事件，返回与输入顺序对应的 event_id。
- `wait_projected(event_id=None, timeout=None) -> bool`：`event_id is None` 时刷新当前 persona 最大 event_id 后立即返回已追平；显式 event_id 已存在时立即返回 `True`，未来 event_id 会短间隔轮询当前 persona 最大 event_id，直到到达目标或 timeout 到期。
- `stats() -> dict[str, Any]`：返回当前 persona 的事件进度和固定投影指标。
- `shutdown(timeout=5.0) -> bool` / `close(timeout=5.0) -> bool`：标记仓储关闭；无后台 worker 需要回收。
- `get_event(event_id) -> dict[str, Any] | None`
- `get_events(event_ids) -> list[dict[str, Any] | None]`
- `iter_events(limit=100, after_event_id=None, before_event_id=None, order="asc") -> list[dict[str, Any]]`
- `events_for_conversation(conversation_id, limit=100, before_event_id=None) -> list[dict[str, Any]]`
- `events_by_type(event_type, limit=100, after_event_id=None, before_event_id=None, order="asc") -> list[dict[str, Any]]`

写入语义：

- 不复制旧 `EventStore` 的外置 append-log、后台投影 worker 和 backpressure 机制。
- `append_events()` 在单个 SQLite `BEGIN IMMEDIATE` 事务中直接写入 `event_log`，并同步更新 `event_projection_state(persona_id, "last_projected_event_id")` 到当前 persona 最大 event_id。
- 因没有后台投影 worker，`wait_projected()` 等待的是当前 persona 的事件 ID 到达目标；显式未来 event_id 仍保持旧 `EventStore` 的等待/超时契约。
- 每个新事件按当前 persona 的 `MAX(event_id) + 1` 分配连续 event_id；不同 persona 的 event_id 序列互不影响。
- 同批和已有库内的 `idempotency_key` 在同一 persona 内返回已有 event_id，不重复写入；不同 persona 的幂等键空间隔离。
- 多实例并发时，实例内锁保证同一对象顺序写入；跨实例依赖 SQLite 主键/唯一约束，并在冲突后重查以避免同一 persona 重复 event_id 或重复幂等事件。
- `payload_json`、`payload_hash`、`schema_version` 使用旧事件归一化逻辑生成并原样写入，不在 diana 仓储层重写 payload。

读取语义：

- 所有读取只返回当前 persona 的事件。
- 返回 dict 与旧 `_row_to_event()` 一致，包含解析后的 `payload` 以及原始 `payload_json` / `payload_hash`。
- `limit` clamp、`after_event_id` / `before_event_id` 游标、`order` 校验、会话最近页升序返回、类型分页语义与旧 `EventStore` 保持一致。

状态语义：

- `stats()` 中 `last_appended_event_id` 与 `last_projected_event_id` 均为当前 persona 最大 event_id。
- `projection_lag=0`、`pending_count=0`、`projection_error_count=0`、`last_projection_error=None`、`last_projection_error_event_id=None`、`projection_running=False`。
- `closed` 反映 `shutdown()` / `close()` 是否已调用。

### archive_messages

复制现 `memory/archive_sqlite.py` 的 `archive_messages` 字段，并新增 `persona_id`。

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| rowid | INTEGER | NOT NULL |
| archive_id | TEXT | NOT NULL |
| timestamp | TEXT |  |
| timestamp_unix | INTEGER |  |
| date_key | TEXT |  |
| month_key | TEXT |  |
| conversation_id | TEXT |  |
| conversation_type | TEXT |  |
| target_id | TEXT |  |
| sender_id | TEXT |  |
| sender_name | TEXT |  |
| sender_role | TEXT |  |
| direction | TEXT |  |
| message_kind | TEXT |  |
| content | TEXT |  |
| content_search | TEXT |  |
| original_msg_id | TEXT |  |
| reply_to_msg_id | TEXT |  |
| metadata_json | TEXT |  |
| record_json | TEXT |  |
| created_at | TEXT |  |

约束：`PRIMARY KEY(persona_id, rowid)`；`UNIQUE(persona_id, archive_id)`。

索引：

- `idx_archive_time(persona_id, timestamp_unix)`
- `idx_archive_conversation_time(persona_id, conversation_id, timestamp_unix)`
- `idx_archive_sender_time(persona_id, sender_id, timestamp_unix)`
- `idx_archive_original_msg(persona_id, original_msg_id)`
- `idx_archive_date(persona_id, date_key)`
- `idx_archive_record_json(record_json)`

### archive_message_media

复制现 `memory/archive_sqlite.py` 的 `archive_message_media` 字段，并新增 `persona_id`。

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL |
| archive_id | TEXT | NOT NULL |
| media_type | TEXT |  |
| workspace_path | TEXT |  |
| original_name | TEXT |  |
| metadata_json | TEXT |  |

约束：`PRIMARY KEY(persona_id, id)`；`FOREIGN KEY(persona_id, archive_id)` 级联到 `archive_messages(persona_id, archive_id)`。

索引：

- `idx_archive_media_archive(persona_id, archive_id)`

#### DianaArchiveStore 接口

`memory.diana_stores.DianaArchiveStore(db, persona_id)` 是 `archive_messages` / `archive_message_media` 的轻量归档仓储，构造参数：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。
- `persona_id`：人格 ID；去除首尾空白后不能为空。归档消息、短 ID、媒体 ID、去重和所有查询都按 persona 隔离。

异步接口与旧 `ArchiveStore` 当前调用面对齐：

- `load(force_reload=False) -> list[dict]`：直接返回 `records()`。
- `append_many(records: list[dict]) -> None`
- `records() -> list[dict]`
- `search(conversation_id=None, keyword=None, time_range=None, limit=20) -> list[dict]`
- `filter_records(query) -> dict[str, Any]`
- `get_by_ids(archive_ids: list[str]) -> list[dict]`
- `context_around(archive_id, before, after) -> list[dict]`
- `rag_records() -> list[dict]`
- `media_records(archive_id=None) -> list[dict[str, Any]]`

写入语义：

- `append_many()` 复用旧 archive normalization/filter helper，只归档真实聊天记录，以及 tool `send_result` 中 `qq_visible is True` 的 outbound 发送结果；runtime、system、internal、未证明已外发的 assistant 文本不入库。
- `record_json` 保存归一化后的完整记录 JSON。同一 persona 内按 `record_json` 去重；不同 persona 的去重空间互不影响。
- 当前 persona 的 `rowid` 从 `1` 连续递增，`archive_id` 使用旧短 ID 形态 `a1`、`a2` 等；不同 persona 独立分配。
- 当前 persona 的 `archive_message_media.id` 从 `1` 连续递增，外键写入 `(persona_id, archive_id)`。

读取语义：

- `records()`、`search()`、`filter_records()`、`get_by_ids()`、`context_around()`、`rag_records()`、`media_records()` 都只返回当前 persona 的真实聊天数据或媒体。
- `filter_records()` 复用旧 `_filter_sql_plan()`，但 diana 查询会在主 SQL 和 fallback SQL 的基底强制加 `persona_id = ?`；Python residual filter 只接收当前 persona 已筛出的行。
- `context_around()` 只在当前 persona 且同一 `conversation_id` 中取上下文。
- `rag_records()` 返回真实聊天记录，并用 `content_search` 作为 RAG 文本内容，与旧 SQLite 归档行为一致。

### usage_records

抽取 `model_usage.jsonl` 的 token 和常用维度列，同时保留完整 JSON。

| 列 | 类型 | 约束 |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| persona_id | TEXT |  |
| ts | REAL |  |
| provider | TEXT |  |
| model | TEXT |  |
| agent | TEXT |  |
| operation | TEXT |  |
| prompt_tokens | INTEGER | NOT NULL DEFAULT 0 |
| completion_tokens | INTEGER | NOT NULL DEFAULT 0 |
| reasoning_tokens | INTEGER | NOT NULL DEFAULT 0 |
| cached_tokens | INTEGER | NOT NULL DEFAULT 0 |
| cache_creation_tokens | INTEGER | NOT NULL DEFAULT 0 |
| total_tokens | INTEGER | NOT NULL DEFAULT 0 |
| record_json | TEXT | NOT NULL |
| created_at | TEXT | NOT NULL DEFAULT current UTC text |

索引：

- `idx_usage_ts(ts)`
- `idx_usage_provider_model(provider, model)`
- `idx_usage_agent_operation(agent, operation)`
- `idx_usage_persona_ts(persona_id, ts)`

#### DianaUsageStatsStore 接口

`memory.diana_stores.DianaUsageStatsStore(db, persona_id=None)` 是 `usage_records` 的轻量仓储，构造参数：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。
- `persona_id`：可选人格 ID。`None` 或空白字符串表示全局 usage，写库时 `persona_id` 为 `NULL`；非空值去除首尾空白后保存。

接口与 `core.usage_stats.UsageStatsStore` 对齐：

- `load() -> None`：从 `usage_records` 加载记录到内存列表。构造时指定 persona 时只加载该 persona；未指定 persona 时加载全部记录。
- `record(usage, *, provider="", model="", agent="", operation="", extra=None) -> None`：记录一次模型 usage。
- `summarize(range_name="today") -> UsageSummary`：基于内存列表聚合 `today`、`7d`、`30d` 或 `all`。
- `count`：返回当前内存列表长度。

写入语义：

- 当 `usage.total_tokens`、`usage.prompt_tokens`、`usage.completion_tokens` 全部小于等于 0 时，与旧 `UsageStatsStore` 一致直接返回，不写库也不改内存。
- 每条记录构造完整 record 对象：`ts=time.time()`、`provider`、`model`、`agent`、`operation`、`prompt_tokens`、`completion_tokens`、`reasoning_tokens`、`cached_tokens`、`cache_creation_tokens`、`total_tokens`。`total_tokens` 使用 `usage.total_tokens`，缺省时回退为 `prompt_tokens + completion_tokens`。
- `extra` 有值时合并到完整 record 对象，`kv_*` 等任意诊断字段必须保留。
- `record_json` 保存完整 record 对象，不得只保存抽取字段。
- 同步抽取 `persona_id`、`ts`、`provider`、`model`、`agent`、`operation`、五类 token 与 `total_tokens` 到同名列供后续查询使用。

读取语义：

- `load()` 只从 `record_json` 恢复内存列表；聚合以 `_records` 为准，与旧 JSONL 仓储一致。
- 读取损坏的 `record_json` 或非对象 JSON 时跳过该行并记录 warning；仓储不在读取阶段回写修复数据。
- 所有数据库写入和读取由仓储锁保护，并通过后台线程执行，避免阻塞事件循环。

## Persona state 域

Persona state 域使用 `persona_*` 表名前缀复制现 `mind/db_schema.py` 的字段，并补 `persona_id`。原 `important_memories` 表名已被新重要记忆域使用，因此 persona.db 的旧聚合重要记忆表命名为 `persona_important_state_legacy`。

#### DianaPersonaDB 接口

`memory.diana_stores.DianaPersonaDB(db, persona_id)` 是 `diana.db` 中 `persona_*` legacy domains 的轻量仓储，面向 `mind.db.PersonaDB` 当前公开调用面实现。构造参数：

- `db`：`DianaDB` 实例、数据库路径字符串或 `Path`。
- `persona_id`：人格 ID；去除首尾空白后不能为空，空值抛出 `ValueError("persona_id must not be empty")`。

接口覆盖旧 `PersonaDB` 的 state、state log、update audit、effect、todo、cue、profile、inner monologue、daily trajectory、persona arc、sleep/eat record 与旧聚合 important memory 读写方法。`load()` 只确保 diana schema 可用，并在 `persona_schema_version_legacy` 为当前 persona 写入旧 persona.db schema version；不接 runtime，不导入旧数据，不修改 diana schema version。

旧 `persona.db` 导入映射：

- `schema_version` -> `persona_schema_version_legacy`，补 `persona_id`，`id` 固定为 `1`。
- `persona_state` -> `persona_state`。
- `persona_state_log` -> `persona_state_log`。
- `persona_update_audits` -> `persona_update_audits`。
- `effects` -> `persona_effects`。
- `todos` -> `persona_todos`。
- `cues` -> `persona_cues`。
- `inner_monologues` -> `persona_inner_monologues`。
- `user_profiles` -> `persona_user_profiles`。
- `important_memories` -> `persona_important_state_legacy`，并将其中 `memories_json` 的 list 项作为权威来源合并进新 `important_memories`；`important.json` 中同 ID 或同 fallback ID 的重复项会被跳过。
- `daily_trajectories` -> `persona_daily_trajectories`。
- `persona_arc` -> `persona_arc`。
- `sleep_records` -> `persona_sleep_records`。
- `eat_records` -> `persona_eat_records`。

写入语义：

- 所有 `SELECT`、`INSERT`、`UPDATE`、`DELETE` 必须按当前 `persona_id` 隔离；同一 `diana.db` 中不同 persona 的记录、删除、过期处理和最近记录查询互不影响。
- `persona_state`、`persona_important_state_legacy`、`persona_schema_version_legacy` 是每 persona 单行旧聚合表，主键为 `(persona_id, id=1)`。
- 旧 `AUTOINCREMENT` 表在 diana schema 中改为 `(persona_id, id)` 复合主键；`persona_state_log`、`persona_update_audits`、`persona_inner_monologues`、`persona_daily_trajectories`、`persona_arc`、`persona_eat_records` 的 id 在 `BEGIN IMMEDIATE` 事务内按当前 persona 的 `MAX(id)+1` 分配。
- effect、todo、cue、profile、sleep record 使用旧记录 ID 与 `persona_id` 组成复合主键；todo partial update、sleep/eat update、expire/missed 行为与旧 `PersonaDB` 对齐。
- JSON 保真列保存完整记录，读取时继续复用 `mind.db_records` 的 JSON、时间解析、过期判断、排序、ID 归一化和 mind dataclass 适配 helper。

读取语义：

- state/effect/todo/cue/profile 等返回值通过 `mind.db_records` 适配为 `mind` dataclass；旧 recent raw rows 仍返回 dict。
- update audit 支持 `limit`、`conversation_id`、`user_id` 筛选，且筛选只在当前 persona 内生效。
- 旧聚合 important memory 使用 `persona_important_state_legacy.memories_json`，`read_important(default=None)`、`write_important(data)`、`important_count()` 与旧 `PersonaDB` 行为一致。

### persona_schema_version_legacy

复制旧 `schema_version`，按 persona 记录源 persona.db 版本。

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL CHECK (id = 1) |
| version | INTEGER | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

### persona_state

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL CHECK (id = 1) |
| state_json | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

### persona_state_log

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL |
| state_json | TEXT | NOT NULL |
| created_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

### persona_update_audits

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL |
| audit_json | TEXT | NOT NULL |
| trigger | TEXT |  |
| conversation_id | TEXT |  |
| user_id | TEXT |  |
| created_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

索引：

- `idx_persona_update_audits_conversation(persona_id, conversation_id)`
- `idx_persona_update_audits_user(persona_id, user_id)`

### persona_effects

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| effect_id | TEXT | NOT NULL |
| effect_json | TEXT | NOT NULL |
| expires_at | TEXT |  |
| active | INTEGER | NOT NULL DEFAULT 1 |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, effect_id)`。

索引：`idx_persona_effects_expires(persona_id, expires_at)`。

### persona_todos

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| todo_id | TEXT | NOT NULL |
| todo_json | TEXT | NOT NULL |
| completed | INTEGER | NOT NULL DEFAULT 0 |
| expires_at | TEXT |  |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, todo_id)`。

索引：`idx_persona_todos_completed(persona_id, completed)`。

### persona_cues

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| cue_id | TEXT | NOT NULL |
| cue_json | TEXT | NOT NULL |
| expires_at | TEXT |  |
| active | INTEGER | NOT NULL DEFAULT 1 |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, cue_id)`。

索引：`idx_persona_cues_expires(persona_id, expires_at)`。

### persona_inner_monologues

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL |
| monologue_json | TEXT | NOT NULL |
| created_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

索引：`idx_persona_monologues_recent(persona_id, id)`。

### persona_user_profiles

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| user_id | TEXT | NOT NULL |
| profile_json | TEXT | NOT NULL |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, user_id)`。

### persona_important_state_legacy

复制旧 persona.db 的 `important_memories` 聚合表。

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL CHECK (id = 1) |
| memories_json | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

### persona_daily_trajectories

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL |
| trajectory_json | TEXT | NOT NULL |
| created_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

索引：`idx_persona_trajectories_recent(persona_id, id)`。

### persona_arc

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL |
| event_json | TEXT | NOT NULL |
| created_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

### persona_sleep_records

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| record_id | TEXT | NOT NULL |
| record_json | TEXT | NOT NULL |
| started_at | TEXT |  |
| ended_at | TEXT |  |
| created_at | TEXT | NOT NULL |
| updated_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, record_id)`。

索引：`idx_persona_sleep_started(persona_id, started_at)`。

### persona_eat_records

| 列 | 类型 | 约束 |
|---|---|---|
| persona_id | TEXT | NOT NULL |
| id | INTEGER | NOT NULL |
| record_id | TEXT |  |
| record_json | TEXT | NOT NULL |
| ended_at | TEXT |  |
| status | TEXT |  |
| created_at | TEXT | NOT NULL |

约束：`PRIMARY KEY(persona_id, id)`。

索引：`idx_persona_eat_record_id(persona_id, record_id)`。

## 当前未实现

- 未改 `mind/db.py` 或 `mind/db_schema.py`。
- 未实现统一仓储抽象。
- 未处理向量库/RAG 搬迁；向量库按阶段计划保持独立文件。
