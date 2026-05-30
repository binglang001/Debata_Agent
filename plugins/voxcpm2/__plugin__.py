"""VoxCPM2 本地 TTS 插件入口。"""

from plugins import DownloadSource, PluginMeta

PLUGIN_META = PluginMeta(
    name="voxcpm2",
    display_name="VoxCPM2（本地语音合成）",
    kind="tts",
    model_dir="VoxCPM2",
    size_mb=3000,
    description="基于清华开源 VoxCPM2 的本地 TTS，支持无参考音频的 Voice Design，也可用参考音频做音色克隆。",
    python_deps=["voxcpm>=2.0.0", "soundfile>=0.12.1"],
    download_url="https://huggingface.co/OpenBMB/VoxCPM2",
    download_sources=[
        DownloadSource(
            url="https://huggingface.co/OpenBMB/VoxCPM2/resolve/main/model.safetensors",
            dest_filename="model.safetensors",
            size_bytes=3_000_000_000,
        ),
        DownloadSource(
            url="https://huggingface.co/OpenBMB/VoxCPM2/resolve/main/config.json",
            dest_filename="config.json",
        ),
        DownloadSource(
            url="https://huggingface.co/OpenBMB/VoxCPM2/resolve/main/tokenizer.json",
            dest_filename="tokenizer.json",
        ),
    ],
    config_schema={
        "model_dir": {
            "type": "string",
            "default": "data/models/VoxCPM2/",
            "label": "模型目录",
        },
        "reference_audio": {
            "type": "string",
            "default": "",
            "label": "参考音频路径（可选）",
            "help": "可选。填入 3~10 秒单人音频时做音色克隆；不填时用默认合成音色或默认合成引导语。",
        },
        "default_prompt": {
            "type": "string",
            "default": "",
            "label": "默认合成引导语（可空）",
            "help": "如「年轻女性，温柔语气」。无参考音频时可用它做 Voice Design。",
        },
        "device": {
            "type": "select",
            "default": "auto",
            "label": "设备",
            "options": ["auto", "cuda", "cpu"],
        },
        "load_denoiser": {
            "type": "bool",
            "default": False,
            "label": "启用降噪器",
            "help": "默认关闭，避免首次加载时额外下载 ModelScope 降噪模型。仅在已手动准备降噪模型时开启。",
        },
        "cfg_value": {
            "type": "float",
            "default": 2.0,
            "label": "CFG 引导强度",
        },
        "inference_timesteps": {
            "type": "int",
            "default": 10,
            "label": "推理步数",
        },
    },
    auto_download=False,
)


def build(config: dict):
    import importlib.util
    from pathlib import Path
    impl_path = Path(__file__).parent / "voxcpm_impl.py"
    spec = importlib.util.spec_from_file_location("plugins.voxcpm2._impl", impl_path)
    impl_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(impl_mod)
    return impl_mod.VoxCPM2Service(
        model_dir=config.get("model_dir", "data/models/VoxCPM2/"),
        reference_audio=config.get("reference_audio", ""),
        default_prompt=config.get("default_prompt", ""),
        device=config.get("device", "auto"),
        load_denoiser=bool(config.get("load_denoiser", False)),
        cfg_value=float(config.get("cfg_value", 2.0)),
        inference_timesteps=int(config.get("inference_timesteps", 10)),
    )
