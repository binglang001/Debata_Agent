# 语音合成（TTS · VoxCPM2）

## 先做

1. 模型页面：[VoxCPM2](https://huggingface.co/OpenBMB/VoxCPM2)。
2. 下载完整仓库，放到 `data/models/VoxCPM2/`，或在「模型管理」里拖入文件夹自动识别。
3. 设置页 / 向导里打开 TTS，运行方式选「本地 · VoxCPM2」。
4. 默认音色/语气建议填写，例如：`年轻女性，自然口语，带一点调侃`。

## 可选项

- 参考音频可以不填；只写音色/语气也能合成。
- 要做音色克隆时，再填 3-30 秒清晰干声。
- 设备优先 `auto`，有 NVIDIA 显卡会走 CUDA。
- 降噪会改善电音感，但需要 FFmpeg full-shared DLL。

## 降噪所需 FFmpeg

开启降噪前，把 Windows **full-shared** 版 FFmpeg 的 `bin` 目录放到：

`data/tools/ffmpeg/bin/`

下载入口：

- [FFmpeg 官方下载页](https://ffmpeg.org/download.html)
- [gyan.dev Windows builds](https://www.gyan.dev/ffmpeg/builds/)：下载 `full-shared` 版本，解压后复制其中的 `bin` 文件夹

里面应能看到：

- `ffmpeg.exe`
- `avutil-*.dll`
- `avcodec-*.dll`
- `avformat-*.dll`

只有 exe 的 `full_build` / static 版不够用。

## 语气提示词

语气提示词会被加到合成文本前，能明显改善情绪和口吻。写短句即可：

- `冷淡、压低声音、慢一点`
- `朋友间吐槽，语速自然`
- `温柔但不装，可爱一点`

长文本合成慢，建议让 AI 发短语音。
