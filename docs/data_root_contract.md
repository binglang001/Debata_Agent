# 数据根契约（阶段 0）

阶段 0 的目标是先稳定启动路径和目录契约：新安装默认使用系统数据根，已有源码目录下的 `data/` 会在启动早期复制到当前实例目录，避免旧 `config.yaml` 尚未入根就被误判为首次启动。

## 解析顺序

`AppPaths` 按以下顺序决定数据根：

1. 显式传入 `data_root` 时使用该目录。
2. 设置 `DEBATA_DATA_ROOT` 时使用环境变量指向的目录。
3. 项目根存在 `.debata-dev-data-root` 时进入开发标记模式。
4. 显式传入 `config_file` 时保留旧兼容布局，`DATA_DIR` 仍为 `project_root/data`。
5. 显式传入非默认项目根时保留测试/开发兼容布局，`DATA_DIR` 仍为 `project_root/data`。
6. 其它正式默认启动使用系统用户数据目录下的 `Debata_Agent`。

阶段 0 初始化必须发生在读取 `CONFIG_FILE`、初始化密钥、加载配置之前。正式入口和 `Runtime.start()` 都会调用同一个启动 helper；该 helper 幂等，第二次启动只读取迁移记录并跳过重复复制。

## 环境变量

- `DEBATA_DATA_ROOT`：覆盖数据根目录。设置后实例数据位于 `<DEBATA_DATA_ROOT>/instances/<实例名>/`。
- `DEBATA_MODELS_DIR`：由 `Runtime.start()` 根据当前 `paths.MODELS_DIR` 设置默认值，供模型相关组件读取。

## 开发标记

项目根的 `.debata-dev-data-root` 用于开发期覆盖：

- 文件为空：使用旧布局 `project_root/data`，不迁移到外部实例目录。
- 文件内容为相对路径：相对项目根解析为数据根。
- 文件内容为绝对路径：直接作为数据根。

## 实例目录

阶段 0 的实例名默认为 `default`。新布局为：

```text
<data_root>/
  root.json
  backups/
  instances/
    <instance_name>/
      instance.json
      config.yaml
      secrets.enc
      secrets.meta
      memory/
      logs/
      emoji/
      models/
      workspace/
```

`DATA_DIR` 指向当前实例目录；旧兼容模式下仍指向 `project_root/data`。

## root.json 字段

`root.json` 位于数据根，阶段 0 写入以下字段：

- `schema`：当前为 `diana.data_root.v0`。
- `app_version`：执行初始化时的应用版本。
- `created_at`：首次创建清单的 UTC 时间戳。
- `updated_at`：最近一次写入迁移记录的 UTC 时间戳。
- `instances`：实例列表，元素包含 `name` 和 `path`。
- `migrations`：数据根级迁移记录；阶段 0 使用 `project_data_to_instance_dir.v0`。

## instance.json 字段

`instance.json` 位于实例目录，阶段 0 写入以下字段：

- `schema`：当前为 `diana.data_instance.v0`。
- `app_version`：执行初始化时的应用版本。
- `created_at`：首次创建清单的 UTC 时间戳。
- `updated_at`：最近一次写入迁移记录的 UTC 时间戳。
- `data_dir`：当前实例目录。
- `migrations`：实例级迁移记录；阶段 0 与 `root.json` 记录同一个迁移。

迁移记录包含应用版本、完成时间、旧 `project_root/data`、目标实例目录、备份目录和复制时跳过的相对路径。

## 配置字段兼容

阶段 0 的配置 schema 会容忍未知字段，避免旧配置或前置阶段写入的字段导致启动失败。未知字段不会在 `save_config` 写回时保留；阶段 1 再处理保留注释、未知字段和配置迁移链。

## 阶段 0 回滚方式

阶段 0 不删除旧 `project_root/data`。如果需要回滚：

1. 退出应用。
2. 清除 `DEBATA_DATA_ROOT`，并移除或清空 `.debata-dev-data-root` 中指向外部数据根的内容。
3. 使用 `--config` 指向旧配置，或在开发/测试场景显式传入项目根以恢复旧兼容布局。
4. 如需恢复新实例中被复制前的状态，可从 `<data_root>/backups/data-<timestamp>/` 取回旧数据副本。

## 当前不做的事项

阶段 0 只完成数据根入根和启动编排，不实现以下内容：

- 配置历史键迁移链。
- `diana.db` 的结构迁移或集中化。
- 人格数据化。
- 多实例之间的模型共享策略。
