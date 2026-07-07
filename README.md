# Debata_Agent

### **或许，`/v1/chat/completions`后面的，是个人？**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](https://github.com/binglang001/Debata_Agent/actions/workflows/test.yml)
[![GUI](https://img.shields.io/badge/UI-PySide6-blueviolet.svg)]
[![Stars](https://img.shields.io/github/stars/binglang001/Debata_Agent?style=social)](https://github.com/binglang001/Debata_Agent)

---

> 「虚拟角色不只是个会回复消息的程序，而是一个**真实的，能和你聊天的，有个性、有内心的**的人.」

框架名取自项目第一个被实现的角色 Debata. Debata_Agent 不绑定任何特定角色，你给它一个人格，它就活成那个人.

[![Debata 人设图](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/personas/debata/角色人设主图.png)]

*Debata —— 17 岁，短黑发，灰色卫衣，对亲近的人毒舌，但心里十分在意.*

### 目录

- [差异](#与一般-ai-机器人的差异) · [功能(hyperlink)](docs/features.md) · [项目部署](#项目部署) · [项目结构](#项目结构) · [文档](#文档) · [设计决策](#设计决策) · [License](#license)

-----

## 与一般 AI 机器人的差异

| 维度  | 一般 AI  | Debata_Agent            |
| --- | ------ | ----------------------- |
| 上下文 | 每对话独立  | **全会话统一时间线 + 状态/关系上下文** |
| 回复  | 一条长消息  | 拆 3-7 条短消息，60% ≤12 字    |
| 节奏  | 永远秒回   | 可以「没看到」/「刚下课」/隔几小时回     |
| 边界  | 「好的！」  | 「？」 / 「嗯」 / 「6」 / 「算了」  |
| 风格  | 对所有人一样 | 对长辈/死党/陌生人**四种切换**      |
| 主动性 | 被动回复   | 自主判断要不要开口               |
| 配置  | 改代码    | **图形向导 + 设置页即时保存**      |

## 项目部署

### 环境要求

- **Python** 3.11 或更高
- **NapCat**  — QQ 协议端 注:[安装指南(hyperlink)](https://napneko.github.io/guide/start-install)
- **LLM API 密钥** — 推荐 DeepSeek（中文好、价格低、缓存命中后便宜）
- Windows / macOS / Linux 均可（GUI 需要图形环境; 服务器用 `--no-gui`）

### 部署命令 (各OS通用)

```bash
git clone https://github.com/binglang001/Debata_Agent.git
cd Debata_Agent
python -m pip install uv
uv sync
uv sync --extra gui # 如果不需要图形化(例如服务器/docker)，这一步可以省略
uv run main.py
```

服务器或无图形环境，可以跳过`uv sync --extra gui`，在`uv run main.py`后加`--no-gui`.

本地 ASR/TTS/Embedding 的重依赖不随默认安装拉取; 打开对应模型的安装指引时，Debata 会检查缺失运行包并在后台用当前 venv 补装.

你也可以使用`uv sync --extra <你要的功能>`来提前安装，这与系统安装的效果相同，模块详见下表

### 可选模块

| 模块   | 做什么            | 需要什么                      |
| ---- | -------------- | ------------------------- |
| 视觉识别 | 看懂图片、提取文字、分析表情 | 多模态模型（GLM-4V / Qwen-VL 等） |
| 语音合成 | 用声音回复          | 本地 VoxCPM2 或云端 TTS        |
| 天气查询 | 查实时天气和预报       | 和风天气 API Key              |
| 联网搜索 | 搜实时信息          | 免费，走 DuckDuckGo           |

项目自带语音处理功能:QQ 语音转写使用 NapCat 内置功能.

### 首次配置（图形向导）

启动后会按顺序问你:

1. 选主模型（推荐路径 7 步 / 自定义路径最多 9 步）
2. 可选功能（视觉 / 天气 / 联网搜索 / RAG 长期记忆 …）
3. 接入 NapCat（地址 + token + 白名单）
4. 选/创建角色（内置 debata 或调 AI 现场生成）

完成后进入仪表盘，常用配置能在设置页调整，无需再跑向导.

## 项目结构

```
Debata_Agent/
├── adapters/        渠道适配器（NapCat）
├── providers/       LLM 提供商（含 13 个预设 + 教程）
├── agents/          主聊天 / 主动思考 / 总结 / 人格生成 / 人格状态管理
├── mind/            人格状态 / 用户画像 / 亲近度 / 短期线索 / 后台审计存储
├── memory/          历史 + 重要记忆（文件 + RAG 向量）
├── tools/           AI 工具 + 装饰器注册
├── features/        视觉 / TTS / 天气 / 搜索 / Embedding
├── core/            事件总线 / 消息管道 / Runtime 生命周期
├── app_config/      配置 + AES/RSA 加密密钥
├── ui/              PySide6 GUI（向导 + 7 页仪表盘 + 托盘）
├── plugins/         本地模型插件（TTS / Embedding）
├── personas/        人格目录（debata 入 git，其他 gitignore）
├── utils/           CQ 解析 / KV 缓存测量 / 时间
├── docs/            开发者文档
├── tests/           单元 / 集成测试
└── main.py          入口
```

## 文档

- [图文教程](docs/getting_started.md) — 从零开始，截图指引每一步
- [角色编写指南](docs/persona_writing_guide.md) — 怎么写出会"活"的人格
- [架构总览](docs/architecture.md) — 模块依赖 + KV 缓存设计
- [KV 缓存基准](docs/kv_cache_benchmark.md) — 真实命中率数据
- [提供商开发](docs/provider_development.md) — 接新的 LLM
- [适配器开发](docs/adapter_development.md) — 接新的聊天平台
- [UI 风格指南](docs/ui_style_guide.md) — 设计原则与组件规范

## 设计决策

几个不打算妥协的事:

- **单一 system + XML 分区**:稳定区前置，让 KV 缓存命中率 > 90%，省 token + 省延迟
- **「真人不完美」即合法**:不修跑题/改口/忘事，那不是 bug，是体感
- **加密密钥不要密码**:用户体验优先，密钥存 OS keyring + AES-GCM，密码学保证强度
- **常用配置尽量 GUI 可改**:常用开关和密钥走设置页即时保存，Persona 文件仍然保留手写空间

## License

[Apache 2.0](LICENSE) — 自由商用 / 修改 / 分发，保留版权与协议声明即可.

## 致谢

- [NapCat](https://github.com/NapNeko/NapCatQQ) — 现代 QQ 协议端
- [PySide6](https://wiki.qt.io/Qt_for_Python) — Qt 的 Python 绑定
- [qasync](https://github.com/CabbageDevelopment/qasync) — Qt 事件循环与 asyncio 桥接
- DeepSeek / Anthropic / 智谱 等提供 LLM 服务

---

如果这个项目对你有用，欢迎给个 [Star](https://github.com/binglang001/Debata_Agent)，这对我们真的很重要，球球啦qwq
