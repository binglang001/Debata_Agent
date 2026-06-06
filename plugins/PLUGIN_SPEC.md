# Debata 插件规范（Plugin Spec）

Debata 的插件用来接「本地模型 / 重资源依赖」的可选能力（TTS / 本地 embedding 等）。
主程序只依赖 `features/` 下定义的**轻量接口**，插件提供具体实现 + 重依赖。

## 目录布局

```
plugins/
├── __init__.py            # 导出 PluginManager
├── base.py                # PluginMeta / PluginManager / PluginStatus / PluginError
├── downloader.py          # 模型下载辅助
├── PLUGIN_SPEC.md         # 你正在看的这份
├── voxcpm2/
│   ├── __plugin__.py      # 必需，导出 PLUGIN_META + build(config)
│   └── voxcpm_impl.py     # 实际 TTS 实现（lazy import）
├── embedding_minilm/
│   └── __plugin__.py
└── embedding_bge_zh/
    └── __plugin__.py
```

## `__plugin__.py` 必须导出两样

```python
from plugins import PluginMeta
from features.tts import ITTSService  # 或别的 service 接口

PLUGIN_META = PluginMeta(
    name="voxcpm2",                 # 唯一标识，小写英文
    display_name="VoxCPM2（本地语音合成）",
    kind="tts",                     # 'tts' | 'embedding'
    model_dir="VoxCPM2",            # 相对路径会拼到项目 data/models/ 下；绝对路径直接用
    size_mb=3000,
    description="基于 VoxCPM2 的本地 TTS。",
    python_deps=["voxcpm>=2.0.0", "soundfile>=0.12.1"],
    download_url="https://huggingface.co/openbmb/VoxCPM2",
    config_schema={
        "model_dir": {
            "type": "string",
            "default": "data/models/VoxCPM2",
            "label": "模型目录",
            "help": "VoxCPM2 模型文件所在目录。",
        },
        "reference_audio": {
            "type": "string",
            "default": "",
            "label": "参考音频",
            "help": "3-30 秒清晰干声，用于音色克隆。留空则只用音色描述。",
        },
        "default_prompt": {
            "type": "string",
            "default": "",
            "label": "默认音色描述",
            "help": "如「年轻女性，自然口语，带一点调侃」。",
        },
        "device": {
            "type": "select",
            "default": "auto",
            "label": "设备",
            "options": ["auto", "cuda", "cpu"],
            "help": "auto 优先 cuda；无 GPU 自动回退 cpu。",
        },
        "load_denoiser": {
            "type": "bool",
            "default": False,
            "label": "启用降噪",
            "help": "需要 FFmpeg full-shared DLL 放在 data/tools/ffmpeg/bin/。",
        },
        "cfg_value": {
            "type": "float",
            "default": 2.0,
            "label": "CFG 强度",
            "help": "引导强度。越大越贴近描述但可能失真。",
        },
        "inference_timesteps": {
            "type": "int",
            "default": 10,
            "label": "推理步数",
            "help": "10-30 之间比较合适。越多音质越好但越慢。",
        },
    },
    auto_download=False,            # 历史兼容字段；当前 UI 统一显示「安装指引」
)


def build(config: dict) -> ITTSService:
    """返回 ITTSService 实例。config 是 UI 表单填的值。"""
    from .voxcpm_impl import VoxCPM2Service  # lazy import 避免主程序 import 失败
    return VoxCPM2Service(model_dir=config.get("model_dir", "data/models/VoxCPM2"))
```

## 实装侧契约

- **build() 不加载模型**：`build(config)` 只创建轻量 service 实例，不做任何模型 I/O。真正加载在 `warmup()` 里，由 Runtime 启动时用 `asyncio.to_thread` 后台执行。
- **必须实现对应 `features/` 接口**：
  - kind=`tts`  → `features.tts.ITTSService`
  - kind=`embedding` → `features.embedding.IEmbeddingService`
- **必须有 `warmup()` 方法**：异步方法，负责后台加载模型文件。幂等（多次调用不会重复加载）。主业务方法（如 `synthesize()`）内部 `await self._ready.wait()` 等待 warmup 完成。
- **必须有 `aclose()`**：哪怕是 no-op；`PluginManager.shutdown_all()` 会调。
- **重依赖只在 `build()` 内部做 lazy import**：`__plugin__.py` 顶层只能 import 标准库 + `plugins.base` + `features.{kind}`。`torch` / `voxcpm` 等重依赖用 `importlib.util` 动态导入，不在顶层 import。
- **模型文件不入仓库**：`plugins/{name}/` 只放代码（`.py`）。模型文件放 `data/models/{model_dir}/`。

## 状态机

```
NOT_INSTALLED  ──按安装指引手动放模型──>  INSTALLED  ──build()──>  ENABLED
                                       ▲                       │
                                       └───── shutdown() ──────┘

  ERROR  ← 任何阶段失败都跳到这里，error 字段记简述
```

UI（模型管理页）渲染状态时：
- NOT_INSTALLED：灰色 / 「未安装」/ 「安装指引」按钮
- INSTALLED：白色 / 「未启用」/ 「启用」按钮（弹详情页填配置 → 调 PluginManager.build()）
- ENABLED：青瓷青 / 「使用中」/ 「停用」按钮
- ERROR：朱砂红 / 「错误」/ hover tooltip 显示 error

## Runtime 与插件的对接

Runtime 在装配 features 时按 `features.tts.type/plugin` 决定要不要 build：

```python
# core/runtime.py::_setup_plugins
if cfg.features.tts.enabled and cfg.features.tts.type == "local":
    pm = self.plugin_manager  # 启动时已 scan()
    plugin_name = "voxcpm2"
    self.tts = pm.build(plugin_name, {})
```

`features.{tts,embedding}.enabled / type` 字段已在 schema 定义，由 Runtime._setup_plugins 读取。

## 工具系统挂钩（仅 TTS）

`tools/feature_tools.py` 会按 `ctx.tts is None` 决定 `send_voice_message` 是否实际工作；
`tools/__init__.py::build_default_registry` 按 `features.tts.enabled` 决定要不要注册该工具。

ASR 已从项目中移除。QQ 语音转写走 NapCat 内置 `fetch_ptt_text`，无需 Debata 侧再做语音识别。

## 给开发者的清单

写新插件时：
1. 在 `plugins/{你的插件名}/__plugin__.py` 按上面格式写 `PLUGIN_META` 和 `build(config)`
2. 实装类放在同目录其它 `.py`，lazy import
3. 模型文件放 `data/models/{你的 model_dir}/`
4. 启动 Debata → 模型管理页应能看到你的插件，并能打开安装指引
5. 设置页对应 feature 节会出现你的插件名（下拉选择），在这里启用并配置
