<div align="center">

# Debata_Agent

**让虚拟角色「活过来」的通用聊天框架**

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](https://github.com/binglang001/Debata_Agent/actions/workflows/test.yml)
[![GUI](https://img.shields.io/badge/UI-PySide6-blueviolet.svg)](#)
[![Stars](https://img.shields.io/github/stars/binglang001/Debata_Agent?style=social)](https://github.com/binglang001/Debata_Agent)

<br>

<img src="https://raw.githubusercontent.com/binglang001/Debata_Agent/main/ui/icon.png" width="128" alt="Debata">

</div>

---

> 「想让虚拟角色不只是个会回复消息的程序，而是一个**真实的、能在QQ和你聊天的、有个性的、有内心的**的人。」

Debata_Agent 不绑定任何特定角色，你给它一个人格，它就活成那个人。框架名取自项目第一个被实现的角色 Debata。

<div align="center">
<img src="https://raw.githubusercontent.com/binglang001/Debata_Agent/main/personas/debata/角色人设主图.png" width="600" alt="Debata 人设图">

*Debata —— 17 岁，短黑发，炭灰色卫衣。有主见，对亲近的人毒舌但心里十分在意。*
</div>

- [差异](#与一般-ai-机器人的差异) · [功能](#功能) · [快速开始](#快速开始) · [部署指南](#部署指南) · [项目结构](#项目结构) · [文档](#文档) · [设计决策](#设计决策) · [License](#license)

## 与一般 AI 机器人的差异

| 维度 | 一般 AI | Debata_Agent |
|------|--------|-------------|
| 上下文 | 每对话独立 | **全会话统一时间线 + 状态/关系上下文** |
| 回复 | 一条长消息 | 拆 3-7 条短消息，60% ≤12 字 |
| 节奏 | 永远秒回 | 可以「没看到」/「刚下课」/隔几小时回 |
| 边界 | 「好的呢！」 | 「？」 / 「嗯」 / 「6」 / 「算了」 |
| 风格 | 对所有人一样 | 对长辈/死党/陌生人**四种切换** |
| 主动性 | 被动回复 | 自主判断要不要开口 |
| 配置 | 改代码 | **图形向导 + 设置页即时保存** |

## 功能

### 真人聊天，不是客服

Debata 的聊天风格来自 30 多段真实微信记录。它会把一段话拆成 3-7 条短消息瀑布式发出去，60% 的消息不超过 12 个字。不打句号、不说再见、聊完就停。对不同的人自动切换语气：对死党毒舌互损，对长辈恭敬，对陌生人礼貌但有距离。跑题、改口、忘事、已读不回，这些都可以做到。

### 表情包

把你的表情包放进 `data/emoji/` 目录，AI 会按对话情绪自动选图发送。文件名就是 AI 看到的名字，建议起容易懂的（比如「笑哭」而不是 `emoji_0142`）。设置页提供拖放添加、缩略图预览、重命名和删除。

### 长期记忆

Debata 会记住你在对话中告诉它的事：承诺、约定、偏好、身份信息。每条记忆带 scope（全局 / 某个人 / 某个群）和 pinned 标记。支持两种模式：

- **文件模式**：直接写 JSON，零配置，完全透明。单人聊天够用。
- **RAG 向量模式**：后台自动提取记忆做语义检索，长期多群运行更准。

记忆页可以查看、编辑、手动添加、导入导出。

### 人格管理与后台心智

0.9.0 把人格从一段静态提示词推进到可持续维护的角色状态：角色支持年龄档位和年龄提示，也会记录精力、饱腹、心情、社交需求等日常状态。睡醒/进食结束时会触发模型恢复评估，失败时用公式兜底；后台定时维护默认 30 分钟，可在设置里调整。

聊天时，人格代理会拿到整理后的结构化上下文：事件、聊天现场、当前状态、对象画像、画像审计、短期影响、线索、待办和相关长期记忆都会参与判断语气、主动性与后续记忆。聊天现场使用有边界的 episode 片段，不会无限追加。后台维护带审计记录，UI 也能观察状态变化、恢复来源和维护结果，辅助排查人格后台行为。

### 可选模块

| 模块 | 做什么 | 需要什么 |
|------|--------|---------|
| 视觉识别 | 看懂图片、提取文字、分析表情 | 多模态模型（GLM-4V / Qwen-VL 等） |
| 语音合成 | 用声音回复 | 本地 VoxCPM2 或云端 TTS |
| 天气查询 | 查实时天气和预报 | 和风天气 API Key |
| 联网搜索 | 搜实时信息 | 免费，走 DuckDuckGo |

项目自带语音处理功能：QQ 语音转写使用 NapCat 内置功能。

### 多 Agent 协作

框架内部跑着几类 Agent，可以分别绑不同的模型：

- **主聊天**：处理每条消息，调用工具，跑多轮工具循环
- **主动思考**：后台定时判断要不要主动开口，用小模型省成本
- **历史总结**：定期把老对话压成摘要存进长期记忆
- **人格管理**：维护年龄档位、日常状态、用户关系和短期线索，向聊天提供结构化上下文

### 13 家 LLM 提供商

OpenAI / Anthropic Claude / DeepSeek / 智谱 GLM / 通义千问 / 火山方舟 / Gemini / Moonshot / 硅基流动 / OpenRouter / Groq / Together / xAI。新增 provider 只需写一个 preset.yaml，无需写代码。

### 安全

API 密钥用 AES-256-GCM + RSA-2048 加密，存系统 keyring，不需要记密码。配置文件即时保存，大部分改动不需要重启。

### GUI

无边框圆角窗口，深浅主题。图形化配置向导（推荐路径 7 步、自定义最多 9 步），7 页仪表盘，设置页实时保存。托盘常驻，左键打开仪表盘，右键菜单。

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
1. 选主模型（推荐路径 7 步 / 自定义路径最多 9 步）
2. 可选功能（视觉 / 天气 / 联网搜索 / RAG 长期记忆 …）
3. 接入 NapCat（地址 + token + 白名单）
4. 选/创建角色（内置 debata 或调 AI 现场生成）

完成后进入仪表盘，常用配置能在设置页调整，无需再跑向导。

## 项目结构

```
Debata_Agent/
├── adapters/        渠道适配器（NapCat / 未来 Discord / ...）
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

## 部署指南

### Windows 桌面

```bash
git clone https://github.com/binglang001/Debata_Agent.git
cd Debata_Agent
python -m venv venv
venv\Scripts\activate
pip install -e ".[gui]"
python main.py
```

首次启动进入配置向导。之后每次启动直接进仪表盘，托盘常驻。

### Linux 服务器（无头）

```bash
git clone https://github.com/binglang001/Debata_Agent.git
cd Debata_Agent
python -m venv venv
source venv/bin/activate
pip install -e .              # 跳过 GUI 依赖
python main.py --no-gui
```

systemd 服务（`/etc/systemd/system/debata.service`）：

```ini
[Unit]
Description=Debata Agent
After=network.target

[Service]
Type=simple
User=debata
WorkingDirectory=/opt/Debata_Agent
ExecStart=/opt/Debata_Agent/venv/bin/python main.py --no-gui
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 常见问题

- **NapCat 连不上**：确认 NapCat 已启动，WS 地址和端口正确。设置页「测试连接」可诊断。
- **API 密钥无效**：确认复制完整（sk- 开头），账户余额充足。
- **没收到消息**：检查白名单模式，切到「管理员审核」试试。
- **日志刷屏**：设置页 → 日志与诊断 → 日志级别切到 INFO。

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
- **常用配置尽量 GUI 可改**：常用开关和密钥走设置页即时保存，Persona 文件仍然保留手写空间

## License

[Apache 2.0](LICENSE) — 自由商用 / 修改 / 分发，保留版权与协议声明即可。

## 致谢

- [NapCat](https://github.com/NapNeko/NapCatQQ) — 现代 QQ 协议端
- [PySide6](https://wiki.qt.io/Qt_for_Python) — Qt 的 Python 绑定
- [qasync](https://github.com/CabbageDevelopment/qasync) — Qt 事件循环与 asyncio 桥接
- DeepSeek / Anthropic / 智谱 等提供 LLM 服务

---

<div align="center">

如果这个项目对你有用，欢迎给个 [Star](https://github.com/binglang001/Debata_Agent) ⭐

</div>
