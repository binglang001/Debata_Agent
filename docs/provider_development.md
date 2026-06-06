# LLM 提供商开发指南

如何接入新的 LLM API。

## 两种方式

### A. 用已有协议（最常见）

当前只有两个协议（`app_config/schema.py::ProtocolType`）：

| 协议 | 适用 |
|------|------|
| `openai_compat` | OpenAI / DeepSeek / GLM / Moonshot / Qwen / Gemini（兼容端点）/ 火山方舟（兼容端点）/ SiliconFlow / OpenRouter / xAI / Together / Groq |
| `anthropic` | Claude |

只要新厂商提供 OpenAI 兼容端点（绝大多数都提供），写一份 `preset.yaml` 即可，无需写 Python 代码。

### B. 新协议（极少需要）

只有当上游 API 与 OpenAI/Anthropic 都不兼容、且也没法做兼容层时，才在 `providers/registry.py::PROTOCOL_REGISTRY` 中新增协议实现。

---

## A. 写 preset.yaml

`providers/presets/{name}/preset.yaml`：

```yaml
id: my_platform
display_name: My Platform
protocol: openai_compat
base_url: https://api.myplatform.com/v1
reasoning_style: none          # none / thinking_extra_body / thinking_extra_header

models:
  - id: my-model-pro
    display_name: My Model Pro
    capabilities: [chat, tool_call, reasoning]
    context_length: 128000
    pricing:
      input_per_million: 1.0
      output_per_million: 2.0

registration_url: https://platform.myplatform.com
```

`capabilities` 选项：`chat` / `tool_call` / `reasoning` / `vision` / `embedding`。

`tests/test_providers_presets.py` 会自动加载校验。

---

## B. 实现新协议

### 1. 创建 `providers/protocols/myprotocol.py`

```python
from providers.base import IProvider, CompletionResult, ToolCall, Usage, ReasoningConfig

class MyProtocolProvider(IProvider):
    def __init__(self, name, base_url, api_key, *, timeout=120.0):
        super().__init__(name)
        self.client = MySDKClient(base_url, api_key, timeout=timeout)

    async def chat_completion(
        self, messages, *, model, tools=None, temperature=0.6, top_p=1.0,
        max_tokens=16384, reasoning=None, stream=True, timeout=120.0,
        first_token_timeout=30.0, extra=None,
    ) -> CompletionResult:
        sdk_messages = self._convert_messages(messages)
        response = await self.client.chat(model=model, messages=sdk_messages, ...)
        return CompletionResult(
            content=response.text,
            tool_calls=[ToolCall(id=tc.id, name=tc.name, arguments=tc.args_json)
                        for tc in response.tool_calls],
            reasoning_content=response.thinking or "",
            finish_reason=response.stop_reason,
            usage=Usage(
                prompt_tokens=response.usage.input,
                completion_tokens=response.usage.output,
                total_tokens=response.usage.input + response.usage.output,
            ),
            model=response.model,
            raw=response,
        )

    async def aclose(self):
        await self.client.close()
```

### 2. schema 加协议类型

`app_config/schema.py`:

```python
ProtocolType = Literal["openai_compat", "anthropic"]  # 在此添加新协议名
```

### 3. registry 加分支

`providers/registry.py` 的 `build_provider()`:

```python
if protocol == "myprotocol":
    from providers.protocols.myprotocol import MyProtocolProvider
    return MyProtocolProvider(provider_id, base_url, api_key)
```

---

## 约定

### 输入 messages（OpenAI 风格，固定不变）

```python
[
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "..."},
]
```

协议层负责转成各家 SDK 格式。

### tool_calls 格式

```python
{
    "id": "call_abc",
    "type": "function",
    "function": {
        "name": "send_private_messages",
        "arguments": "{...}",  # JSON 字符串，不是 dict
    },
}
```

`arguments` 必须保持 LLM 原始字符串，不要重新编码。

### reasoning_content

支持思考的 provider（DeepSeek R / Claude / Gemini thinking）把思考内容放 `CompletionResult.reasoning_content`。

### usage

必填。`raw` 保留原始 SDK 响应，`utils.cache_metrics` 会按协议解析缓存命中。

---

## KV 缓存

| Provider | 缓存机制 | cached_tokens 字段路径 |
|---------|---------|---------------------|
| OpenAI / DeepSeek / GLM | 自动 | `usage.prompt_tokens_details.cached_tokens` |
| Anthropic | 显式 `cache_control` | `usage.cache_read_input_tokens` |
| Gemini | `cached_contents` API | `usage_metadata.cached_content_token_count` |

Anthropic 风格需要给前缀加 `cache_control: {"type": "ephemeral"}`——参考 `providers/protocols/anthropic_proto.py`。

---

## 参考实现

- `providers/protocols/openai_compat.py`
- `providers/protocols/anthropic_proto.py`

## 提交 PR

- ✅ preset.yaml 通过 `test_builtin_presets_load`
- ✅ 新协议有单元测试（消息转换 + 工具 + reasoning + usage）
- ✅ 不修改 `IProvider` 抽象
