# DeepSeek 任务清单

> 给 DeepSeek 直接复制粘贴的任务规格。
> 每个任务自包含：动机 + 接口签名 + 文件路径 + 验收标准。
> Claude 已搭好框架接口，DS 只填实装代码。

## 通用约束（所有任务都要遵守）

- **不动其它文件**。每个任务限定改/创建的文件清单；不在清单内的文件**绝对不要碰**。
- **不重命名 / 不重构 / 不顺手优化**。只解决任务描述的事，不写"灵活性 / 可扩展性"代码。
- **写中文注释**，但**不写多段 docstring**。一行简短中文足够。
- **不加 fallback / 不加重试**，除非任务明确要求。
- **不写新测试**，除非任务明确要求。
- **`venv/Scripts/python -m pytest tests/ -q --ignore=tests/test_kv_cache_real.py` 必须仍全过**。如果通不过，回退你的改动重写。
- **Provider import 路径**：用 `from providers import OpenAICompatProvider, AnthropicProvider`，构造传 `name` 位置参数，关闭用 `await provider.aclose()`（不是 `close()`）。
- **绝对不要写 Co-Authored-By**。绝对不要 `git commit`。改完让 binglang001 自己 commit。

---

## ✅ 已完成（不要重做）

- **任务 1**：embedding API service（`features/embedding/service.py`）—— DS 已实装。
- **任务 5**：CHANGELOG + CI（`CHANGELOG.md` + `.github/workflows/test.yml`）—— DS 已完成。
- **任务 6**：4 个文件 docstring 补完 —— DS 已完成。

---

## 任务 2：本地 sentence-transformers embedding（现在做）

### 动机
RAG 也支持纯本地不联网模式。在 `features/embedding/local_service.py` 实现一个用 `sentence-transformers` 的版本。
模型存在 `F:/.models/bge-large-zh-v1.5/`（质量）或 `F:/.models/all-MiniLM-L6-v2/`（速度）。

### 文件
- 创建 `features/embedding/local_service.py`
- 修改 `features/embedding/__init__.py`：把 `LocalEmbeddingService` 加到 `__all__`，但用 **lazy import**（不在顶层 import 它）

### 接口（继承现有 IEmbeddingService）
```python
from .service import IEmbeddingService

class LocalEmbeddingService(IEmbeddingService):
    def __init__(self, model_dir: str, device: str = "auto") -> None: ...
    async def embed_one(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    async def aclose(self) -> None: ...  # no-op
    @property
    def dimension(self) -> int: ...
```

### 实现要点
- `from sentence_transformers import SentenceTransformer`
- `device="auto"` 时优先 cuda（`torch.cuda.is_available()`），没有则 cpu
- `embed_one`：用 `asyncio.to_thread(self._model.encode, text)` 包同步调用，然后 `.tolist()`
- `embed_batch`：同上但传 list
- `dimension`：`self._model.get_sentence_embedding_dimension()`
- `aclose`：no-op
- 模型 lazy load：构造时只存参数，第一次 embed 时才 `SentenceTransformer(model_dir, device=device)`

### `features/embedding/__init__.py` 改法
顶层**不要** import `local_service`（避免在用户没装 sentence-transformers 时整个模块崩）。改成：

```python
def get_local_service(model_dir: str, device: str = "auto"):
    """工厂函数，按需 lazy import LocalEmbeddingService。"""
    from .local_service import LocalEmbeddingService
    return LocalEmbeddingService(model_dir, device)
```

并在 `__all__` 加 `"get_local_service"`。

### 依赖
- `venv/Scripts/pip install sentence-transformers torch`

### 验收
- `venv/Scripts/python -c "from features.embedding import get_local_service; svc = get_local_service('F:/.models/bge-large-zh-v1.5'); print('ok')"` 不报错（即使模型不存在，构造也不能挂——lazy load）
- 全 279 测试仍过

---

## 任务 3：Whisper 本地 ASR 插件（现在做）

### 动机
让 Diana 能听懂语音消息。基于 faster-whisper。

### 已就位（Claude 已做）
- **接口**：`features/asr/__init__.py` 已定义 `IASRService` + `ASRError`
- **插件机制**：`plugins/` 目录 + `plugins/base.py`（`PluginManager` / `PluginMeta` / `PluginStatus`）
- **规范文档**：`plugins/PLUGIN_SPEC.md` —— **必读**
- **Runtime 装配**：`core/runtime.py::_setup_plugins` 在 `features.asr.enabled=True && type=local` 时自动 `plugin_manager.build("whisper", config)`
- **UI 入口**：仪表盘「插件」页会按 `PLUGIN_META.config_schema` 自动渲染配置表单

### 你要做的文件
- 创建 `plugins/whisper/__plugin__.py`（按 `plugins/PLUGIN_SPEC.md` 格式）
- 创建 `plugins/whisper/whisper_impl.py`（真正的 IASRService 实现，lazy import faster_whisper）
- **不要碰** `features/asr/__init__.py`、`plugins/base.py`、`core/runtime.py`

### `plugins/whisper/__plugin__.py` 模板
```python
from plugins import PluginMeta

PLUGIN_META = PluginMeta(
    name="whisper",
    display_name="Whisper（本地语音识别）",
    kind="asr",
    model_dir="faster-whisper",  # 实际路径 F:/.models/faster-whisper/
    size_mb=800,
    description="基于 faster-whisper 的本地 ASR，离线可用。",
    python_deps=["faster-whisper>=1.0.0"],
    download_url="https://huggingface.co/Systran/faster-whisper-large-v3",
    config_schema={
        "model_size": {
            "type": "select",
            "default": "large-v3",
            "label": "模型大小",
            "options": ["tiny", "base", "small", "medium", "large-v3"],
            "help": "越大越准也越慢；large-v3 中文识别推荐。",
        },
        "device": {
            "type": "select",
            "default": "auto",
            "label": "设备",
            "options": ["auto", "cuda", "cpu"],
        },
        "language": {
            "type": "string",
            "default": "zh",
            "label": "默认语言（ISO 639-1；留空=自动检测）",
        },
        "model_dir": {
            "type": "string",
            "default": "F:/.models/faster-whisper/",
            "label": "模型目录",
        },
    },
    auto_download=False,
)


def build(config: dict):
    from .whisper_impl import WhisperService
    return WhisperService(
        model_size=config.get("model_size", "large-v3"),
        device=config.get("device", "auto"),
        language=config.get("language", "zh"),
        model_dir=config.get("model_dir", "F:/.models/faster-whisper/"),
    )
```

### `whisper_impl.py` 实现要点
- `from features.asr import IASRService, ASRError`
- 构造接受上面 4 个参数；**不**加载模型
- `transcribe(audio_path)`：第一次调用 lazy 加载 `WhisperModel(model_size, device=resolved_device, compute_type="float16" if cuda else "int8", download_root=model_dir)`
- `device="auto"` 时按 `torch.cuda.is_available()` 选 cuda/cpu
- `model.transcribe(audio_path, language=self.language or None)` → 拼 `"".join(seg.text for seg in segments).strip()`
- `aclose`：`self._model = None`，no-op 即可（faster-whisper 没有显式关闭）

### 依赖
- `venv/Scripts/pip install faster-whisper`（GPU 推理还要 `nvidia-cublas-cu12` `nvidia-cudnn-cu12`，按需）

### 验收
- 给一个 wav/mp3 能转录出文字
- 启动 Diana 时不加载模型（lazy）
- 全 279 测试仍过

---

## 任务 4：VoxCPM2 本地 TTS 插件（现在做）

### 已就位
- **接口**：`features/tts/__init__.py` 已定义 `ITTSService` + `TTSError`
- **工具**：`tools/feature_tools.py::send_voice_message` 已注册（feature='tts'），仅 `features.tts.enabled=True` 时进入 schema
- **Runtime 装配**：`core/runtime.py::_setup_plugins` 在 `features.tts.enabled=True && type=local` 时自动 `plugin_manager.build("voxcpm2", config)` 并把 instance 传给 pipeline.tts 和 ToolContext.tts
- **adapter.send_voice 约定**：`async def send_voice(target_type: str, target_id: int, audio_path: Path) -> str | None` —— 当前 NapCatAdapter 还**没有**这个方法，请你顺手在 `adapters/napcat/adapter.py` 加上（用 OneBot V11 的 `send_msg` API + record 段）

### 你要做的文件
- 创建 `plugins/voxcpm2/__plugin__.py`
- 创建 `plugins/voxcpm2/voxcpm_impl.py`
- 在 `adapters/napcat/adapter.py` 加 `send_voice` 方法（**只加这一个方法**，不要碰别的）

### `__plugin__.py` 框架
照任务 3 的模板，把 kind 改成 `"tts"`，config_schema 至少含：
- `model_dir`（string，默认 `"F:/.models/VoxCPM2/"`）
- `reference_audio`（string，**必填**，给个例子在 help 里）
- `default_prompt`（string，可空）

### `voxcpm_impl.py` 实装要点
- 实现 `ITTSService.synthesize(text, reference_audio=None, prompt="") -> Path`
- lazy 加载 VoxCPM 模型（具体 API 看 https://github.com/OpenBMB/VoxCPM）
- 输出 wav 到 `data/uploads/voice_{timestamp}.wav` 然后返回 Path
- `reference_audio=None` 时用构造时传的 default
- 失败 raise `TTSError`

### `NapCatAdapter.send_voice` 实装
- OneBot V11 用 `[CQ:record,file=file:///绝对路径.wav]` 段
- 调 `self.api.call_action("send_msg", message_type=target_type, user_id/group_id=target_id, message=[{type:"record", data:{file: ...}}])`
- 返回 message_id（用现有的 `send_text` 做参考）

### 验收
- AI 调 send_voice_message 工具 → 合成 → 通过 NapCat 真的发到 QQ
- 不启用 TTS 时（`features.tts.enabled=False`），`send_voice_message` 工具不出现在 schema
- 全 279 测试仍过

---

## ✅ 任务 5：已完成（CHANGELOG + CI）

---

## ✅ 任务 6：已完成（docstring）

---

## 任务 7：新增 provider preset（现在做，全做）

### 目标
为 3 个常见但 Diana 还不支持的 LLM 平台添加 preset。**全做**，不要挑。

### 候选清单
1. **xAI Grok**（base_url: `https://api.x.ai/v1`，protocol: openai_compat）
2. **Together AI**（base_url: `https://api.together.xyz/v1`，protocol: openai_compat）
3. **Groq**（base_url: `https://api.groq.com/openai/v1`，protocol: openai_compat）

### 文件
对每个：
- 创建 `providers/presets/{name}/preset.yaml`（**照 `providers/presets/deepseek/preset.yaml` 葫芦画瓢**，含 models 列表）
- 创建 `providers/presets/{name}/tutorial/get_api_key.md`（简短中文教程，3-5 段，含官网注册地址 + 计费说明）

### models 列表参考（仅写 3-5 个主流型号即可，不要列全）
- xAI：`grok-2-1212`、`grok-2-vision-1212`
- Together：`meta-llama/Llama-3.3-70B-Instruct-Turbo`、`Qwen/Qwen2.5-72B-Instruct-Turbo`、`deepseek-ai/DeepSeek-V3`
- Groq：`llama-3.3-70b-versatile`、`mixtral-8x7b-32768`、`gemma2-9b-it`

### 验收
- `venv/Scripts/python -c "from providers.registry import ProviderRegistry; r = ProviderRegistry(); from pathlib import Path; r.load_presets(Path('providers/presets')); print(r.list_presets())"` 能加载新增 preset 不报错
- 全 279 测试仍过

---

## 任务 8：CONTRIBUTING.md 追加两节（现在做）

### Claude 已写
`CONTRIBUTING.md` 已有「项目准则 / PR 流程 / Code Style / 报 bug / License」5 节。
另外两节用 HTML 注释占位：
```
<!-- DS-INSERT-PROVIDER-PRESET -->
<!-- DS-INSERT-NEW-TOOL -->
```

### 你要做的
**只**替换这两个占位（连同它们上面的「> 本节由 DeepSeek 补充」一行一起替换），写：

#### 「加一个 provider preset」节
步骤化（1. 2. 3.）。每步给文件路径 + 最小示例。
内容覆盖：
- 在 `providers/presets/{name}/` 新建 `preset.yaml`（含 base_url / protocol / models 列表 / 默认 timeout）
- 在 `providers/presets/{name}/tutorial/get_api_key.md` 写中文教程
- 启动 Diana → 向导 / 设置页能看到新 provider
- 引用现有：「照 `providers/presets/deepseek/` 葫芦画瓢」

#### 「加一个 Tool」节
步骤化。覆盖：
- 在 `tools/schemas.py` 加 Pydantic args 模型（继承 `_ToolArgs`）
- 在 `tools/{category}_tools.py`（messaging / memory_tools / platform_tools / control_tools / feature_tools 其中之一）加 `@tool(...)` 装饰的 async 函数
- 若依赖 ToolContext 新字段：在 `tools/base.py::ToolContext` 加字段 + 在 `core/runtime.py` 装配时注入
- 工具签名：`async def my_tool(args: MyArgs, ctx: ToolContext) -> dict`
- 返回值约定：`{"ok": True, ...}` 或 `{"ok": False, "error": "..."}`
- 引用现有：「照 `tools/feature_tools.py` 的 web_search 葫芦画瓢」

### 约束
- **只动 CONTRIBUTING.md 这一个文件**
- **只替换两个占位**，不动 Claude 已写的 5 节
- 不写「Code of Conduct」「Release Notes」等其它节

### 验收
- markdown 渲染正常
- 两个 `<!-- DS-INSERT-* -->` 占位被实际内容替换
- 占位标记本身可以删掉
- 全 279 测试仍过（不涉及代码改动，但跑一下）

---

## 不要给 DS 做的（Claude 留着自己做）

| 任务 | 原因 |
|---|---|
| **修任何已存在 bug** | 容易"顺手优化"踩雷，不可控 |
| **任何 UI 改动**（除非任务 4 加的 send_voice） | 前科：PySide6 风格难拿捏 |
| **改架构、改接口签名** | 跨模块一致性 |
| **改 `agents/context_builder.py`** | KV 缓存命中率敏感，乱改会破坏 cache |
| **改 `core/message_pipeline.py` / `core/runtime.py`** | 消息处理与装配核心，bug 影响范围大 |
| **Persona 相关任何东西** | yuexi 私设隐私敏感 |
| **`plugins/base.py` / `features/{asr,tts,embedding}/__init__.py` 修改** | 接口层 Claude 已定，DS 只能实装具体插件 |

---

## 工作流

每完成一个任务：
1. 跑 `venv/Scripts/python -m pytest tests/ -q --ignore=tests/test_kv_cache_real.py` 确认 279 测试仍过
2. 让 binglang001 自己 review diff，**不要自己 commit**
3. 不要写"已完成 / 总结"之类的话，直接告诉 binglang001 改了哪些文件就够
