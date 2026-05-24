<div align="center">

# Diana_Agent

**让虚拟角色「活过来」的通用聊天框架**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)

</div>

---

> 「想让虚拟角色不只是个会回复消息的程序，而是一个**会发牢骚、会跑题、会忘事、会半夜突然问你在不在**的人。」

Diana_Agent 不绑定任何特定角色——你给它一个人格，它就活成那个人。框架名取自项目第一个被实现的角色 Diana。

## 与一般 AI 机器人的差异

| 维度 | 一般 AI | Diana_Agent |
|------|--------|-------------|
| 上下文 | 每对话独立 | **全会话统一时间线** |
| 回复 | 一条长消息 | 拆 3-7 条短消息，60% ≤12 字 |
| 节奏 | 永远秒回 | 可以"没看到"/"刚下课"/隔几小时回 |
| 边界 | "好的呢！" | "？" / "嗯" / "6" / "算了" |
| 风格 | 对所有人一样 | 对长辈/死党/陌生人**四种切换** |
| 主动性 | 被动回复 | 自主判断要不要开口 |
| 配置 | 改代码 | **图形向导 + 托盘** |

## 核心特性

- **真人聊天方法论**——读了 30+ 段真实微信记录提炼的硬规则
- **关系矩阵**——同角色对不同关系切换 4 种语气
- **真人不完美合法**——允许跑题/改口/忘事/不回
- **多 LLM 提供商**——OpenAI/Anthropic/Gemini/火山/DeepSeek/GLM/Moonshot/Qwen 等十余家
- **多渠道适配器**——当前 NapCat (QQ)，可扩展 Discord/Telegram
- **每 Agent 独立配置**——主聊天/主动思考/总结分别用不同模型
- **可选高级功能**——视觉/ASR/TTS/天气/搜索/RAG 长期记忆
- **AES-256-GCM + RSA-2048** 加密密钥，存系统 keyring，无需密码
- **AI 辅助人格生成**——向导引导，多轮调整

## 快速开始

```bash
git clone https://github.com/{your-org}/Diana_Agent.git
cd Diana_Agent
python -m venv venv
venv/Scripts/activate  # macOS/Linux: source venv/bin/activate
pip install -e .
diana                  # 启动（首次进配置向导）
diana --no-gui         # 纯 CLI 模式
```

需要：Python 3.10+、[NapCat](https://napneko.github.io/guide/start-install)、一个 LLM 密钥（推荐 DeepSeek）。

## 项目结构

```
Diana_Agent/
├── adapters/        渠道适配器（NapCat/Discord/...）
├── providers/       LLM 提供商
├── agents/          主聊天/主动思考/总结/人格生成
├── memory/          历史 + 重要记忆（文件 + 可选 RAG）
├── tools/           17 个工具 + 装饰器注册
├── features/        视觉/语音/天气/搜索/Embedding
├── core/            事件总线/消息管道/生命周期
├── app_config/      配置 + 加密密钥
├── ui/              PySide6 GUI
├── personas/        人格目录（diana 入 git，其他 gitignore）
├── utils/           CQ 解析 / KV 缓存测量 / 时间
├── docs/            文档
├── tests/           单元测试（273 个）
└── main.py          入口
```

## 文档

- 📖 [角色编写指南](docs/persona_writing_guide.md)
- 🎨 [UI 风格指南](docs/ui_style_guide.md)
- 🔌 [适配器开发](docs/adapter_development.md)
- ⚙️ [提供商开发](docs/provider_development.md)
- 🏛️ [架构总览](docs/architecture.md)

## 开发状态

| Phase | 状态 |
|------|------|
| 1.0-1.7 基础架构/适配器/提供商/记忆/Agent/工具 | ✅ |
| 1.8 拆分 handler.py | 🚧 |
| 1.9 main.py + 性能优化 | 🚧 |
| 1.10 集成测试 + KV 缓存实测 + 清理 | 🚧 |
| 2 PySide6 GUI | ⏸️ |
| 3 本地模型集成 | ⏸️ |
| 4 文档完善 + 开源发布 | ⏸️ |

当前 **273/273 单元测试通过**。

## License

[Apache 2.0](LICENSE)

---

<div align="center">

**「砚台旁有墨，纸上有空」**

</div>
