# Context Rework Phase D/E 全量流程测试

日期：2026-05-30

## 覆盖范围

- Phase D：重要记忆 `scope` / `pinned` 元数据、按会话注入、RAG scope 过滤、记忆页编辑。
- Phase E：`summarize_conversation` 本地归档总结，以及与 `summarize_chat_history` 的职责区分。
- 回归：主动思考路由小上下文、发送回执链路、工具结果精简、KV 缓存友好的 append-only 历史语义。

## 自动化验证

在仓库根目录执行：

```powershell
venv\Scripts\python -m compileall memory tools core ui agents tests -q
venv\Scripts\python -m pytest tests\ -q --ignore=tests\test_kv_cache_real.py
```

验收标准：

- `compileall` 无输出且退出码为 0。
- pytest 全部通过。
- `tests/test_kv_cache_real.py` 仍单独保留给真实模型/真实缓存环境，不纳入常规全量。

## 机器人手动测试

以下步骤都用同一个机器人实例测试。建议先清理或临时换一个测试 persona，避免旧记忆影响判断。

### 1. 当前会话 scope 默认保存

在群 `1087440069` 发：

```text
记住群1087440069的测试偏好是乌龙茶
```

预期：

- 关键词保存写入重要记忆。
- 仪表盘「重要记忆」里该条 scope 显示为 `group:1087440069`。
- `pinned` 默认为未勾选。

### 2. 其他会话不应注入群专属记忆

在私聊发：

```text
你记得群1087440069的测试偏好吗？
```

预期：

- 如果模型不知道，应通过 `recall_history` 或直接说明需要查，而不是像当前私聊常驻记忆一样自然说出群专属记忆。
- 若手动把该条改成 `pinned=true` 或 `scope=global`，再次测试时才应更容易直接提到。

### 3. pinned 常驻注入

在仪表盘「重要记忆」选中上一条，勾选「置顶」，保存范围。

再在任意私聊发：

```text
现在检查长期记忆里有没有测试偏好
```

预期：

- 置顶记忆会进入上下文，模型能自然提到「乌龙茶」。
- 保存后如果开启 RAG，索引会重建，不需要重启程序。

### 4. RAG scope 过滤

启用长期记忆 RAG 和 embedding 后，分别在两个群写入相似记忆：

群 A：

```text
记住本群测试饮品是乌龙茶
```

群 B：

```text
记住本群测试饮品是柠檬水
```

随后在群 A 问：

```text
本群测试饮品是什么？
```

预期：

- RAG 召回应优先返回群 A 的 `group:{群A}` 记忆与 `global` / `pinned` 记忆。
- 不应把群 B 的「柠檬水」当作群 A 的答案。

### 5. 本地归档总结

先制造几轮对话，然后等上下文压缩或手动准备已有归档。对机器人说：

```text
总结一下我们这个私聊里之前关于上下文重构的讨论
```

预期：

- 模型优先调用 `summarize_conversation`。
- 工具结果 `source` 为 `local_archive`。
- 私聊也能总结；不会去调用只支持群聊的 `summarize_chat_history`。

如果要测试 NapCat 服务器侧群历史，改在群里说：

```text
帮我拉一下这个群最近的服务器侧聊天历史做个概括
```

预期：

- 这类请求才适合 `summarize_chat_history`，且必须有群号。

### 6. 主动思考重要记忆注入

在群里发：

```text
我的主动思考测试暗号是青柠气泡水
```

再发：

```text
下次主动思考时，如果重要记忆里有这个暗号，就私聊我“暗号检查通过”
```

等待主动思考空闲触发。

预期：

- 主动路由上下文是 system-only 的纯文本摘要，不含 tool role。
- 主动行动轮知道这是后台主动触发。
- 如果重要记忆命中，机器人私聊发送「暗号检查通过」。

## 日志观察点

- clean completion 日志应是「发送完成（全部消息已发出）」或同等 clean completion 文案，不应生成 `<send_receipt>`。
- 被新消息打断时才应出现 `<send_receipt>`，且 `interrupted=true`。
- 主动路由日志中不应再出现 provider 因孤立 tool role 报错。
- 主动路由上下文不应携带 msg_id / send_id 等内部长标识。

## 本轮自动化结果

- `venv\Scripts\python -m compileall memory tools core ui agents tests -q`：通过。
- `venv\Scripts\python -m pytest tests\ -q --ignore=tests\test_kv_cache_real.py`：431 passed in 14.80s。
