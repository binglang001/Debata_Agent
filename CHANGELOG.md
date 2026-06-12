# Changelog

本文件记录 Debata 所有重要变更，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

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
