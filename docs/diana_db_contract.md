# diana.db v1 契约

本文档定义阶段 2 的 `diana.db` 基础契约：单库位置、版本记录、备份策略、v1 空表结构与索引。当前实现包含建库和版本/备份能力，以及 `DianaHistoryStore` 对 `history_records` 的最小读写；不接 runtime，不导入旧数据，不实现 important/summary/usage/runtime 仓储。

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
