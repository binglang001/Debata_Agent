# 图像识别（Vision）

## 直接操作

1. 先去平台创建 API Key：[智谱](https://open.bigmodel.cn/usercenter/apikeys) / [阿里百炼](https://bailian.console.aliyun.com/) / [火山方舟](https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey) / [OpenAI](https://platform.openai.com/api-keys) / [Anthropic](https://console.anthropic.com/settings/keys) / [Gemini](https://aistudio.google.com/apikey)。
2. 在向导或设置页打开「看懂图片」。
3. 选择独立视觉 provider，填模型 ID 和 API Key。
4. 点「测试连接」，通过后保存并重启 Runtime。

## 当前推荐模型

- 智谱：[模型文档](https://docs.bigmodel.cn/)：`glm-5v-turbo`
- 阿里百炼：[Qwen 模型文档](https://help.aliyun.com/zh/model-studio/)：`qwen3.6-plus`
- 火山方舟：[模型列表](https://www.volcengine.com/docs/82379)：`doubao-seed-2-0-lite-260428`
- OpenAI：[模型文档](https://platform.openai.com/docs/models)：`gpt-5.5`
- Anthropic：[模型文档](https://docs.anthropic.com/en/docs/about-claude/models/overview)：`claude-sonnet-4-6`，更强可用 `claude-opus-4-8`
- Gemini：[模型文档](https://ai.google.dev/gemini-api/docs/models)：`gemini-3-pro`

## 常见问题

- DeepSeek 主模型不能看图：给 Vision 单独配 GLM / Qwen / 豆包。
- 提示没有绑定模型：确认 Vision 已启用，provider 和视觉模型 ID 都不为空。
- 图片太大：先压到 5MB 或 4K 以下。
