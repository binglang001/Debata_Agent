# 图文教程

从零开始，把 Debata 跑起来。遇到问题先看文末的 [常见问题](#常见问题)。

---

## 1. 安装 NapCat

Debata 通过 [NapCat](https://napneko.github.io/guide/start-install) 接入 QQ。先到 NapCat 官网下载安装，按教程跑起来。

NapCat 启动后，记下它的 WebSocket 地址和 token（如果设了的话）——下一步要用。

---

## 2. 安装 Debata

```bash
git clone https://github.com/binglang001/Debata_Agent.git
cd Debata_Agent

python -m venv venv
venv\Scripts\activate       # Windows
source venv/bin/activate    # macOS / Linux

pip install -e ".[gui]"
python main.py
```

首次启动会自动进入配置向导。以后启动直接进仪表盘。服务器跑 `python main.py --no-gui` 跳过 GUI。

环境要求：Python 3.11+，一个 LLM API 密钥（推荐 [DeepSeek](https://platform.deepseek.com)）。

Linux 服务器或 SSH 环境可以直接跑纯 CLI 向导：

```bash
python main.py --no-gui --setup
```

CLI 向导覆盖主模型、子 Agent、图片理解、天气、TTS、RAG、NapCat 和人格等主要配置。密钥优先保存到系统 keyring；如果 Linux 没有可用 Secret Service，会自动退回 `data/rsa_private.pem` 本地私钥文件，并使用 0600 权限保护。

---

## 3. 走完向导

推荐路径 7 步，约 5 分钟。每一步都可以"上一步"回去改。

### 第一步：选路径

![欢迎页](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_welcome.png)

推荐路径用 DeepSeek 主模型快速上手。想逐个调校每个参数选自定义。

### 第二步：主模型

![主模型](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_model_quick.png)

填入 [DeepSeek API 密钥](https://platform.deepseek.com/api_keys)，点「测试连接」验证。推荐先充 5 块钱，够测很久。

自定义路径可以选其他 provider，调 temperature、top_p 等参数。

![自定义模型](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_model_custom.png)

### 第三步：可选功能

![功能开关](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_features.png)

4 个可选功能，按需打开。以后在设置页随时能改：

- **看懂图片**（[Vision](feature_guides/vision.md)）：需要多模态模型，比如 GLM-4V 或 Qwen-VL
- **查天气**（[和风天气](feature_guides/weather.md)）：需要去和风控制台申请 API Key
- **用声音说话**（[TTS](feature_guides/tts_voxcpm.md)）：本地 VoxCPM2 或云端 TTS
- **联网搜索**（[DuckDuckGo](feature_guides/web_search.md)）：免费，不需要密钥，建议开启

### 第四步：记忆方式

![记忆方式](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_embedding.png)

选 [长期记忆](feature_guides/embedding_rag.md) 的工作方式：

- **文件模式**：零配置，直接写 JSON 文件，AI 调工具保存。单人聊天够用。
- **向量模式（RAG）**：配一个 embedding 服务，后台自动提取记忆做语义检索。多群多用户长期运行更靠谱。

### 第五步：接 NapCat

![NapCat](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_adapter.png)

在NapCat创建Server后（注意：NapCat填Server意味着程序要选择Client模式），填入 NapCat 的 WebSocket 地址和 token。白名单建议选「管理员审核」——陌生人加好友时你手动确认，避免意外费用。

### 第六步：选角色

![人格选择](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_persona.png)

三种方式：

- **仓库自带**：直接用 Debata，开箱即用
- **AI 辅助创建**：描述你想要的角色，AI 帮你写完整的 [人格档案](persona_writing_guide.md)
- **导入**：你已经写好了 `persona_prompt.py`，从磁盘导入

### 确认 & 启动

![确认](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/wizard_summary.png)

检查配置没问题，点「启动」。以后在 [设置页](#改配置) 随时能改。

---

## 4. 仪表盘

![总览](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/dashboard_overview.png)

左边 7 个导航：

| 页面 | 做什么 |
|------|--------|
| 总览 | 渠道状态、模型健康、用量、KV 缓存命中率 |
| 对话 | 看历史对话，展开 AI 的思考过程 |
| 记忆 | 管理 Debata 记住的事（scope / pinned / 编辑 / 删除） |
| 日志 | 实时日志流，按等级和模块过滤 |
| 角色 | 切换、新建、修改、导入导出人格 |
| 模型管理 | 本地模型安装状态和指引 |
| 设置 | 所有配置实时保存 |

---

## 5. 常见操作

### 切换人格

[角色页](#仪表盘) → 选中目标 → 点「切换为当前」→ 重启 Debata。

### 加表情包

把自己收藏的图片（png / jpg / gif）放进 `data/emoji/` 目录。文件名就是 AI 引用时的名字，建议起容易懂的（如「笑哭」「困」）。设置页表情包区也可以拖放添加。

![表情包](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/dashboard_settings_emoji.png)

### 改模型

![设置模型](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/dashboard_settings_model.png)

设置页 → 模型 → 改 Provider 密钥或 Agent 模型。点「测试连接」确认可用。

### 调上下文预算

设置页 → Token预算 → 滚动摘要压缩，可以调触发滚动摘要的上下文占比、压缩后的活跃窗口目标，以及第一次压缩仍超预算时的重试压缩目标。正常使用建议保留默认值；这里不再提供“保留最近 N 条工作历史”一类旧裁剪开关。

### 调主题

![外观](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/dashboard_appearance.png)

设置页 → 软件行为 → 外观 → 深浅主题即时切换。

---

## 6. 常见问题

**NapCat 连不上？**
确认 NapCat 是否在跑，WS 地址和端口是否正确。设置页的「测试连接」可以诊断。

**API 密钥无效？**
确认密钥复制完整（sk- 开头），账户余额是否充足。

**没收到消息？**
检查白名单模式——如果是「白名单」模式且你的 QQ 不在名单里，Debata 不会回复。切到「管理员审核」试试。

**日志刷屏？**
设置页 → 日志与诊断 → 日志级别切到 INFO 或 WARNING。排查问题时再开 DEBUG。

**改配置不生效？**
部分配置（模型、Adapter、功能开关等）改完需要重启 Debata 服务。设置页底部有重启按钮。

---

## 更多

- [架构总览](architecture.md)
- [角色编写指南](persona_writing_guide.md)
- [KV 缓存实测报告](kv_cache_benchmark.md)
- [提供商开发指南](provider_development.md)
- [适配器开发指南](adapter_development.md)
