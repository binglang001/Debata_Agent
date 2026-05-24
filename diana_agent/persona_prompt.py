"""人格提示词动态加载器 —— 根据 config.yaml 中 persona 字段加载对应人格"""

import importlib.util
import os
import re

from .config import PERSONA as _persona_name

if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", _persona_name):
    raise ValueError(
        f"无效的人格名称: {_persona_name}，"
        f"只允许字母、数字、下划线、连字符，长度 1-64"
    )

_persona_dir = os.path.join(os.path.dirname(__file__), "..", "personas", _persona_name)
_persona_file = os.path.join(_persona_dir, "persona_prompt.py")

if not os.path.isfile(_persona_file):
    raise FileNotFoundError(
        f"人格 '{_persona_name}' 不存在: {_persona_file}\n"
        f"请检查 config.yaml 中 persona 配置，或创建对应的 personas/{_persona_name}/ 目录"
    )

_spec = importlib.util.spec_from_file_location(
    f"persona_{_persona_name}", _persona_file
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

PERSONA_PROMPT = _module.PERSONA_PROMPT
PERSONA_VARS = getattr(_module, "PERSONA_VARS", {})
