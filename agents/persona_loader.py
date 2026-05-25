"""人格动态加载 —— 从 personas/ 加载 persona_prompt.py。

每个人格目录包含：
    persona_prompt.py    必填。导出 PERSONA_PROMPT 字符串，可选 PERSONA_VARS dict。
    avatar.png           （可选）头像，UI 展示用
    icon.ico             （可选）

所有人格平级放在 personas/{name}/ 下：
    - 仓库自带的（如 diana/）随仓库一起发布
    - 用户自创的由 .gitignore 排除，不上传
    程序加载时无差别对待。
"""

from __future__ import annotations

import importlib.util
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app_config.paths import AppPaths

logger = logging.getLogger(__name__)

# 人格名允许：中英文 / 数字 / 下划线 / 连字符。
# 禁止 OS 路径敏感字符（/ \ : * ? " < > | 等），避免目录名引发问题。
_PERSONA_NAME_RE = re.compile(
    r"^[\w一-龥㐀-䶿＀-￯-]{1,64}$"
)
"""\\w 已含 [A-Za-z0-9_]；后两段覆盖中日韩统一汉字与扩展 A 区 + 全角符号区。
连字符单独列出（不在 \\w 内）。明确禁止 / \\ : * ? " < > | 等路径敏感字符。
"""


@dataclass(slots=True)
class Persona:
    """已加载的人格。"""

    name: str
    prompt: str
    """PERSONA_PROMPT 字符串"""

    vars: dict[str, Any] = field(default_factory=dict)
    """PERSONA_VARS dict（含 admin 列表、name 等元信息）"""

    source_dir: Path | None = None
    """加载来源（用户目录 or 内置目录）"""

    def get_admins(self) -> list[dict[str, Any]]:
        """获取管理员列表（如有）。"""
        return list(self.vars.get("admins", []) or [])

    def display_name(self) -> str:
        return self.vars.get("name", self.name) or self.name


def validate_persona_name(name: str) -> None:
    if not _PERSONA_NAME_RE.match(name):
        raise ValueError(
            f"无效的人格名: {name!r}。只允许字母/数字/下划线/连字符，长度 1-64"
        )


def find_persona_dir(paths: AppPaths, name: str) -> Path | None:
    """查找人格目录。返回 None 表示未找到。"""
    validate_persona_name(name)
    persona_dir = paths.PERSONAS_DIR / name
    if (persona_dir / "persona_prompt.py").exists():
        return persona_dir
    return None


def load_persona(paths: AppPaths, name: str) -> Persona:
    """加载指定人格。失败抛 FileNotFoundError 或 ValueError。"""
    validate_persona_name(name)
    persona_dir = find_persona_dir(paths, name)
    if persona_dir is None:
        raise FileNotFoundError(
            f"未找到人格 {name!r}。查找路径: {paths.PERSONAS_DIR / name}"
        )

    persona_file = persona_dir / "persona_prompt.py"
    spec = importlib.util.spec_from_file_location(f"_persona_{name}", persona_file)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载 {persona_file}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    prompt = getattr(module, "PERSONA_PROMPT", None)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{persona_file} 缺少 PERSONA_PROMPT 字符串或为空")

    persona_vars = getattr(module, "PERSONA_VARS", {})
    if not isinstance(persona_vars, dict):
        logger.warning(f"{persona_file} 的 PERSONA_VARS 不是 dict，忽略")
        persona_vars = {}

    return Persona(
        name=name,
        prompt=prompt,
        vars=persona_vars,
        source_dir=persona_dir,
    )


def list_available_personas(paths: AppPaths) -> list[str]:
    """列出所有可用的人格名。"""
    found: set[str] = set()
    base = paths.PERSONAS_DIR
    if not base.exists():
        logger.warning(f"personas 目录 {base} 不存在，请检查项目结构")
        return []
    for d in base.iterdir():
        if (
            d.is_dir()
            and not d.name.startswith("_")
            and not d.name.startswith(".")
            and (d / "persona_prompt.py").exists()
            and _PERSONA_NAME_RE.match(d.name)
        ):
            found.add(d.name)
    if not found:
        logger.warning(
            f"personas 目录 {base} 下没有任何可用人格。"
            f"请克隆仓库自带的 diana/ 或新建一个（参考 docs/persona_writing_guide.md）。"
        )
    return sorted(found)
