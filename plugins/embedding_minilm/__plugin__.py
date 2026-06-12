"""all-MiniLM-L6-v2 本地 Embedding 模型插件。"""

from plugins import DownloadSource, PluginMeta

PLUGIN_META = PluginMeta(
    name="embedding_minilm",
    display_name="all-MiniLM-L6-v2（本地向量化）",
    kind="embedding",
    model_dir="embedding/all-MiniLM-L6-v2",
    size_mb=90,
    description="轻量 sentence-transformers embedding 模型，适合低资源环境。",
    python_deps=["sentence-transformers>=2.7.0"],
    download_url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
    download_sources=[
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/config.json",
            dest_filename="config.json",
        ),
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/model.safetensors",
            dest_filename="model.safetensors",
            size_bytes=90_000_000,
        ),
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/modules.json",
            dest_filename="modules.json",
        ),
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/sentence_bert_config.json",
            dest_filename="sentence_bert_config.json",
        ),
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json",
            dest_filename="tokenizer.json",
        ),
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer_config.json",
            dest_filename="tokenizer_config.json",
        ),
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/vocab.txt",
            dest_filename="vocab.txt",
        ),
        DownloadSource(
            url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/1_Pooling/config.json",
            dest_filename="1_Pooling/config.json",
        ),
    ],
    auto_download=False,
)


def build(config: dict):
    from features.embedding import get_local_service

    return get_local_service(
        config.get("model_dir", "data/models/embedding/all-MiniLM-L6-v2"),
        config.get("device", "auto"),
    )
