# 图像识别（Vision）

## 直接操作

1. 先去平台创建 API Key：[智谱](https://open.bigmodel.cn/usercenter/apikeys) / [阿里百炼](https://bailian.console.aliyun.com/) / [火山方舟](https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey) / [OpenAI](https://platform.openai.com/api-keys) / [Anthropic](https://console.anthropic.com/settings/keys) / [Gemini](https://aistudio.google.com/apikey)。
2. 在向导或设置页打开「看懂图片」。
3. 选择独立视觉 provider，填模型 ID 和 API Key。
4. 点「测试连接」，通过后保存并重启 Runtime。

## 模型选择

选择带 vision / image understanding 能力的多模态模型即可。具体模型 ID 经常变化，优先以 provider 预设、设置页模型列表和各平台官方文档为准：

- 智谱：[模型文档](https://docs.bigmodel.cn/)
- 阿里百炼：[Qwen 模型文档](https://help.aliyun.com/zh/model-studio/)
- 火山方舟：[模型列表](https://www.volcengine.com/docs/82379)
- OpenAI：[模型文档](https://platform.openai.com/docs/models)
- Anthropic：[模型文档](https://docs.anthropic.com/en/docs/about-claude/models/overview)
- Gemini：[模型文档](https://ai.google.dev/gemini-api/docs/models)

## 常见问题

- DeepSeek 主模型不能看图：给 Vision 单独配 GLM / Qwen / 豆包。
- 提示没有绑定模型：确认 Vision 已启用，provider 和视觉模型 ID 都不为空。
- 图片太大：先压到 5MB 或 4K 以下。
