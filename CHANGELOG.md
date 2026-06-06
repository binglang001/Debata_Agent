# Changelog

本文件记录 Debata 所有重要变更，格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased] — v0.3.0-alpha（开发中）

### Added
- Phase 2 GUI：无边框窗口 + 图形化配置向导 + 7 页仪表盘（总览/会话/记忆/日志/人格/模型管理/设置）
- RAG 长期记忆（embedding 服务接口 + OpenAI 兼容实现 + scope/pinned 注入选择）
- `summarize_conversation` 本地归档总结工具，私聊和群聊都可用
- 图像识别、联网搜索、天气查询三大 feature 实装
- VoxCPM2 本地 TTS 与本地 embedding 插件接入，模型管理页提供安装指引
- 设置页即时保存，无需重启生效
- 密钥解密失败时自动引导用户重新输入（不崩溃）

### Changed
- 配置 schema 字段重命名统一（`timeout` → `timeout_seconds` 等）
- DeepSeek 默认模型切换为 `deepseek-v4-flash`
- Provider import 路径统一为 `from providers import OpenAICompatProvider, AnthropicProvider`
- 主动思考路由改用 system-only 极简上下文，避免 tool role 污染路由判断
- `summarize_chat_history` 职责收窄为 NapCat/QQ 服务器侧近期群历史总结
- ASR 从项目中移除，QQ 语音转写统一走 NapCat 内置 `fetch_ptt_text`

### Fixed
- NapCat 测试连接逻辑修正（client/server 模式分别处理）
- 向导完成后仪表盘 `runtime.paths` 为 None 的启动顺序问题
- 发送回执 Case B 显著性不足导致模型误说”等回执”的问题
- clean completion 与 interrupted send receipt 的语义区分
- 多轮 UI 打磨：表情包发送语义拆分、居中内容宽度修正、表情包投放区主题色适配、仪表盘对话框拆分与本地主题样式、无边框窗口布局微调

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
