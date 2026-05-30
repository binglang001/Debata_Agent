# 联网搜索（Web Search）

## 先做

1. 设置页 / 向导里打开 Web Search。
2. 默认使用 [DuckDuckGo](https://duckduckgo.com/)，不需要 API Key。
3. 建议 `max_results` 保持 5，超时时间保持 10 秒。

## 依赖

项目依赖使用 [`ddgs`](https://pypi.org/project/ddgs/)。如果启动时报缺依赖，请重新安装项目依赖。

## 注意

- DuckDuckGo 在部分网络环境下不可达，搜索会超时。
- 搜索结果会占上下文，结果数不要开太大。
- AI 只会在需要实时信息时调用搜索，不会每条消息都查。
