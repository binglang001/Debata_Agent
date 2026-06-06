# 图像识别（Vision）

让 Debata 能看懂图片——用户发了图，AI 看了再回复。

## 前提

需要一个多模态模型。推荐 GLM-4V、Qwen-VL、GPT-4o 或 Gemini。DeepSeek 不支持看图，给它单独配一个 Vision provider 就行。

## 操作

### 添加提供商

在配置之前，请确保你配置了提供商：

设置页 → 模型 →「添加提供商」按钮。

点击后，按照图示推荐配置来配置视觉提供商（推荐采用火山方舟）。

![添加提供商](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_vision_add_provider.png)

### 打开 Vision

设置页 → 功能 →「看懂图片」卡片，打开开关。首次打开会弹配置对话框，若上一步提供商按照推荐配置，则可参考如图配置。

![设置页 Vision 卡片](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_vision_settings.png)

**配置完后点击下方“重启Debata服务”**

## 获取 API Key

- [智谱](https://open.bigmodel.cn/usercenter/apikeys)
- [阿里百炼](https://bailian.console.aliyun.com/)
- [火山方舟](https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey)
- [OpenAI](https://platform.openai.com/api-keys)
- [Anthropic](https://console.anthropic.com/settings/keys)
- [Gemini](https://aistudio.google.com/apikey)

## 常见问题

**图片太大？** 压到 5MB 或 4K 以下再发。
