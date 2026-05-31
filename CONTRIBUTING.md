# 贡献指南

感谢愿意为 Debata_Agent 添砖加瓦。本文写给希望提 PR、加 provider、加工具、报 bug 或重写文档的人。
读完前 3 节即可开始动手；后两节是「加 provider」「加 Tool」的实操指引。

---

## 项目准则

Debata 不是 ChatGPT 套壳，也不是「能多就多」的 AI 全家桶。她有边界、有取舍。

**保留的：**
- 全会话统一上下文（不隔离不同用户/群聊）
- 真人聊天形态（拆条、不告别、风格随关系切换、允许跑题/改口/忘事）
- KV 缓存友好的单一 system + XML 分区
- 加密密钥不要密码（AES-GCM + RSA + keyring，密码学保证强度）
- 用户配置全 GUI 可改（除 persona 文件本身）

**不接受的 PR：**
- 把 Debata 改成「全平台多账号 SaaS」
- 加「礼貌话术润色器」「敬语自动检测」之类破坏人格的「礼貌补丁」
- 把单一 system prompt 拆成多 system 段（破坏 KV 缓存）
- 引入「记忆数据库 ORM 层」「插件市场后端」等过度抽象
- 复杂的 fallback / retry / 自愈机制 — 错误应直接报，让用户/开发者知道

**始终欢迎：**
- 新 LLM provider preset（10 行 YAML + 教程）
- 新 adapter（Discord / Telegram / 微信...）
- 新插件（本地模型、ASR/TTS、embedding 等）
- 新工具（按现有工具系统的装饰器风格）
- 文档与文案润色（特别是英文 i18n）
- 单元测试 / 集成测试加固

---

## PR 流程

1. **先开 Issue 或在已有 Issue 上 +1**。除非是 typo 修复或文案微调，不要默写大改动 PR — 容易白做。
2. **fork → 拉 develop 分支**。`main` 只接 release 提交；日常开发都在 `develop`。
3. **写代码 + 测试**。
   - 改了行为：补回归测试
   - 加了新功能：先写用例测试，再实现到通过
   - 不要直接 commit 到 `main`
4. **安装测试依赖并跑全部测试**：`pip install -e ".[dev,gui]"` 后执行 `venv/Scripts/python -m pytest tests/ -q --ignore=tests/test_kv_cache_real.py`，确保全过。
5. **提 PR 到 `develop` 分支**。
   - PR 标题：动词开头，一句话讲明白做了什么（如「fix: 修白名单 verify 模式漏判群消息」）
   - PR 描述写：动机 + 变更点 + 自测清单
6. **绝对不要写 `Co-Authored-By: Claude <...>` 或类似 AI 协作署名**。所有 commit 只署人类作者。
7. **不要 squash / amend 别人的 commit**。
8. **PR 内不要塞无关改动**。一个 PR 解决一件事。

我会尽快 review；如果两周没动静请 ping 一下。

---

## Code Style

项目用 [`ruff`](https://docs.astral.sh/ruff/) 做 lint 与格式化。`pyproject.toml` 里有 `[tool.ruff]` 配置。
本地跑：
```bash
venv/Scripts/python -m ruff check .
venv/Scripts/python -m ruff format .
```

**约定（在 ruff 之外）：**

- **中文注释 + 英文标识符**。注释直接写中文；变量/函数/类名英文。
- **不写多段 docstring**。除非接口很复杂，一行简短中文足够。
- **不写「设计意图史」注释**。代码读出来就知道；动机写在 PR / commit message。
- **不引入新依赖前 grep 一下**。可能已有同效用的库。新依赖加到 `pyproject.toml` 时分组要清楚（核心 / dev / optional）。
- **异步优先**：所有 I/O（HTTP / 文件 / 数据库）都走 async。CPU 密集任务用 `asyncio.to_thread`。
- **Pydantic v2**：所有配置 / 工具 args 用 Pydantic 校验，禁用 `extra="allow"`。
- **路径用 `pathlib.Path`**，不用 os.path 字符串拼接。
- **日志用 `logging`，不用 print**。日志级别：DEBUG（开发追踪）/ INFO（启动 + 关键流转）/ WARNING（可降级失败）/ ERROR（致命）。
- **禁止 try-bare-except**。捕异常要写具体类型，BLE001 (`Exception`) 必加 `# noqa: BLE001` 并写明原因。
- **不在 commit message 加 emoji**（README / 文案除外）。

---

## 加一个 provider preset

1. **在 `providers/presets/{name}/` 下新建 `preset.yaml`**。最少填 `id` / `display_name` / `protocol` / `base_url` / `models` 列表。照 `providers/presets/deepseek/preset.yaml` 葫芦画瓢即可。示例：
   ```yaml
   id: my_platform
   display_name: My Platform
   protocol: openai_compat
   base_url: https://api.myplatform.com/v1
   registration_url: https://myplatform.com/console
   reasoning_style: none
   models:
     - id: my-model-v1
       display_name: My Model V1
       capabilities: [chat, tool_call]
       context_length: 131072
   ```
   `reasoning_style` 可选值：`none`（无思考）、`thinking_extra_body`（DeepSeek 风格）、`thinking_extra_header`（Anthropic 风格）。

2. **写 `providers/presets/{name}/tutorial/get_api_key.md`**。3~5 段简短中文教程，覆盖：官网注册地址 → 如何获取 API Key → 计费说明 → 填到 Debata 的步骤。

3. **验证**：启动 Debata → 向导或设置页 Model 节，下拉菜单应能看到新 provider。或用脚本：
   ```bash
   venv/Scripts/python -c "from providers.presets_loader import load_all_presets; from app_config import AppPaths; from pathlib import Path; print(load_all_presets(AppPaths(project_root=Path('.')).PROVIDER_PRESETS_DIR).keys())"
   ```

---

## 加一个 Tool

1. **在 `tools/schemas.py` 加 Pydantic args 模型**。继承 `_ToolArgs`（已在 schemas.py 顶部定义；核心约束：`extra="forbid"` + `strict=False`）。示例：
   ```python
   class MyToolArgs(_ToolArgs):
       target_id: str = Field(..., description="目标 QQ 号")
       message: str = Field(..., description="要发送的内容", min_length=1, max_length=2000)
   ```

2. **在对应分类文件里加 `@tool(...)` 函数**。分类文件对应：
   - `tools/messaging.py`：消息类（send / recall / upload）
   - `tools/memory_tools.py`：重要记忆类（保存 / 删除）
   - `tools/platform_tools.py`：平台类（好友列表 / 群信息 / 验证请求 / 历史召回与总结等）
   - `tools/control_tools.py`：控制类（no_action / 延迟唤醒）
   - `tools/feature_tools.py`：功能类（天气 / 搜索 / 视觉 / 语音 等）
   - `tools/workspace_tools.py`：文件类（read / write / edit / run_python 等）

   函数签名固定为 `async def my_tool(args: MyToolArgs, ctx: ToolContext) -> dict`。返回值固定 `{"ok": True, ...}` 或 `{"ok": False, "error": "..."}`。照 `tools/feature_tools.py` 的 `web_search` 葫芦画瓢。

3. **若需要 ToolContext 新字段**：在 `tools/base.py::ToolContext` 加字段 → 在 `core/runtime.py` 装配时注入值。不要直接在工具函数内 import 全局单例。

4. **验证**：启动 Debata → 设置页确认工具已出现在 schema 中（按 feature 开关决定是否暴露）。运行 pytest 确保全过。

---

## 报 bug

用 [Issue Template](.github/ISSUE_TEMPLATE/bug_report.md)。最少给：
- 复现步骤
- 期望行为 vs 实际行为
- 环境（OS、Python 版本、Debata 版本/commit hash）
- 相关日志（脱敏后）

---

## License

提交即代表你同意你的贡献以 [Apache-2.0](LICENSE) 协议被合入。
