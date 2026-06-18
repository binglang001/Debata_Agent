# diana.db v1 契约

本文档定义阶段 2 的 `diana.db` 基础契约：单库位置、版本记录、备份策略、v1 空表结构与索引。当前实现包含建库和版本/备份能力，以及 `DianaHistoryStore` 对 `history_records`、`DianaImportantStore` 对 `important_memories`、`DianaRollingSummaryStore` 对 `rolling_summary`、`DianaUsageStatsStore` 对 `usage_records` 的最小读写；不接 runtime，不导入旧数据，不实现 runtime 仓储。

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

- 未接 `core/runtime.py`。
- 未改现有 memory/history/important/event/archive manager。
- 未改 `mind/db.py` 或 `mind/db_schema.py`。
- 未实现统一仓储抽象。
- 未实现旧 `history.jsonl`、`important.json`、`rolling_summary.json`、`events.sqlite3`、`archive.sqlite3`、`model_usage.jsonl`、`persona.db` 的导入器。
- 未处理向量库搬迁；向量库按阶段计划保持独立文件。
