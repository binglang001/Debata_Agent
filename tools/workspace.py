"""Workspace 沙箱辅助：路径解析与安全检查。

所有 workspace_* 工具都从这里获取经检查的绝对路径。
设计原则：
    - 永远 resolve 一次（解开 ../ 等相对符号）
    - 再用 is_relative_to(workspace) 校验
    - 失败 raise WorkspaceError；调用方包成 dict 返回给 LLM
"""

from __future__ import annotations

from pathlib import Path


class WorkspaceError(Exception):
    """workspace 操作异常。"""


def resolve_in_workspace(user_path: str, workspace_dir: Path | None) -> Path:
    """把用户给的路径解析为 workspace 内的绝对路径。

    Args:
        user_path: 相对 workspace 的路径（"sub/a.txt"）或绝对路径
        workspace_dir: workspace 根目录；None 时拒绝

    Returns:
        绝对路径（已 resolve）

    Raises:
        WorkspaceError: workspace_dir=None / 路径解析失败 / 不在 workspace 下
    """
    if workspace_dir is None:
        raise WorkspaceError("workspace 未配置，文件类工具被禁用")

    try:
        ws_root = workspace_dir.resolve(strict=False)
    except OSError as e:
        raise WorkspaceError(f"workspace 目录无效: {e}") from e

    p = Path(user_path)
    if not p.is_absolute():
        p = ws_root / p
    try:
        resolved = p.resolve(strict=False)
    except OSError as e:
        raise WorkspaceError(f"路径解析失败: {e}") from e

    try:
        if not resolved.is_relative_to(ws_root):
            raise WorkspaceError(
                f"路径不在 workspace 内：{resolved} 不在 {ws_root} 下"
            )
    except ValueError as e:
        raise WorkspaceError(str(e)) from e

    return resolved


def relative_to_workspace(path: Path, workspace_dir: Path) -> str:
    """把绝对路径转成对 workspace 的相对路径展示。"""
    try:
        ws_root = workspace_dir.resolve(strict=False)
        return str(path.resolve(strict=False).relative_to(ws_root)).replace("\\", "/")
    except (OSError, ValueError):
        return str(path)
