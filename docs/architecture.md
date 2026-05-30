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
（合并窗口 / 中断检测 / 关键词强制保存）
  ↓
ChatAgent.run(messages, tools, executor)
  ├─ IProvider.chat_completion() → 多轮工具循环
  └─ ToolRegistry.executor → 工具执行（17 个工具）
       ├─ messaging tools → ctx.collected
       ├─ memory tools → ImportantMemoryManager
       ├─ platform tools → IAdapter.list_friends / get_user_info / ...
       └─ feature tools → IVisionService / IWebSearchService / ...
  ↓
MessagePipeline._execute_collected()（真实发送 + 中断检测）
  ↓
IAdapter.send_text() → 用户收到消息
```

---

## 模块矩阵

| 模块 | 依赖 | 暴露 | 测试覆盖 |
|------|------|------|---------|
| `app_config` | - | `RootConfig`, `AppPaths`, `SecretsManager` | ✅ |
| `adapters` | `app_config` | `IAdapter`, `NapCatAdapter`, `Target`, `IncomingMessage` | ✅ |
| `providers` | - | `IProvider`, `build_provider()`, 10 个预设 | ✅ |
| `memory` | - | `HistoryManager`, `ImportantMemoryManager` | ✅ |
| `agents` | `providers`, `memory`, `app_config` | `ChatAgent`, `Persona`, `build_messages()`, prompts | ✅ |
| `tools` | `adapters`, `memory`, `providers`, `app_config` | `ToolRegistry`, `ToolContext`, `build_default_registry()` | ✅ |
| `core` | 上面全部 | `Runtime`, `MessagePipeline`, `EventBus` | ✅ |
| `features` | `providers` | `IVisionService`, `IWebSearchService`, `IWeatherService`, `IEmbeddingService`, `IASRService`, `ITTSService` | ✅（Vision/WebSearch/Weather/Embedding 已实装；ASR/TTS 接口已定义，实装走插件） |
| `plugins` | `features` | `PluginManager`, `PluginMeta`, `PluginStatus` | ✅（机制就绪；Whisper/VoxCPM2 待 DS 装实） |
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

**稳定性递减顺序前置**，确保 LLM 的 KV 缓存命中率最大化。每轮变化的 `task_context`（时间、表情包列表、提示等）作为单独的 system 消息追加到 history 末尾，**不破坏前缀**。

详见 [docs/ui_style_guide.md](ui_style_guide.md) 和 `agents/context_builder.py` 注释。

---

## Task Contract 重注入

`AgentRunner.run(messages, task_contract=...)` 接受任务合约。每 `cfg.refocus_interval` 轮（默认 5）在 messages 末尾追加焦点提醒，对抗多步任务焦点漂移（76-89% 任务漂移率，研究结论）。

放在 messages 末尾**不破坏前缀缓存**。

---

## 工具结果创建即定型

工具结果只在刚返回时做一次精简，写入 `history` 后不再回改。这样旧 `tool` record 的字节稳定，避免为了清理长结果而破坏 KV 缓存前缀。

入口：

- 工具内部做语义明确的精简，例如 `describe_image` 保存完整描述、`get_user_info` 丢弃二进制 buffer、`read_file` 分页。
- `tools.result_shrink.shrink_tool_result()` 在 `ToolRegistry` executor 出口做统一兜底，例如搜索结果、合并转发、Python 输出、超长未知工具结果。

相关配置位于 `behavior.context`：

- `tool_result_soft_limit_tokens`：默认 600，超过后走工具特定精简策略。
- `tool_result_hard_cap_tokens`：默认 1500，超过后做中央 head/tail 截断。
- `tool_result_soft_overrides`：按工具名覆盖软阈值，如 `describe_image=900`。

设置页「高级 → 上下文预算」提供对应入口。

---

## 长期记忆双模式

`features.long_term_memory.mode`：

- **`file`**（默认）：AI 主动调 `save_important_memory` + 关键词强制保存（"记住"/"约定"/"我叫"）。零开销、完全透明。
- **`rag`**：被动抽取 + 向量库 + 语义检索。AI 不调主动保存工具。需要 `features.embedding` 启用。

切换模式时，`build_tool_use_protocol(memory_mode)` 会动态注入不同的工具说明，避免在 RAG 模式下还告诉 AI 主动保存。

### RAG 装配

`core/runtime.py::_setup_rag` 在 `mode=rag` 时按顺序装：

```
features.embedding.provider → 复用现有 LLM provider 的 base_url
                            → OpenAICompatEmbeddingService(base_url, key, model)
mem_dir / rag.jsonl         → RagStore（cosine top-k 检索）
ImportantMemoryManager.attach_rag(svc, store)
```

之后 `ImportantMemoryManager.save()` 会自动给新条目算 embedding 并存 rag.jsonl。
`message_pipeline` 拼上下文时用最后一条用户消息当 query 调 `retrieve_for_query()`，
只把 top-k 相关条目注入 system prompt，省 token。

向量算法（`memory/rag_store.cosine_similarity`）固定用余弦相似度；
不做归一化（外面传进来的向量保留原长度，cosine 公式自带归一化）。

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

UI 入口在仪表盘「插件」页：列表 + 详情 + 启停 + 配置表单（按 `PLUGIN_META.config_schema` 动态生成）。

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
  10. WakeupScheduler / PendingRequestStore / RateLimiter
  11. MessagePipeline（拼装上面全部）
  12. RecallHandler / RequestHandler / ProactiveLoop
  13. EventBus（订阅 pipeline + handlers）
  14. adapter.start() / proactive_loop.start()

Runtime.wait_until_stop() → 等 SIGINT/SIGTERM

Runtime.shutdown() → 反序关闭
```

详见 `core/runtime.py`。

---

## 测试策略

- **单元测试**（`tests/`）：每模块独立，用 fake 替身注入依赖
- **集成测试**（P1.10 待做）：跑完整 message pipeline，验证收消息 → Agent → 发消息全链路
- **Live 测试**（`tests/test_kv_cache_real.py`）：需真实 API 密钥，标记 `@pytest.mark.live`。默认不跑
- **KV 缓存命中率实测**（P1.10 必做）：验证连续 10 轮对话整体命中率 > 90%

跑测试：

```bash
venv/Scripts/python -m pytest tests/ -q
```

---

## 性能优化点

- **uvloop**（Linux/Mac）替代默认 asyncio
- **orjson** 替代 json
- **aiofiles** 替代同步文件 IO
- **JSONL 增量追加**（HistoryManager）替代每次重写整个 JSON
- **KV 缓存前缀稳定**（context_builder）
- **WebSocket 长连接 + 心跳**（NapCatAdapter）

详见 Phase 1.9 完成后的实测数据。
