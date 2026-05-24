"""配置加载器 —— 从 config.yaml 读取所有可调参数，不存在时自动创建"""

import os
import shutil

import yaml

_config_dir = os.path.join(os.path.dirname(__file__), "..")
_config_path = os.path.join(_config_dir, "config.yaml")
_example_path = os.path.join(_config_dir, "config.example.yaml")

if not os.path.isfile(_config_path):
    if os.path.isfile(_example_path):
        shutil.copy(_example_path, _config_path)
    else:
        raise FileNotFoundError(
            f"配置文件模板不存在: {_example_path}\n"
            f"请创建 config.yaml 配置文件"
        )

try:
    with open(_config_path, "r", encoding="utf-8") as f:
        _cfg = yaml.safe_load(f)
except yaml.YAMLError as e:
    raise yaml.YAMLError(f"配置文件格式错误: {_config_path}\n{e}")


def get(*keys, default=None):
    """按路径读取配置，如 get('model', 'temperature') → 0.6"""
    v = _cfg
    for k in keys:
        if isinstance(v, dict):
            v = v.get(k)
        else:
            return default
        if v is None:
            return default
    return v


# 常用配置直接暴露为模块变量，方便 import 使用
PERSONA = get("persona", default="yuexi")
PRO_MODEL = get("model", "pro", default="deepseek-v4-pro")
FLASH_MODEL = get("model", "flash", default="deepseek-v4-flash")
TEMPERATURE = get("model", "temperature", default=0.6)
MAX_TOKENS = get("model", "max_tokens", default=384000)
FIRST_TOKEN_TIMEOUT = get("model", "first_token_timeout", default=30)

MAX_LOOPS = get("agent", "max_loops", default=15)
MERGE_WINDOW = get("agent", "merge_window", default=0.5)
RECALL_MERGE_WINDOW = get("agent", "recall_merge_window", default=2.0)
GREETING_INTERVAL = get("agent", "greeting_interval", default=600)

TYping_CHARS_PER_SECOND = get("typing", "chars_per_second", default=3)
TYping_MAX_DELAY = get("typing", "max_delay", default=2.0)

SUMMARIZE_AT = get("summarize", "trigger_at", default=20000)
SUMMARIZE_RANGE_START = get("summarize", "range_start", default=9000)
SUMMARIZE_RANGE_END = get("summarize", "range_end", default=11000)
SUMMARIZE_RANGE = (SUMMARIZE_RANGE_START, SUMMARIZE_RANGE_END)
CHAT_HISTORY_COUNT = get("summarize", "chat_history_count", default=10000)

RATE_LIMIT_WINDOW = get("rate_limit", "window", default=60)
RATE_LIMIT_MAX = get("rate_limit", "max_messages", default=5)

