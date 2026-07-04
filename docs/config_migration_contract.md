# 配置迁移契约

## 当前版本

- 当前配置版本：`CURRENT_CONFIG_VERSION = 2`
- 稳定 API：`app_config.CURRENT_CONFIG_VERSION`、`app_config.migrate_config`、`app_config.ConfigMigrationReport`
- `load_config(paths, set_global=True)` 的公共返回值始终是 `RootConfig`，不会返回原始 `dict`。

## 迁移链登记表

| 迁移 ID | 来源版本 | 目标版本 | 入口 |
| --- | --- | --- | --- |
| `config.v1_to_v2` | `1` | `2` | `migrate_config(raw)` |

`migrate_config` 只处理纯 `dict` 数据，不读写文件。文件备份、写回和日志由 loader 负责。

`config.v1_to_v2` 仍然表示版本链从 v1 升级到 v2 的迁移步骤；4.5 历史键规范化不是只依赖版本号的步骤，会在所有非未来版本配置上执行。因此 `version: 2` 但仍含 4.5 旧键的配置也会被规范化、报告 `changed=True`，并由 loader 在校验成功后备份和写回。`version: 2` 且不含旧键的配置不会因此产生假变更。

## 历史键映射表

| 历史路径 | 当前路径 | 处理 |
| --- | --- | --- |
| `behavior.merge_window` | `behavior.merge_window_seconds` | 重命名 |
| `behavior.recall_merge_window` | `behavior.recall_merge_window_seconds` | 重命名 |
| `behavior.greeting_interval` | `behavior.proactive_think_interval_seconds` | 重命名 |
| `behavior.summarize.chat_history_count` | `behavior.default_history_fetch_count` | 重命名 |
| `behavior.rate_limit.window` | `behavior.rate_limit.window_seconds` | 重命名 |
| `providers.<id>.timeout` | `providers.<id>.timeout_seconds` | 重命名 |
| `agents.<id>.first_token_timeout` | `agents.<id>.first_token_timeout_seconds` | 重命名 |
| `adapters.<id>.whitelist.mode = all` | `open` | 值规范化 |
| 历史 OpenAI 兼容 provider protocol | `openai_compat` | 值规范化 |

## 废弃字段

`behavior.typing.max_delay` 已废弃并在 v1 -> v2 迁移中删除。当前 schema 没有等价字段；发送延迟由模型逐条填写 `target.delay`，旧值不会写回到不存在的 `max_delay_seconds`。迁移报告会把该路径记录为 `removed`，并附带 warning。

## 高版本策略

当配置版本高于 `CURRENT_CONFIG_VERSION` 时：

- `migrate_config` 不修改原始数据，返回 `future_version=True` 的报告。
- loader 记录 warning，不写回、不备份。
- loader 不用当前 `RootConfig` 强行校验完整未来配置；未知 extra 字段按当前 schema 的 `ignore` 规则不进入当前对象，触发校验失败的未来叶子值只在白名单内才会从内存副本删除，并用剩余兼容子集构造当前可用的 `RootConfig`。
- 被当前 schema 忽略或从内存副本删除的未来字段只影响本次运行中的当前对象，不会改动磁盘文件。
- 未来版本配置对象禁止通过 `save_config` 保存；调用方若把 `cfg.version > CURRENT_CONFIG_VERSION` 的对象传给 `save_config`，loader 会抛出配置错误，避免用当前 schema 子集覆盖未来配置文件并丢字段。

未来裁剪采用白名单，不按 Pydantic 错误类型泛化处理。当前只允许以下叶子值在内存副本中删除：

- `app.theme` 的未知 literal 值。删除后回落到当前默认主题 `auto`。
- `providers.<id>.protocol` 的未知 literal 值，但仅限该 provider 同时配置了 `preset`。preset 模式下协议由 preset/运行时解析，当前 schema 子集仍可用。

其它未来 literal/enum 值一律不自动降级。尤其是模式、类型、鉴权、路由类字段，例如 `adapters.<id>.type`、`adapters.<id>.mode`、`adapters.<id>.whitelist.mode`、`features.*.type`，遇到当前版本不认识的值时必须抛出 `ConfigError`，不写回、不备份，避免回落到当前 schema 默认值后静默错误启动。模型级错误、引用错误、缺失必要字段、字段类型错误等也不会自动删除整段对象。

## 备份与写回

自动迁移写回和 `save_config(..., backup=True)` 使用同一保存路径：

- 备份目录：`<DATA_DIR>/config_backups/`
- 文件名：`config-<timestamp>-v<oldversion>.yaml`
- 若文件名碰撞，追加数字 suffix，避免覆盖。
- 写回使用临时文件加原子替换。

自动迁移写回只在 `RootConfig.model_validate(migrated_raw)` 成功后发生；schema 校验失败不会写回，也不会创建迁移备份。

## 注释保留限制

当前不引入 YAML round-trip 依赖。写回时保留原文件中的纯注释行和空行，并把它们提升到文件头部。段内纯注释会保留文本，但不保证原位置；inline comment 不作为保留契约。

写回后的 YAML 内容以当前 schema dump 为准，不会为了保留注释而保留旧字段或生成无效 YAML。
