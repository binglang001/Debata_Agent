# Changelog

本文件记录 Debata 所有重要变更，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [v0.9.0-alpha] — 2026-07-04

### Added
- PersonaAgent 新增对话后更新、周期维护、睡眠开始、进食开始、睡醒/进食结束结算和启动离线对账链路，让人格状态可以围绕真实运行事件持续推进。
- PersonaContextView / PersonaEpisodeBuffer 落地，向 PersonaAgent 提供事件、聊天现场、当前状态、对象画像、画像审计、短期影响、线索、待办和相关长期记忆组成的结构化上下文。
- episode buffer 增加明确边界：会话切换、闲置超时、进入或离开吃饭/睡觉/崩溃动作、片段数上限和字符数上限都会打开新 episode，避免上下文无限追加。
- 人格更新审计扩展到状态、短期影响、待办、线索、用户画像和关系更新，记录触发来源、会话、用户、前后快照和变更摘要。
- mind SQLite schema 升级到 version 2，补齐 persona_update_audits、人格状态、状态日志、用户画像、短期影响、待办、线索、睡眠记录、进食记录、轨迹和长期重要记忆等存储。
- 睡醒/进食结束时支持模型恢复评估；模型失败或离线对账场景使用公式兜底，并在日志中记录 recovery_source。
- 周期维护会清理过期短期项、线索和待办，过期待办会标记为 missed，并触发日常状态整理。
- 待办、线索和短期影响支持基于 ID 的创建、更新、关闭/删除；未知 ID 的更新会被审计为丢弃，避免错误写入。
- 用户画像和关系更新接入 PersonaAgent，支持好感、摘要、特征、互动次数和最近互动时间的持续维护。
- 人格后台页新增运行概况、实时状态、背景动向、状态日志、短期影响、待办/线索、用户画像、睡眠/进食和整理轨迹查看。
- 设置页新增人格管理开关、当前人格年龄覆盖、精力/饱腹模式、兜底恢复/衰减参数，以及 Persona / Social / Subconscious Agent 的后台配置入口。

### Changed
- 项目命名与数据存储从 Diana 收敛为 Debata，运行时数据库改为 `memory/<persona>/<persona>.db`。
- 人格上下文从零散状态文本扩展为结构化上下文视图，聊天现场改为有边界 episode，不再依赖无限追加的片段。
- 年龄相关描述以年龄档位、年龄提示和当前人格年龄覆盖为准，不再表达为自动年龄增长。
- 睡眠和进食恢复从“动作开始即同步恢复”调整为动作结束或离线结算时评估，并保留公式兜底来源。

### Fixed
- 本地 RAG 缺少 `sentence-transformers` 时改为启动后台依赖安装并提示重启生效，避免启动流程直接失效。
- GUI 模式启用 qasync timer 多路复用，并收敛 wakeup、NapCat 和 proactive 后台循环的重复启动风险，降低 Windows 原生 timer/句柄耗尽概率。
- 修复 PersonaAgent 相关 JSON 输出兼容性问题：复数字段稳定按数组解析，同时兼容 profile、relationship、effect、todo、cue 等单条旧字段。
- 修复短期影响、待办、线索更新时缺少 ID、引用未知 ID、空内容和重复待办导致的数据漂移问题。
- 修复待办状态兼容性问题，统一 complete / done / cancelled / deleted / missed 等状态归一和关闭判断。
- 修复进食记录缺少 record_id 的兼容迁移问题，为旧记录补齐可更新 ID。
- 收敛画像、关系、短期项、审计记录与结构化上下文之间的数据一致性问题。

## [v0.8.5-alpha] — 2026-06-11

### Added
- Linux/CLI 启动配置完善，补齐无头环境启动、路径与密钥处理链路
- 上下文压缩与工作窗口机制落地，提升长对话稳定性和上下文预算控制
- 聊天归档与实时聊天能力增强，私聊和群聊均可进行本地归档总结
- QQ/NapCat 适配与发送工具扩展，覆盖消息发送、回执处理和平台侧历史摘要
- RAG、长期记忆、工具预算与仪表盘能力整合，支持记忆注入、预算观测和图形化管理

### Changed
- 发布状态整理为 v0.8.5-alpha
- 启动配置、上下文管理、聊天工具、记忆系统和仪表盘相关变更按当前发布面重新归类

### Fixed
- 修正 Linux/CLI、NapCat 连接、发送回执、上下文压缩和仪表盘启动链路中的近期问题

## [v0.2.0-alpha] — 2026-05-24

### Added
- 289 项集成测试（pytest + pytest-asyncio）
- KV 缓存命中率实测验证
- NapCat 心跳/重连配置项（`ping_interval_seconds` 等）

### Changed
- `WhitelistConfig.mode` 值 `"all"` 改为 `"open"`
- `ProtocolType` 收窄为 `Literal["openai_compat", "anthropic"]`

### Removed
- V1 兼容代码全部清理（migrate / detect_legacy / `TOOL_USE_PROTOCOL` 别名等 11 处）

## [v0.1.0-alpha] — 2026-05-24

### Added
- V2 全栈重构：Core / Providers / Agents / Adapters / Tools / Memory / Features / AppConfig 七大模块
- NapCat OneBot V11 适配器（WebSocket 直连）
- 多 LLM 提供商：OpenAI 兼容协议 + Anthropic 原生协议
- 工具系统（消息/记忆/平台/控制/功能），Pydantic schema 自动派生 OpenAI tool 格式
- AES-256-GCM + RSA-2048 + keyring 三级加密配置管理
- 消息管线：合并窗口 + 中断检测 + 工具循环 + 总结
- 主动思考定时循环（proactive_loop）
- 人格系统（persona prompt + persona_gen_agent）
- 滑动窗口速率限制 + 好友/群申请验证流程
