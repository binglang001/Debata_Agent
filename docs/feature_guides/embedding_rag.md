# RAG 长期记忆（Embedding）

## 直接操作

1. 先准备 Embedding 服务密钥：[阿里百炼](https://bailian.console.aliyun.com/) / [智谱](https://open.bigmodel.cn/usercenter/apikeys) / [火山方舟](https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey) / [OpenAI](https://platform.openai.com/api-keys)。
2. 在向导或设置页把「长期记忆」切到 RAG。
3. 选 API 服务或本地模型，填模型 ID，点「测试连接」。
4. 通过后进入下一步；失败时看返回的 HTTP 状态和响应内容。

## 记忆范围

长期记忆仍是一份全局存储，不会按群或私聊拆库。每条记忆有两个范围字段：

- `scope`：`global`、`user:QQ号` 或 `group:群号`。机器人在当前会话里优先注入匹配当前私聊/群聊的记忆和全局记忆。
- `pinned`：置顶。置顶记忆在所有会话中常驻注入，适合身份、长期约定、全局禁忌这类稳定信息。

模型调用 `save_important_memory` 或用户触发“记住”关键词时，系统会默认按当前会话推断 scope。需要手动调整时，到仪表盘「重要记忆」页修改 scope 或勾选置顶。RAG 模式下保存元数据会同步重建索引。

## API 模式推荐

- 阿里百炼：[Embedding 文档](https://help.aliyun.com/zh/model-studio/)：`text-embedding-v4`
- 智谱：[Embedding 文档](https://docs.bigmodel.cn/)：`embedding-3`
- 火山方舟：[Embedding API](https://www.volcengine.com/docs/82379/1302003/)：`doubao-embedding-text-240515`
- OpenAI：[Embedding 文档](https://platform.openai.com/docs/guides/embeddings)：`text-embedding-3-small` 或 `text-embedding-3-large`

DeepSeek 主要是聊天模型平台，不建议拿它复用做 Embedding；如果返回 400 或参数调用错误，通常是模型 ID、接口类型或 provider 不匹配，请换独立 Embedding provider。
火山的 `doubao-embedding-vision-*` 是图文多模态向量模型，不适合作为普通文本 `/embeddings` 默认项；RAG 文本检索优先使用 `doubao-embedding-text-*`。

## 本地模式

在「模型管理」打开安装指引，下载后拖入模型文件夹。

- 轻量：`sentence-transformers/all-MiniLM-L6-v2`
- 中文质量：`BAAI/bge-large-zh-v1.5`

切换 Embedding 模型后，旧向量不能混用。检索明显不准时，删除对应 persona 记忆目录下的 `rag.jsonl` 后重新积累记忆。
