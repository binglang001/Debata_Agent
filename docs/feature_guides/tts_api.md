# 语音合成 API 配置

本页用于配置 `send_voice_message` 使用的云端语音合成。

## 推荐：EdgeTTS

EdgeTTS 是默认推荐项：

1. 在“用声音说话（TTS）”里打开开关。
2. 运行方式选择“云端 API”。
3. API Provider 选择“EdgeTTS（推荐 · 无需密钥）”。
4. 不需要填写 API Key、AppID 或 Secret。
5. “说话人”可以留空，默认使用 `zh-CN-XiaoxiaoNeural`；如需指定微软语音名，可填写例如 `zh-CN-XiaoyiNeural`。

注意：EdgeTTS 是免费的在线服务，但依赖网络和微软在线朗读服务。合成可能因为网络、代理、地区或服务策略失败；失败时工具会返回明确错误，改用讯飞或本地 VoxCPM2 即可。

## 科大讯飞

讯飞适合需要稳定商业服务或指定发音人的场景。

1. 打开讯飞在线语音合成页面：<https://www.xfyun.cn/services/online_tts?target=price>
2. 创建或进入应用，开通“在线语音合成（流式版）”。
3. 在控制台服务页获取 `AppID`、`APIKey`、`APISecret`。
4. 在 Debata 设置里选择“云端 API”。
5. API Provider 选择“科大讯飞”。
6. 填写 `API Key`、`App ID`、`API Secret`。
7. “说话人”可填控制台已开通的发音人参数，例如 `x4_xiaoyan`；留空使用默认值。

讯飞单次文本限制小于 8000 字节。小语种或特殊方言需要先在讯飞控制台开通对应发音人，否则会返回授权错误。

## 本地 VoxCPM2

如果你希望不依赖在线服务，可以选择本地 VoxCPM2：

1. 运行方式选择“本地（VoxCPM2）”。
2. 确认模型目录已放好模型文件。
3. 可填写默认音色/语气，也可提供 3-10 秒参考音频做音色克隆。

本地模式首次加载较慢，但可离线运行。
