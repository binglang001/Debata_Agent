# DeepSeek 任务清单

> 给 DeepSeek 直接复制粘贴的任务规格。
> 每个任务自包含：动机 + 接口签名 + 文件路径 + 验收标准。
> Claude 已搭好框架接口，DS 只填实装代码。
> 这是历史协作任务归档，不是当前运行时提示词或通用开发规则；不要把其中针对特定任务的限制迁移到新提示词、schema 或实现里。

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
- **任务 2**：本地 sentence-transformers embedding（`features/embedding/local_service.py`）—— DS 已实装；Claude 后续追加了 warmup() 改造，现在已经支持后台预热。
- **任务 5**：CHANGELOG + CI（`CHANGELOG.md` + `.github/workflows/test.yml`）—— DS 已完成。
- **任务 6**：4 个文件 docstring 补完 —— DS 已完成。

---

## ⚠️ 重要环境变化（开始前必读）

Claude 这一轮做了以下基础设施改动，**所有任务必须基于这些假设**：

1. **项目改名 Diana → Debata**。包名 `debata_agent`，默认人格 `personas/debata/`。**`KEYRING_SERVICE` 也跟着变了**，所以旧 keyring 的密钥失效——用户需要重新走 `--setup` 或在向导里输密钥。这是预期行为，不要尝试兼容。
2. **预热接口**：`IASRService` / `ITTSService` / `IEmbeddingService` 都加了 `async def warmup()` 抽象方法。**所有新写的插件 service 类必须实装 warmup**（API 类可以是 no-op，本地模型必须真加载模型）。`IEmbeddingService.warmup` 有默认 no-op 实现，可不 override；`IASRService` 和 `ITTSService` 必须实装。
3. **插件下载机制**：`PluginMeta` 加了 `download_sources: list[DownloadSource]` 字段，`PluginManager.install(name, on_progress)` 已就位但 `plugins/downloader.py` 是 NotImplementedError 占位。
4. **workspace 系统**：旧 `data/uploads/` → 新 `data/workspace/`。新增 6 个文件工具（read/write/edit/list/delete/run_python）。`ToolContext.upload_allowed_dir` 已改名为 `workspace_dir`。**修改 send_voice 时音频要存到 `data/workspace/.run/voice_{ts}.wav` 这种 workspace 内位置**。
5. **教程文件**：`docs/feature_guides/{vision,weather,web_search,embedding_rag,asr_whisper,tts_voxcpm}.md` 已存在骨架。

---

## 任务 3：Whisper 本地 ASR 插件（含 warmup）

### 已就位
- **接口**：`features/asr/__init__.py` 定义 `IASRService` + `ASRError`，包含 `async def warmup()` 抽象方法
- **插件机制**：`plugins/base.py`（`PluginManager` / `PluginMeta` / `PluginStatus` / `DownloadSource`）
- **规范文档**：`plugins/PLUGIN_SPEC.md` —— **必读**
- **Runtime 装配**：`core/runtime.py::_setup_plugins` 在 `features.asr.enabled=True && type=local` 时自动 `plugin_manager.build("whisper", config)`，build 完会 fire-and-forget 调 warmup()
- **UI 入口**：仪表盘「插件」页会按 `PLUGIN_META.config_schema` 自动渲染配置表单

### 你要做的文件
- 创建 `plugins/whisper/__plugin__.py`（按 `plugins/PLUGIN_SPEC.md` 格式）
- 创建 `plugins/whisper/whisper_impl.py`（真正的 IASRService 实现，lazy import faster_whisper）
- **不要碰** `features/asr/__init__.py`、`plugins/base.py`、`core/runtime.py`

### `plugins/whisper/__plugin__.py` 模板
```python
from plugins import DownloadSource, PluginMeta

PLUGIN_META = PluginMeta(
    name="whisper",
    display_name="Whisper（本地语音识别）",
    kind="asr",
    model_dir="faster-whisper",  # 实际路径 data/models/faster-whisper/
    size_mb=1500,
    description="基于 faster-whisper 的本地 ASR，离线可用。",
    python_deps=["faster-whisper>=1.0.0"],
    download_url="https://huggingface.co/Systran/faster-whisper-large-v3",
    # auto_download=True 才能用「下载」按钮。文件清单（从 HF 直链）：
    download_sources=[
        DownloadSource(
            url="https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/model.bin",
            dest_filename="large-v3/model.bin",
            size_bytes=3_000_000_000,  # 大概 3GB
        ),
        DownloadSource(
            url="https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/config.json",
            dest_filename="large-v3/config.json",
        ),
        DownloadSource(
            url="https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/tokenizer.json",
            dest_filename="large-v3/tokenizer.json",
        ),
        DownloadSource(
            url="https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/vocabulary.txt",
            dest_filename="large-v3/vocabulary.txt",
        ),
        DownloadSource(
            url="https://huggingface.co/Systran/faster-whisper-large-v3/resolve/main/preprocessor_config.json",
            dest_filename="large-v3/preprocessor_config.json",
        ),
    ],
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
            "default": "data/models/faster-whisper/",
            "label": "模型目录",
        },
    },
    auto_download=True,
)


def build(config: dict):
    from .whisper_impl import WhisperService
    return WhisperService(
        model_size=config.get("model_size", "large-v3"),
        device=config.get("device", "auto"),
        language=config.get("language", "zh"),
        model_dir=config.get("model_dir", "data/models/faster-whisper/"),
    )
```

### `whisper_impl.py` 实现要点
- `from features.asr import IASRService, ASRError`
- 构造接受上面 4 个参数；**不**加载模型
- `async def warmup()`：lazy 加载 `WhisperModel(model_size, device=resolved_device, compute_type="float16" if cuda else "int8", download_root=model_dir)`。用 `asyncio.to_thread` 包同步调用。幂等（用 `self._ready: asyncio.Event` 标记）
- `async def transcribe(audio_path)`：若 `_ready` 未 set，先 `await warmup()`；然后 `asyncio.to_thread(self._model.transcribe, audio_path, language=self.language or None)` → 拼 `"".join(seg.text for seg in segments).strip()`
- `device="auto"` 时按 `torch.cuda.is_available()` 选 cuda/cpu
- `aclose`：`self._model = None`，no-op 即可

### 依赖
- `venv/Scripts/pip install faster-whisper`（GPU 推理还要 `nvidia-cublas-cu12` `nvidia-cudnn-cu12`，按需）

### 验收
- 给一个 wav/mp3 能转录出文字
- 启动 Debata 时不**同步**加载模型；后台 warmup 完成后日志会打 `ASR 预热完成（耗时 XX.Xs）`
- 全 279 测试仍过

---

## 任务 4：VoxCPM2 本地 TTS 插件（含 warmup + send_voice）

### 已就位
- **接口**：`features/tts/__init__.py` 定义 `ITTSService` + `TTSError`，含 `warmup()` 抽象方法
- **工具**：`tools/feature_tools.py::send_voice_message` 已注册（feature='tts'），仅 `features.tts.enabled=True` 时进入 schema
- **Runtime 装配**：`core/runtime.py::_setup_plugins` 在 `features.tts.enabled=True && type=local` 时自动 `plugin_manager.build("voxcpm2", config)` 并把 instance 传给 pipeline.tts 和 ToolContext.tts
- **adapter.send_voice 约定**：`async def send_voice(target_type: str, target_id: int, audio_path: Path) -> str | None` —— 当前 NapCatAdapter 还**没有**这个方法，请你顺手在 `adapters/napcat/adapter.py` 加上

### 你要做的文件
- 创建 `plugins/voxcpm2/__plugin__.py`
- 创建 `plugins/voxcpm2/voxcpm_impl.py`
- 在 `adapters/napcat/adapter.py` 加 `send_voice` 方法（**只加这一个方法**，不要碰别的）

### `__plugin__.py` 框架
照任务 3 的模板，把 kind 改成 `"tts"`，size_mb≈3000，config_schema 至少含：
- `model_dir`（string，默认 `"data/models/VoxCPM2/"`）
- `reference_audio`（string，**必填**，给个例子在 help 里）
- `default_prompt`（string，可空）

`download_sources` 看 VoxCPM2 仓库放了哪些文件（一般有 model.safetensors / config.json / tokenizer.json），全列出。

### `voxcpm_impl.py` 实装要点
- 实现 `ITTSService`，包括 `warmup() / synthesize() / aclose()`
- `warmup()` 加载 VoxCPM 模型（具体 API 看 https://github.com/OpenBMB/VoxCPM）
- `synthesize(text, reference_audio=None, prompt="")`：
  - 若未 warmup，先 await warmup
  - 输出 wav 到 **workspace 内**：`data/workspace/.run/voice_{int(time.time()*1000)}.wav`（不是 data/uploads！）
  - 返回 Path
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
为 3 个常见但 Debata 还不支持的 LLM 平台添加 preset。**全做**，不要挑。

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
- 启动 Debata → 向导 / 设置页能看到新 provider
- 引用现有：「照 `providers/presets/deepseek/` 葫芦画瓢」

#### 「加一个 Tool」节
步骤化。覆盖：
- 在 `tools/schemas.py` 加 Pydantic args 模型（继承 `_ToolArgs`）
- 在 `tools/{category}_tools.py`（messaging / memory_tools / platform_tools / control_tools / feature_tools / workspace_tools 其中之一）加 `@tool(...)` 装饰的 async 函数
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
- 全 279 测试仍过

---

## 任务 9：完善 6 篇 feature 教程内容（现在做）

### 动机
Claude 已写了 6 篇教程骨架，但是骨架是「我大致知道这些功能要写什么」级别，**很多技术细节、配置示例、踩坑提示需要你补充**。

### 文件清单
- `docs/feature_guides/vision.md`（图像识别）
- `docs/feature_guides/weather.md`（和风天气）
- `docs/feature_guides/web_search.md`（DuckDuckGo）
- `docs/feature_guides/embedding_rag.md`（RAG 长期记忆）
- `docs/feature_guides/asr_whisper.md`（Whisper ASR）
- `docs/feature_guides/tts_voxcpm.md`（VoxCPM2 TTS）

### 你要做的
**只**润色和扩充内容，**不改文件名 / 不删 Claude 写的节**。允许的操作：
- 在每个章节下加更多详细步骤、截图位置标记（用 `![待补图：xxx](placeholder)`）、踩坑案例
- 补充实际配置示例 yaml
- 校对技术准确性（特别是 API 端点、模型 ID、价格等会过时的信息）—— 如果发现 Claude 写错，请改，但要可靠（不要瞎写）
- 加 FAQ 节（常见错误码、问题答疑）

### 约束
- 保持简洁，每篇控制在 250 行以内
- 中文，对话感强
- 不写 marketing 话术（「行业领先」「业界最优」之类废话）
- 不要在文档里写 Co-Authored-By 或任何 AI 协作署名

### 验收
- 6 个文件存在且有充实内容
- 启动 Debata → 向导 features 页 → 点教程按钮 → 能看到内容
- 全 279 测试仍过（不涉及代码，但跑一下）

---

## 任务 10：插件下载器实装（现在做，**重要**）

### 动机
Claude 已写了 `plugins/downloader.py` 的接口骨架（`async def download_sources(...)`），但内部是 `NotImplementedError`。你来填实装。这是任务 3（Whisper）能用「下载」按钮自动拉模型的前提。

### 文件
- 只改 `plugins/downloader.py`（**不要碰** `plugins/base.py` 或别的）

### 接口签名（已存在，不要改）
```python
async def download_sources(
    sources: list[DownloadSource],
    target_dir: Path,
    on_progress: DownloadProgressCallback | None = None,
) -> None:
```

`DownloadSource` 字段（在 `plugins/base.py`）：
- `url: str` —— 下载 URL（HTTPS）
- `dest_filename: str` —— 相对 target_dir 的目标路径（支持 `large-v3/model.bin` 这种子目录）
- `sha256: str = ""` —— 非空时下载后校验
- `size_bytes: int = 0` —— UI 进度条用，可不精确
- `required: bool = True`

`on_progress` 签名：`(filename: str, bytes_done: int, bytes_total: int, msg: str) -> None`

### 实装要点
1. **串行下载**所有 sources（不并发，避免 NapCat 服务器或 HF CDN 限流）
2. **跳过已存在 + sha256 匹配**的文件
3. **stream 下载**：`httpx.AsyncClient.stream` 配合 `iter_bytes(chunk_size=64*1024)`，每收到一块就累加 done，每 ~500ms 调一次 `on_progress`
4. **sha256 校验**：边下边算 `hashlib.sha256()`；下载完非空校验失败 → 删文件 raise `ValueError(...)`
5. **重试机制**：每个文件失败重试最多 3 次，间隔 1s/3s/5s 指数退避；3 次后 raise `RuntimeError("文件 X 下载失败：网络错误重试耗尽")`
6. **代理支持**：`httpx.AsyncClient(proxy=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"))`
7. **on_progress 异常隔离**：用 `try / except Exception: pass` 包住每次回调
8. **断点续传（可选，加分项）**：如果文件已存在但 size 比 total 小，发 `Range: bytes={done}-` 续传；服务器不支持 206 Partial Content 时降级为重下

### 进度回调示例
```python
# 开始时：
on_progress(dest_filename, 0, total, "开始下载")
# 流式过程中：
on_progress(dest_filename, done, total, "下载中")
# 完成：
on_progress(dest_filename, total, total, "完成")
# 失败：
on_progress(dest_filename, done, total, f"失败：{e}")
```

### 验收
- 用真实 HuggingFace 链接测试一个小文件下载，能正常完成并触发 on_progress
- sha256 校验失败时文件被删
- 全 279 测试仍过
- `venv/Scripts/python -c "from plugins import downloader; print(downloader.download_sources)"` 不再返回 NotImplementedError 函数

---

## 不要给 DS 做的（Claude 留着自己做）

| 任务 | 原因 |
|---|---|
| **修任何已存在 bug** | 容易"顺手优化"踩雷，不可控 |
| **任何 UI 改动**（除非任务 4 加的 send_voice 完全是 adapter 层） | 前科：PySide6 风格难拿捏 |
| **改架构、改接口签名** | 跨模块一致性 |
| **改 `agents/context_builder.py`** | KV 缓存命中率敏感，乱改会破坏 cache |
| **改 `core/message_pipeline.py` / `core/runtime.py`** | 消息处理与装配核心，bug 影响范围大 |
| **Persona 相关任何东西** | 私设隐私敏感 |
| **`plugins/base.py` / `features/{asr,tts,embedding}/__init__.py` 修改** | 接口层 Claude 已定，DS 只能实装具体插件 |
| **`tools/workspace_tools.py` / `tools/workspace.py` 修改** | workspace 沙箱安全敏感 |

---

## 工作流

每完成一个任务：
1. 跑 `venv/Scripts/python -m pytest tests/ -q --ignore=tests/test_kv_cache_real.py` 确认 279 测试仍过
2. 让 binglang001 自己 review diff，**不要自己 commit**
3. 不要写"已完成 / 总结"之类的话，直接告诉 binglang001 改了哪些文件就够
