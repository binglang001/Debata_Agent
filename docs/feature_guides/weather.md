# 天气查询（Weather）

## 先做

1. 打开 [和风天气控制台](https://console.qweather.com/)。
2. 创建 Web API 项目，复制控制台分配的 **API Host** 和 **API Key**。
3. 设置页 / 向导里打开 Weather。
4. Host 填控制台给你的域名，Key 填 API Key，然后点「测试连接」。

## Host 怎么填

新账号通常是控制台分配的独立域名，不是公共 `devapi.qweather.com`（那是免费开发版用的）。
请直接复制控制台里的 API Host，接口路径由程序拼接，Host 只填域名。

## 常见问题

- 404：Host 填错，或把路径一起填进 Host 了。
- 401：Key 不匹配当前 Host。
- 城市失败：先用中文城市名测试，例如 `北京`。
