# RAG 长期记忆（Embedding）

Debata 有两种记忆模式。文件模式零配置直接跑，RAG 模式需要配 embedding 服务但检索更精准。

## 选择模式

设置页 → 记忆 →「长期记忆模式」卡片。

![记忆模式](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_rag_mode.png)

**文件模式**：AI 主动调工具保存记忆，写 `important.json`。单人聊天够用，完全透明。

**RAG 模式**：后台自动提取记忆做向量索引，按语义检索。多群多用户长期运行更靠谱，但需要配 embedding。

## 配 Embedding（RAG 模式）

### API 模式

设置页 → 模型 →「添加提供商」按钮。

点击后，按照图示推荐配置来配置RAG提供商（推荐采用火山方舟）。

![添加提供商](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_rag_add_provider.png)

设置页 → 记忆。
点击“编辑 Embedding 配置”，选 API 服务，填 provider + 模型 ID + Key，若上一步采用火山方舟，则推荐如图配置。

![Embedding API 配置](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_rag_api.png)

推荐：

- 阿里百炼 `text-embedding-v4`
- 智谱 `embedding-3`
- 火山方舟 `doubao-embedding-text-240515`
- OpenAI `text-embedding-3-small`

DeepSeek 主要是聊天平台，不建议拿它做 Embedding。火山的 `doubao-embedding-vision-*` 是图文多模态向量模型，可通用。

### 本地模式

![Embedding 本地配置](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_rag_local.png)

不需要网络。在模型管理页找到对应的 embedding 插件，按安装指引下载模型文件，拖到 `data/models/` 目录。

轻量：`all-MiniLM-L6-v2`（约 23MB）
中文好：`bge-large-zh-v1.5`（约 400MB）

# 注意：配置完后点击下方“重启Debata服务”

## 常见问题

**换 Embedding 模型后检索不准？** 删除 `data/memory/{角色名}/rag.jsonl`，从头积累。

**API 模式报 400？** 可能是模型 ID 填错了，或者 Embedding provider 和聊天用了同一个 DeepSeek key——换独立 provider。
