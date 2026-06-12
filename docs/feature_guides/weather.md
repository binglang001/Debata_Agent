# 天气查询（Weather）

Debata 查实时天气和未来预报。走和风天气 API。

## 前提

需要一个和风天气的 API Key 和 API Host。

## 操作

### 1. 注册

打开 [和风天气控制台](https://console.qweather.com)，点击“免费注册”，注册账号后，返回登录界面，点击登录进入引导，点击“现在开始”，根据指引完成引导后进入控制台。

![和风控制台](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_weather_console.png)

### 2. 创建项目与API

点击左侧“项目管理”，点击右上角“创建项目”，项目名称填写“Debata”。
随后，进入项目页面，点击右侧“创建凭据”，名称填写“Agent”，身份认证方式勾选“API KEY”，如图所示，点击“保存”，在弹出的确认框中点击“确认”。随后复制API KEY。

![和风API](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_weather_api.png)

### 3. 获取Host地址

点击左侧“设置”，复制右侧中上方的“API Host”

### 4. 打开 Weather

回到程序，进入设置页 → 功能 →「查天气」卡片，打开开关。首次打开弹配置对话框，填入前面步骤获得的Host和密钥。

![Weather 开关](https://raw.githubusercontent.com/binglang001/Debata_Agent/main/docs/images/guide_weather_settings.png)

**配置完后点击下方“重启Debata服务”**

## 常见问题

**404？** Host 填错了，或者把路径一起填进去了。Host 只填域名部分。

**401？** Key 跟 Host 不匹配。确认是同一个项目的 Key 和 Host。

**城市查不到？** 先用中文城市名测试，比如「北京」。
