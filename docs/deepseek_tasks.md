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

## 任务 1：embedding API service（RAG 必需，Claude 已搭好接口）

### 动机
`features/embedding/service.py` 已经定义了 `IEmbeddingService` 抽象 + `OpenAICompatEmbeddingService` 的骨架，但 `embed_one` 和 `embed_batch` 还是 `raise NotImplementedError`。装实它们让 RAG 能跑起来。

### 文件
- 只改 `features/embedding/service.py`（不要碰别的）

### 接口（已存在，不要改签名）
```python
class IEmbeddingService(ABC):
    @abstractmethod
    async def embed_one(self, text: str) -> list[float]: ...
    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    @abstractmethod
    async def aclose(self) -> None: ...
    @property
    @abstractmethod
    def dimension(self) -> int: ...
```

### 实现要点
- `OpenAICompatEmbeddingService.__init__(base_url, api_key, model, timeout=60)`：保存这些参数 + 用 `httpx.AsyncClient(base_url=base_url, timeout=timeout)`。
- `embed_one(text)`：调 `POST /embeddings`，body = `{"model": self.model, "input": text}`，Header `Authorization: Bearer {api_key}`。从返回 JSON 取 `data[0].embedding` 返回。
- `embed_batch(texts)`：同上但 `input` 直接传 list。返回 `[item["embedding"] for item in data]`。注意：input 不要分批，一次性传，OpenAI 兼容协议都支持。
- `aclose()`：`await self._client.aclose()`。
- `dimension`：第一次成功 embed 后从结果长度推断并缓存到 `self._dim`；未调用过返回 0。

### 错误处理
- HTTP 4xx/5xx：raise 一个清晰的 `EmbeddingError(f"embedding 请求失败：{status_code} {text}")`（`EmbeddingError` 在 `features/embedding/__init__.py` 已定义）。
- 网络超时：raise 同样的 `EmbeddingError`。
- 不要写 retry。

### 验收
- `venv/Scripts/python -c "import asyncio; from features.embedding import OpenAICompatEmbeddingService; svc = OpenAICompatEmbeddingService(base_url='https://api.deepseek.com', api_key='$DEEPSEEK_API_KEY', model='text-embedding-v1'); print(asyncio.run(svc.embed_one('你好')))"` 能跑通输出一个 list。
- 全 275 测试仍过。

---

## 任务 2：本地 sentence-transformers embedding（P3，可选）

### 动机
RAG 也支持纯本地不联网模式。在 `features/embedding/local_service.py` 实现一个用 `sentence-transformers` 的版本。模型存在 `F:/.models/bge-large-zh-v1.5/`（quality）或 `F:/.models/all-MiniLM-L6-v2/`（performance）。

### 文件
- 创建 `features/embedding/local_service.py`
- 修改 `features/embedding/__init__.py` 把 `LocalEmbeddingService` 加到 `__all__`

### 接口
```python
class LocalEmbeddingService(IEmbeddingService):
    def __init__(self, model_dir: str, device: str = "auto") -> None: ...
```

### 实现要点
- 用 `from sentence_transformers import SentenceTransformer`
- `device="auto"` 时优先 cuda，没有则 cpu
- `embed_one`：`return self._model.encode(text).tolist()`
- `embed_batch`：`return self._model.encode(texts).tolist()`
- `dimension`：`self._model.get_sentence_embedding_dimension()`
- `aclose`：no-op

### 依赖
- `venv/Scripts/pip install sentence-transformers torch`（用户机器有 16GB GPU + CUDA）

### 验收
- 同任务 1，能跑通 embed_one 不报错。
- 不能在 `features/embedding/__init__.py` 顶层 import 它（避免在用户不装 sentence-transformers 时整个模块炸）—— 用 lazy import：
  ```python
  def get_local_service(model_dir, device="auto"):
      from .local_service import LocalEmbeddingService
      return LocalEmbeddingService(model_dir, device)
  ```

---

## 任务 3：Whisper 本地 ASR 插件（P3）

### 动机
Phase 3 计划，集成本地 faster-whisper 做语音识别，让 Diana 能听懂语音消息。

### 文件
- 创建 `features/asr/local_whisper.py`
- 修改 `features/asr/__init__.py` 加 `LocalWhisperService` 导出

### 接口
- 模仿现有 `IVisionService` 风格（`features/vision/__init__.py` 里有），写 `IASRService` 抽象（如果不存在）+ `LocalWhisperService` 实现。
- 关键方法：`async def transcribe(audio_path: Path | str) -> str`

### 实现要点
- 用 `from faster_whisper import WhisperModel`
- 构造接受 `model_size="large-v3"`, `device="auto"`, `language="zh"`
- model 用 lazy load（首次 transcribe 时才加载，避免启动慢）
- 模型目录在 `F:/.models/faster-whisper/{model_size}/`
- 转录：`segments, info = model.transcribe(audio_path, language=self.language)`，拼接所有 segments 的 text 返回。

### 依赖
- `venv/Scripts/pip install faster-whisper`

### 验收
- 给一个 wav/mp3/m4a 文件能转录出文字。
- 启动时不加载模型（lazy）。

---

## 任务 4：VoxCPM2 本地 TTS 插件（P3）

### 动机
Phase 3，让 Diana 能用声音说话。模型存在 `F:/.models/VoxCPM2/`。

### 文件
- 创建 `features/tts/local_voxcpm.py`
- 修改 `features/tts/__init__.py` 加导出
- 修改 `tools/feature_tools.py` 加 `send_voice_message` 工具（仅 TTS 启用时注册）

### 注意
- VoxCPM2 是清华开源 TTS，需要参考音频做 voice cloning
- 配置：`reference_audio: str`, `default_prompt: str`
- 不要追加新依赖如果 VoxCPM2 已经在仓库 vendored；否则 `pip install voxcpm`

### 验收
- 给 text + 参考音频，能输出 wav 文件
- send_voice_message 工具能让 AI 调用，发到 NapCat

---

## 任务 5：README.md 之外的 Phase 4 文档

### 注意
- **README.md 由 Claude 写，不要碰**
- 你只写下面 3 个文件

### 5.1 CHANGELOG.md
- 在仓库根目录创建 `CHANGELOG.md`
- 用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式
- 用 `git log --oneline v0.1.0-alpha..HEAD` 列出所有 commits，按 Added / Changed / Fixed / Removed 分类
- 现有 tag 信息：
  - `v0.1.0-alpha`：Phase 1.0~1.8 V2 重构
  - `v0.2.0-alpha`：Phase 1.9 集成测试 + KV 实测 + V1 清理
  - 当前 develop HEAD：Phase 2 GUI（向导 + 7 页仪表盘 + 即时保存设置 + 圆角窗口 + Persona 中文名 + 密钥失败恢复 等）→ 准备 tag 为 v0.3.0-alpha

### 5.2 .github/workflows/test.yml
- GitHub Actions CI
- 触发：push 到 main/develop + PR
- Matrix：Windows + Linux，Python 3.11 + 3.12
- 步骤：
  ```yaml
  - checkout
  - setup-python
  - pip install -r requirements.txt（如有；否则跳过 → 仅装 pyproject.toml 依赖：pip install .）
  - pytest tests/ -q --ignore=tests/test_kv_cache_real.py
  ```
- ruff lint（如果配置有）

### 5.3 .github/ISSUE_TEMPLATE/{bug,feature}.md
- 标准模板，参考 https://github.com/anthropics/anthropic-quickstarts 风格
- bug：复现步骤 / 期望行为 / 实际行为 / 环境信息（OS、Python、Diana 版本）
- feature：动机 / 期望行为 / 替代方案

### 验收
- 三个文件存在、内容齐全
- GitHub Actions yaml 语法正确

---

## 任务 6：补 docstring（限定文件清单）

### 目标
给以下文件的**公开类和函数**补一行中文 docstring（仅一行，不要多段）。私有方法（`_` 开头）不动。

### 文件清单（只动这些）
- `core/state.py`
- `core/event_bus.py`
- `tools/base.py`
- `tools/registry.py`

### 规范
- 仅补**完全没有 docstring** 的类和函数
- 已有 docstring 的**不要改**
- 一行简短描述，禁止超过 80 字
- 例：
  ```python
  def foo(x: int) -> int:
      """把 x 加 1 返回。"""
      ...
  ```

### 验收
- 全 275 测试仍过
- `git diff --stat` 改动行数应远小于 100 行（如果超过 200 行说明你又自由发挥了，回退）

---

## 任务 7：新增 provider preset 模板

### 目标
为常见但 Diana 还不支持的 LLM 平台添加 preset。

### 候选清单（按需选做）
- xAI Grok（base_url: https://api.x.ai/v1，protocol: openai_compat）
- Together AI（base_url: https://api.together.xyz/v1，protocol: openai_compat）
- Groq（base_url: https://api.groq.com/openai/v1，protocol: openai_compat）

### 文件
对每个：
- 创建 `providers/presets/{name}/preset.yaml`（照 `providers/presets/deepseek/preset.yaml` 葫芦画瓢）
- 创建 `providers/presets/{name}/tutorial/get_api_key.md`（简短中文教程，3-5 段，包含官网注册地址 + 计费说明）

### 验收
- `venv/Scripts/python -c "from providers.presets_loader import load_all_presets; from app_config import AppPaths; from pathlib import Path; print(load_all_presets(AppPaths(project_root=Path('.')).PROVIDER_PRESETS_DIR))"` 能加载新增 preset 不报错
- 全测试仍过

---

## 任务 8：CONTRIBUTING.md 的「贡献者快速指引」节

### 目标
写 CONTRIBUTING.md 的「如何加新 provider preset」和「如何加新工具」两个小节（仅这两个小节）。

### 文件
- 创建 `CONTRIBUTING.md` 框架，但只写以下两节：
  - `## 加一个 provider preset`
  - `## 加一个 Tool`
- 其它节（Code of Conduct、PR 流程等）**不要写**，留给 Claude 后续补

### 内容要求
- 步骤化（1. 2. 3.）
- 每步给出文件路径
- 给出最小代码示例
- 引用现有代码作为参考（如「照 `providers/presets/deepseek/` 葫芦画瓢」）

### 验收
- 文件存在，markdown 渲染正常
- 不写本节以外的内容

---

## 不要给 DS 做的（Claude 留着自己做）

| 任务 | 原因 |
|---|---|
| **README.md 整体** | 项目门面，需要口吻 / 排版细致 |
| **修任何已存在 bug** | 容易"顺手优化"踩雷，不可控 |
| **任何 UI 改动** | 前科：PySide6 风格难拿捏 |
| **改架构、改接口签名** | 跨模块一致性 |
| **memory/rag_store.py 实装** | 检索算法精度敏感，cosine + 归一化要对 |
| **core/runtime.py 装配 RAG** | 涉及多组件初始化顺序 |
| **agents/context_builder.py 改造** | KV 缓存命中率敏感，乱改会破坏 cache |
| **pipeline 改动** | 消息处理核心，bug 影响范围大 |
| **Persona 相关任何东西** | yuexi 私设隐私敏感 |

---

## 工作流

每完成一个任务：
1. 跑 `venv/Scripts/python -m pytest tests/ -q --ignore=tests/test_kv_cache_real.py` 确认 275 测试仍过
2. 让 binglang001 自己 review diff，**不要自己 commit**
3. 不要写"已完成 / 总结"之类的话，直接告诉 binglang001 改了哪些文件就够
