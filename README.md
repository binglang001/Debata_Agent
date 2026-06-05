<div align="center">

# Debata_Agent

**让虚拟角色「活过来」的通用聊天框架**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-431%20passing-brightgreen.svg)](#)
[![GUI](https://img.shields.io/badge/UI-PySide6-blueviolet.svg)](#)

</div>

---

> 「想让虚拟角色不只是个会回复消息的程序，而是一个**会发牢骚、会跑题、会忘事、会半夜突然问你在不在**的人。」

Debata_Agent 不绑定任何特定角色——你给它一个人格，它就活成那个人。框架名取自项目第一个被实现的角色 Debata。

## 与一般 AI 机器人的差异

| 维度 | 一般 AI | Debata_Agent |
|------|--------|-------------|
| 上下文 | 每对话独立 | **全会话统一时间线** |
| 回复 | 一条长消息 | 拆 3-7 条短消息，60% ≤12 字 |
| 节奏 | 永远秒回 | 可以「没看到」/「刚下课」/隔几小时回 |
| 边界 | 「好的呢！」 | 「？」 / 「嗯」 / 「6」 / 「算了」 |
| 风格 | 对所有人一样 | 对长辈/死党/陌生人**四种切换** |
| 主动性 | 被动回复 | 自主判断要不要开口 |
| 配置 | 改代码 | **图形向导 + 设置页即时保存** |

## 核心特性

- **真人聊天方法论** — 读了 30+ 段真实微信记录提炼的硬规则
- **关系矩阵** — 同角色对不同关系切换 4 种语气
- **真人不完美合法** — 允许跑题/改口/忘事/不回
- **十余家 LLM 提供商** — OpenAI / Anthropic Claude / DeepSeek / 智谱 GLM / 通义千问 / 火山方舟 / Gemini / Moonshot / 硅基流动 / OpenRouter
- **多渠道适配器** — 当前 NapCat (QQ)，可扩展 Discord/Telegram
- **每 Agent 独立配置** — 主聊天/主动思考/总结分别用不同模型与思考深度
- **可选高级能力** — 视觉 / ASR / TTS / 天气 / 联网搜索 / **RAG 长期记忆**
- **AES-256-GCM + RSA-2048** 加密密钥，存系统 keyring，无需密码
- **AI 辅助人格生成** — 向导引导，流式预览，多轮调整
- **现代 GUI** — 无边框圆角窗口，深浅主题，10 步配置向导，7 页仪表盘，设置页即时保存

## 快速开始

```bash
git clone https://github.com/binglang001/Debata_Agent.git
cd Debata_Agent

python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -e ".[gui]"

python main.py            # 启动（首次进配置向导）
python main.py --no-gui   # 纯 CLI 模式
```

服务器或无图形环境只跑 `--no-gui` 时，可以用 `pip install -e .` 跳过 PySide6/qasync。
本地 ASR/TTS/Embedding 的重依赖不随默认安装拉取；打开对应模型的安装指引时，Debata 会检查缺失运行包并在后台用当前 venv 补装。

### 环境要求

- **Python** 3.11 或更高
- **NapCat** [安装指南](https://napneko.github.io/guide/start-install) — QQ 协议端
- **LLM API 密钥** — 推荐 DeepSeek（中文好、价格低、KV 缓存便宜）
- Windows / macOS / Linux 均可（GUI 需要图形环境；服务器用 `--no-gui`）

### 首次配置（图形向导）

启动后会按顺序问你：
1. 选主模型（推荐路径 5 步 / 自定义路径 10 步）
2. 可选功能（视觉 / 天气 / 联网搜索 / RAG 长期记忆 …）
3. 接入 NapCat（地址 + token + 白名单）
4. 选/创建角色（内置 debata 或调 AI 现场生成）

完成后进入仪表盘——一切配置都能在设置页改，无需再跑向导。

## 它能做什么

- 接管 QQ 账号在群里 / 私聊里聊天，**看起来像本人在打字**
- 记住你告诉它的承诺（"下周记得提醒我交报告"）
- 看图、查天气、网上搜东西（按需开启）
- 长期对话自动归档总结 + 带 scope / pinned 的重要记忆注入与 RAG 召回
- 主动找你聊（"在干嘛？" — 但只在合理时刻）

## 项目结构

```
Debata_Agent/
├── adapters/        渠道适配器（NapCat / 未来 Discord / ...）
├── providers/       LLM 提供商（含 13 个预设 + 教程）
├── agents/          主聊天 / 主动思考 / 总结 / 人格生成
├── memory/          历史 + 重要记忆（文件 + RAG 向量）
├── tools/           AI 工具 + 装饰器注册
├── features/        视觉 / ASR / TTS / 天气 / 搜索 / Embedding
├── core/            事件总线 / 消息管道 / Runtime 生命周期
├── app_config/      配置 + AES/RSA 加密密钥
├── ui/              PySide6 GUI（向导 + 7 页仪表盘 + 托盘）
├── personas/        人格目录（debata 入 git，其他 gitignore）
├── utils/           CQ 解析 / KV 缓存测量 / 时间
├── docs/            开发者文档
├── tests/           431 个单元 / 集成测试
└── main.py          入口
```

## 文档

- [角色编写指南](docs/persona_writing_guide.md) — 怎么写出会"活"的人格
- [UI 风格指南](docs/ui_style_guide.md) — 中国风现代极简的设计原则
- [适配器开发](docs/adapter_development.md) — 接新的聊天平台
- [提供商开发](docs/provider_development.md) — 接新的 LLM
- [架构总览](docs/architecture.md) — 模块依赖 + KV 缓存设计
- [KV 缓存基准](docs/kv_cache_benchmark.md) — 真实命中率数据

## 开发状态

| Phase | 状态 | 说明 |
|------|------|------|
| 1.0~1.9 基础架构 / 适配器 / 提供商 / 记忆 / Agent / 工具 / 集成测试 | ✅ | |
| 2 PySide6 GUI（向导 + 仪表盘 + 托盘） | ✅ | 圆角窗口 / 即时保存 / 主题切换 |
| 3 本地模型可选插件（VoxCPM2 / 本地 embedding） | 🚧 | VoxCPM2 与本地 embedding 已接入；QQ 语音转写走 NapCat 内置能力 |
| 4 文档完善 + 开源发布（CHANGELOG / CI / 模板） | 🚧 | CHANGELOG / CI / Issue 模板已就位 |

当前全量测试覆盖 RAG / KV 缓存 / 跨模块集成 / 上下文重构链路；具体命令见归档测试流程文档。

## 一份默认人格的快速演示

`personas/debata/` 自带一个完整人格档案：豆蔻年华女孩、慢热又毒舌、对长辈尊敬、对死党随便、对陌生人冷淡。这是项目最初的样品，也是「关系矩阵 + 真人聊天」方法论的验证。你可以：

- **直接用 debata**：首次配置时选默认人格
- **AI 现场捏一个**：向导里描述「我想要一个 25 岁的程序员、嘴硬心软、爱猫」，等几秒看 AI 写出来
- **手写**：照 `personas/debata/persona_prompt.py` 葫芦画瓢，放到 `personas/{你的名字}/`

每个 persona 都有自己独立的 `data/memory/{name}/` 目录存历史与重要记忆，互不干扰。

## 设计决策

几个不打算妥协的事：

- **单一 system + XML 分区**：稳定区前置，让 KV 缓存命中率 > 90%，省 token + 省延迟
- **「真人不完美」即合法**：不修跑题/改口/忘事，那不是 bug，是体感
- **加密密钥不要密码**：用户体验优先，密钥存 OS keyring + AES-GCM，密码学保证强度
- **配置全 GUI 可改**：除了 Persona 文件本身，配置页可改一切，不强迫用户回向导

## License

[Apache 2.0](LICENSE) — 自由商用 / 修改 / 分发，保留版权与协议声明即可。

## 致谢

- [NapCat](https://github.com/NapNeko/NapCatQQ) — 现代 QQ 协议端
- [PySide6](https://wiki.qt.io/Qt_for_Python) — Qt 的 Python 绑定
- [qasync](https://github.com/CabbageDevelopment/qasync) — Qt 事件循环与 asyncio 桥接
- DeepSeek / Anthropic / 智谱 等提供 LLM 服务

---

<div align="center">

**「砚台旁有墨，纸上有空」**

</div>
