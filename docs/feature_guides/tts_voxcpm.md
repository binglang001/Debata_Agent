# 语音合成（TTS）

让 Debata 用声音回复。本地 VoxCPM2 或云端 TTS 都可以。

## 本地 · VoxCPM2

### 安装模型

模型管理页 → VoxCPM2 → 点「安装指引」，按提示下载模型文件，拖到 `data/models/VoxCPM2/`。

### 配置

设置页 → 功能 →「用声音说话」卡片 → 编辑配置。运行方式选「本地」，填音色描述。

参考音频可以不填。只写音色描述也能合成，比如「年轻女性，自然口语，带一点调侃」。

**开启降噪**需要 FFmpeg full-shared 版。把 `bin` 文件夹放到 `data/tools/ffmpeg/bin/`，里面要有 `ffmpeg.exe` 和 `avutil-*.dll` 等文件。下载入口：[gyan.dev](https://www.gyan.dev/ffmpeg/builds/)。

### 语气提示词

提示词加到合成文本前面，明显改善情绪和口吻。写短句就行：

- `冷淡、压低声音、慢一点`
- `朋友间吐槽，语速自然`
- `温柔但不装`

长文本合成慢，建议让 AI 发短语音。

**配置完后点击下方“重启Debata服务”**

## 云端 TTS

选 Edge TTS（免费）或 iFlyTek API，不需要本地 GPU。编辑 TTS 配置时切换运行方式即可。
