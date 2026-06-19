# 架构总览

Debata_Agent 各模块职责与依赖关系。

---

## 设计原则

1. **依赖倒置**——所有跨模块边界用抽象接口（IAdapter / IProvider / ITool 等）
2. **依赖注入**——组件不持有全局状态，都通过构造器传入
3. **单一职责**——每个模块只做一件事
4. **可测试**——所有组件能用 fake 替身单独测试

---

## 数据流

```
用户消息
  ↓
IAdapter（NapCat WS / Discord / ...）
  ↓
EventBus（按事件类型分发）
  ↓ ↓ ↓
MessagePipeline / RecallHandler / RequestHandler
  ↓
（合并窗口 / 中断检测）
  ↓
ChatAgent.run(messages, tools, executor)
  ├─ IProvider.chat_completion() → 多轮工具循环
  └─ ToolRegistry.executor → 工具执行（按配置启用）
       ├─ messaging tools → MessagePipeline 异步发送队列
       ├─ memory tools → ImportantMemoryManager（scope / pinned 元数据）
       ├─ platform tools → IAdapter.list_friends / get_user_info / ...
       └─ feature tools → VisionService / WebSearchService / ...
  ↓
MessagePipeline.SendManager（真实发送 + 中断检测 + send_receipt）
  ↓
IAdapter.send_text() → 用户收到消息
```

---

## 模块矩阵

| 模块 | 依赖 | 暴露 | 测试覆盖 |
|------|------|------|---------|
| `app_config` | - | `RootConfig`, `AppPaths`, `SecretsManager` | ✅ |
| `adapters` | `app_config` | `IAdapter`, `NapCatAdapter`, `Target`, `IncomingMessage` | ✅ |
| `providers` | - | `IProvider`, `build_provider()`, 13 个预设 | ✅ |
| `memory` | - | `HistoryManager`, `ImportantMemoryManager` | ✅ |
| `agents` | `providers`, `memory`, `app_config` | `ChatAgent`, `Persona`, `build_messages()`, prompts | ✅ |
| `tools` | `adapters`, `memory`, `providers`, `app_config` | `ToolRegistry`, `ToolContext`, `build_default_registry()` | ✅ |
| `core` | 上面全部 | `Runtime`, `MessagePipeline`, `EventBus` | ✅ |
| `features` | `providers` | `VisionService`, `WebSearchService`, `WeatherService`, `IEmbeddingService`, `ITTSService` | ✅（Vision/WebSearch/Weather/Embedding 已实装；TTS 支持 Edge TTS / iFlyTek API / 本地 VoxCPM2 三种实现；QQ 语音转写走 NapCat 内置能力） |
| `plugins` | `features` | `PluginManager`, `PluginMeta`, `PluginStatus` | ✅（VoxCPM2 与本地 embedding 已接入） |
| `ui` | `core`, `app_config` | `WizardWindow`, `DashboardWindow`, `Tray` | ✅ |
| `utils` | - | `parse_raw_cq`, `MetricsProvider`, `get_time` | ✅ |

---

## KV 缓存友好的 system prompt 结构

`agents/context_builder.build_combined_system_prompt()` 把所有稳定区拼成**单一 system 消息**，内部用 XML 分区：

```xml
<core_rules priority="critical">       — 永不变
<persona priority="high">              — 人格切换才变
<human_chat_patterns priority="high">  — 永不变（真人聊天形态）
<tool_use_protocol priority="high">    — 工具集 + memory_mode 变化才变
<conversation_protocol priority="high">— 永不变
<self_reflection priority="medium">    — 永不变
<qq_format priority="reference">       — 永不变
<long_term_memory priority="medium">   — 重要记忆变化时
```

**稳定性递减顺序前置**，确保 LLM 的 KV 缓存命中率最大化。每轮变化的 `task_context`（时间、表情包列表、提示等）作为单独的 user 消息追加到 history 末尾，**不破坏前缀**。

详见 [KV 缓存基准](kv_cache_benchmark.md) 和 `agents/context_builder.py` 注释。

---

## Task Contract 重注入

`AgentRunner.run(messages, task_contract=...)` 接受任务合约。每 `cfg.refocus_interval` 轮（默认 5）在 messages 末尾追加焦点提醒，对抗多步任务焦点漂移（76-89% 任务漂移率，研究结论）。

放在 messages 末尾**不破坏前缀缓存**。

---

## 主上下文与滚动摘要

`HistoryManager` 对真实聊天、运行时上下文和工具结果使用永久全量 append；主路径不再按“最近 N 条”裁剪工作历史。主模型调用前，`MessagePipeline` 会按 `behavior.context.max_context_tokens`（或推荐预算）预检装配后的上下文：

1. 未超预算：直接调用主模型。
2. 超过预算：触发滚动摘要，把较早的活跃 history 合入 summary，并推进活跃窗口起点。
3. 首次压缩后仍超预算或压缩失败：按更小的重试目标再次扩大压缩范围。
4. 仍无法满足预算：显式退出本轮并记录错误，而不是静默裁剪历史。

相关配置：

- `behavior.summarize.trigger_at_context_percent`：未显式配置 token 阈值时，按工作上下文预算推导触发点。
- `behavior.summarize.target_after_context_percent`：首次压缩后的活跃窗口目标。
- `behavior.summarize.retry_target_after_context_percent`：首次压缩仍不够时的重试压缩目标。
- `behavior.context.summary_token_budget`：滚动摘要注入主上下文的预算。

`range_start_messages`、`range_end_messages` 以及 `behavior.context` 中旧的“活跃历史保底 / 保留最近 N 条运行时记录”字段已经废弃；主动思考路由器使用独立的 `proactive_router_*` 预算字段，不参与主聊天历史裁剪。

---

## 工具结果创建即定型

工具结果只在刚返回时做一次精简，写入 `history` 后不再回改。这样旧 `tool` record 的字节稳定，避免为了清理长结果而破坏 KV 缓存前缀。

入口：

- 工具内部做语义明确的精简，例如 `describe_image` 保存完整描述、`get_user_info` 丢弃二进制 buffer、`read_file` 分页。
- `tools.result_shrink.shrink_tool_result()` 在 `ToolRegistry` executor 出口做统一兜底，例如搜索结果、合并转发、Python 输出、超长未知工具结果。

相关配置位于 `behavior.context`：

- `tool_result_budgets`：按工具分别配置 inline、artifact 和 hard 上限。
- `tool_result_default_budget_tokens`：未单独配置工具的 inline 默认预算。
- `tool_result_default_hard_cap_tokens`：未单独配置工具的事故兜底上限。

设置页「Token预算」提供对应入口。旧的 `tool_result_soft_limit_tokens`、`tool_result_hard_cap_tokens` 和 `tool_result_soft_overrides` 只为兼容历史配置保留。

---

## 长期记忆双模式

`features.long_term_memory.mode`：

- **`file`**（默认）：AI 通过显式 `save_important_memory` / `update_important_memory` 工具维护重要记忆。零额外向量开销、完全透明。
- **`rag`**：同样保存重要记忆，并为会话历史维护向量索引；上下文注入时按语义检索相关历史。需要 `features.embedding` 启用。

切换模式时，`build_tool_use_protocol(memory_mode)` 会动态注入不同的工具说明，区分文件直写与 RAG 语义检索的使用方式。

重要记忆是一份全局存储，不按会话拆库。每条记忆有两个呈现层元数据：

- `scope`：`global` / `user:{qq}` / `group:{gid}`，只决定当前轮优先注入哪些记忆。
- `pinned`：置顶记忆永远注入，不受当前会话 scope 过滤；普通记忆受 `memory_token_budget` 控制。

`MessagePipeline` 会把当前 `conversation_id` 映射到记忆 scope：`private:123 → user:123`，`group:456 → group:456`。`save_important_memory` 默认使用当前会话 scope，除非工具参数显式指定。

### RAG 装配

`core/runtime.py::_setup_rag` 在 `mode=rag` 时按顺序装：

```
features.embedding.provider → 复用现有 LLM provider 的 base_url
                            → OpenAICompatEmbeddingService(base_url, key, model)
vector/<persona>/rag_memory.sqlite3
                            → SqliteVectorStore（cosine top-k 检索）
RagMemoryService            → 监听 HistoryManager 追加并启动归档/活跃历史 bootstrap
```

向量库是实例级独立文件，不并入 `memory/<persona>/diana.db`；旧 `memory/<persona>/rag_memory.sqlite3` 首次启用 RAG 时会复制到新路径，旧文件保留。
`message_pipeline` 拼上下文时用最后一条用户消息当 query 调 `retrieve_for_query()`：
先按当前 `conversation_id` 过滤候选，再做 cosine top-k，最后与重要记忆上下文合并，省 token 且减少跨群/私聊误召回。

向量算法（`memory/rag_store.cosine_similarity`）固定用余弦相似度；
不做归一化（外面传进来的向量保留原长度，cosine 公式自带归一化）。

---

## 本地归档总结

`recall_history` 和 `summarize_conversation` 读取本地 `ArchiveStore` + 当前活跃 `HistoryManager`：

- `recall_history` 返回匹配原文片段，用于找旧 msg_id、旧约定或精确上下文。
- `summarize_conversation(conversation_id?, range_hint?, goal?, max_tokens?)` 用总结模型整理本地归档，私聊和群聊都可用。

`summarize_chat_history` 是另一个工具：它通过 NapCat/QQ 服务器侧接口拉取指定群的近期群消息，只适合补充本地归档以外的群历史，不支持私聊。

---

## 插件机制

`plugins/` 给「重依赖 / 本地模型」类能力一个统一安装入口。Runtime 启动时扫描 `plugins/{name}/__plugin__.py`，
按 `features.{tts,embedding}.type=local` 决定要不要 build 实例并注入运行时。

```
plugins/{name}/
  __plugin__.py    # PLUGIN_META + build(config) -> service
  voxcpm_impl.py   # 真正的实现（lazy import 重依赖）
data/models/{name}/  # 模型文件，不入仓库
```

主程序只依赖 `features/` 的轻量接口（`ITTSService` / `IEmbeddingService`），
插件实现这些接口即可。完整规格见 [plugins/PLUGIN_SPEC.md](../plugins/PLUGIN_SPEC.md)。

UI 入口在仪表盘「模型管理」页：列表 + 详情 + 安装指引。实际启用与参数配置在设置页对应 feature 区域完成。

工具系统挂钩：TTS 启用时 `tools/feature_tools.send_voice_message` 才生效。
ASR 不出现在工具里。QQ/NapCat 渠道使用 NapCat 内置 `fetch_ptt_text`。

---

## 生命周期

```
main.py → Runtime.start()
  1. AppPaths + SecretsManager
  2. load_config()
  3. load_persona()
  4. HistoryManager + ImportantMemoryManager
  5. build providers (各 LLM)
  6. ChatAgent / ProactiveRouterAgent / SummaryAgent
  7. features 服务（vision/web_search/weather, 按配置）
  7.5 RAG（embedding + rag_store）若 mode=rag
  7.6 PluginManager.scan() + build TTS/Embedding（若 type=local）
  8. NapCatAdapter
  9. ToolRegistry（按 features 启用）
  10. State（UsageStats / ModelActivity / ProviderHealth）
  11. WakeupScheduler / PendingRequestStore / RateLimiter
  12. MessagePipeline（拼装上面全部）
  13. RecallHandler / RequestHandler
  14. ProactiveLoop
  15. EventBus（订阅 pipeline + handlers）
  16. adapter.start() / proactive_loop.start()

Runtime.wait_until_stop() → 等 SIGINT/SIGTERM

Runtime.shutdown() → 反序关闭
```

详见 `core/runtime.py`。

---

## 测试策略

- **单元 / 集成测试**（`tests/`）：每模块独立测试，同时覆盖完整 message pipeline、工具执行、发送队列、RAG、UI 配置等链路
- **Live 测试**（`tests/test_kv_cache_real.py`）：需真实 API 密钥，标记 `@pytest.mark.live`。默认不跑
- **KV 缓存命中率实测**：验证稳定 system 前缀、tools 参数、变化 task_context 等场景的缓存命中情况

跑测试：

```bash
pip install -e ".[dev,gui]"
venv/Scripts/python -m pytest tests/ -q --ignore=tests/test_kv_cache_real.py
```

---

## 性能优化点

- **uvloop**（Linux/Mac）替代默认 asyncio
- **orjson** 替代 json
- **aiofiles** 替代同步文件 IO
- **JSONL 增量追加**（HistoryManager）替代每次重写整个 JSON
- **KV 缓存前缀稳定**（context_builder）
- **WebSocket 长连接 + 心跳**（NapCatAdapter）

详见 [KV 缓存基准](kv_cache_benchmark.md)。
