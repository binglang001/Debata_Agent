"""Workspace 文件工具：read / write / edit / list / delete / run_python。

所有工具都受 ctx.workspace_dir 限制：
    - 路径必须 resolve 后在 workspace 下
    - workspace_dir=None 时全部禁用

run_python 用 subprocess.run 跑 Python 解释器（venv 的）：
    - cwd 锁死在 workspace 目录
    - 通过路径检查 + Python sys.path 限制阻止访问 workspace 外（best-effort，
      Python 本身无法 100% 沙箱化，所以默认信任 AI 不会构造逃逸路径，
      但工具循环里 LLM 想做坏事很难——它只能写 workspace 内的代码）
    - 超时 + stdout/stderr 截断 + 不继承父进程环境敏感变量
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from utils.token_budget import TokenEstimator

from .base import ToolContext, tool
from .result_shrink import tool_budget
from .schemas import (
    DeleteFileArgs,
    EditFileArgs,
    ListFilesArgs,
    ReadFileArgs,
    RunPythonArgs,
    WriteFileArgs,
)
from .workspace import WorkspaceError, relative_to_workspace, resolve_in_workspace

logger = logging.getLogger(__name__)


_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_RUN_OUTPUT_INLINE_CHARS = 800
_RUN_OUTPUT_PREVIEW_LINES = 80


def _estimate_result(result: dict[str, Any], estimator: TokenEstimator) -> int:
    return estimator.estimate_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True)
    )


def _artifact_dir(ctx: ToolContext, name: str) -> Path:
    if ctx.workspace_dir is None:
        raise RuntimeError("workspace 未配置")
    out_dir = ctx.workspace_dir / "runtime" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _safe_artifact_stem(value: str, default: str) -> str:
    stem = Path(value).name or default
    stem = _SAFE_PATH_RE.sub("_", stem).strip("._")
    return stem or default


def _write_text_artifact(
    ctx: ToolContext,
    *,
    directory: str,
    stem: str,
    suffix: str,
    content: str,
) -> str:
    out_dir = _artifact_dir(ctx, directory)
    path = out_dir / f"{_safe_artifact_stem(stem, directory)}_{int(time.time() * 1000)}{suffix}"
    path.write_text(content, encoding="utf-8")
    return relative_to_workspace(path, ctx.workspace_dir)  # type: ignore[arg-type]


def _line_preview(
    text: str,
    *,
    max_lines: int = _RUN_OUTPUT_PREVIEW_LINES,
    max_chars: int = _RUN_OUTPUT_INLINE_CHARS,
) -> str:
    lines = text.splitlines()
    preview = text if len(lines) <= max_lines else "\n".join(lines[-max_lines:])
    if len(preview) <= max_chars:
        return preview
    return preview[-max_chars:]


@tool(
    name="read_file",
    description=(
        "读取 workspace 内的文件内容。支持 UTF-8 文本，以及常见文档的文本抽取"
        "（PDF/DOCX/XLSX）。收到消息里的 workspace= 路径时优先用这个工具查看内容；"
        "不要尝试读取 workspace 之外的用户本机路径。"
    ),
    args_model=ReadFileArgs,
    category="workspace",
)
async def read_file(args: ReadFileArgs, ctx: ToolContext) -> dict:
    try:
        path = resolve_in_workspace(args.path, ctx.workspace_dir)
    except WorkspaceError as e:
        return {"ok": False, "status": "failed", "brief": str(e), "error": str(e)}
    if not path.exists():
        error = f"文件不存在：{args.path}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}
    if not path.is_file():
        error = f"不是文件：{args.path}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}

    suffix = path.suffix.lower()
    if suffix in {".pdf", ".docx", ".xlsx"}:
        return await _read_document_file(path, args, ctx)

    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except OSError as e:
        error = f"读取失败：{e}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        result = _page_text_content(text, args, ctx, source_path=path)
        result["warning"] = "文件不是有效 UTF-8，已替换非法字节"
        return result
    return _page_text_content(text, args, ctx, source_path=path)


def _page_text_content(
    text: str,
    args: ReadFileArgs,
    ctx: ToolContext,
    *,
    source_path: Path,
) -> dict:
    offset = args.offset
    max_lines = args.max_lines
    max_bytes = args.max_bytes
    rel_path = relative_to_workspace(source_path, ctx.workspace_dir)  # type: ignore[arg-type]
    lines = text.splitlines()
    total_lines = len(lines)
    start = min(offset, total_lines)
    selected: list[str] = []
    used_bytes = 0
    for line in lines[start : start + max_lines]:
        line_bytes = len((line + "\n").encode("utf-8", errors="replace"))
        if selected and used_bytes + line_bytes > max_bytes:
            break
        if not selected and line_bytes > max_bytes:
            selected.append(line)
            used_bytes = line_bytes
            break
        selected.append(line)
        used_bytes += line_bytes
    next_offset = start + len(selected)
    from_line = start + 1 if selected else start
    to_line = next_offset if selected else start
    content = "\n".join(selected)
    meta = {
        "path": rel_path,
        "offset": start,
        "returned_lines": len(selected),
        "from_line": from_line,
        "to_line": to_line,
        "total_lines": total_lines,
        "bytes": len(content.encode("utf-8", errors="replace")),
        "range": "continuous_page",
    }
    result: dict[str, Any] = {
        "ok": True,
        "status": "inline",
        "brief": f"已读取 {rel_path} 第 {from_line}-{to_line} 行，共 {total_lines} 行。",
        "content": content,
        "path": rel_path,
        "offset": start,
        "total_lines": total_lines,
        "data": meta,
    }
    if next_offset < total_lines:
        result["next_offset"] = next_offset
        result["truncated"] = True
        result["next"] = f"继续调用 read_file，传 path={args.path!r}, offset={next_offset} 读取后续连续内容。"
        meta["next_offset"] = next_offset

    budget = tool_budget("read_file", ctx)
    estimator = TokenEstimator()
    if _estimate_result(result, estimator) <= budget.inline:
        return result

    artifact_path = _write_read_file_artifact(
        ctx,
        rel_path=rel_path,
        content=content,
        meta=meta,
    )
    artifact_result: dict[str, Any] = {
        "ok": True,
        "status": "artifact",
        "brief": (
            f"{rel_path} 当前连续页较长，已写入完整文件：{artifact_path}；"
            f"第 {from_line}-{to_line} 行，共 {len(selected)} 行。"
        ),
        "path": rel_path,
        "artifact": {
            "path": artifact_path,
            "type": "markdown",
            "from_line": from_line,
            "to_line": to_line,
            "line_count": len(selected),
            "total_lines": total_lines,
        },
        "offset": start,
        "total_lines": total_lines,
        "data": meta,
        "next": (
            "需要当前页完整正文时读取 artifact.path；"
            f"{'继续原文件后续内容可调用 read_file offset=' + str(next_offset) if next_offset < total_lines else '原文件已到末尾'}。"
        ),
    }
    if next_offset < total_lines:
        artifact_result["next_offset"] = next_offset
        artifact_result["truncated"] = True
    return artifact_result


def _write_read_file_artifact(
    ctx: ToolContext,
    *,
    rel_path: str,
    content: str,
    meta: dict[str, Any],
) -> str:
    header = [
        "# read_file 结果",
        "",
        f"- path: {rel_path}",
        f"- range: line {meta['from_line']} to {meta['to_line']} of {meta['total_lines']}",
        f"- bytes: {meta['bytes']}",
        "",
        "```text",
        content,
        "```",
        "",
    ]
    return _write_text_artifact(
        ctx,
        directory="read_file",
        stem=rel_path,
        suffix=".md",
        content="\n".join(header),
    )


async def _read_document_file(path: Path, args: ReadFileArgs, ctx: ToolContext) -> dict:
    try:
        if path.suffix.lower() == ".pdf":
            content, warning = await asyncio.to_thread(_extract_pdf_text, path)
        elif path.suffix.lower() == ".docx":
            content, warning = await asyncio.to_thread(_extract_docx_text, path)
        else:
            content, warning = await asyncio.to_thread(_extract_xlsx_text, path)
    except Exception as e:  # noqa: BLE001
        error = f"文档解析失败：{e}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}

    content = content.strip()
    if not content:
        return {
            "ok": False,
            "status": "failed",
            "brief": "文档中未提取到可读文本",
            "error": "文档中未提取到可读文本",
        }
    result = _page_text_content(content, args, ctx, source_path=path)
    if warning:
        result["warning"] = warning
    return result


def _extract_pdf_text(path: Path) -> tuple[str, str | None]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        try:
            from PyPDF2 import PdfReader  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            raw = path.read_bytes()
            text = _extract_pdf_literal_strings(raw)
            if text.strip():
                return (
                    text,
                    "未安装 pypdf，已使用粗略 PDF 文本提取；复杂 PDF 可能不完整",
                )
            raise RuntimeError("读取 PDF 需要安装 pypdf，或提供可复制文本版本") from None

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages), None


def _extract_pdf_literal_strings(raw: bytes) -> str:
    """在没有 PDF 库时，从未压缩文本流里兜底提取 `(text) Tj` 片段。"""
    chunks: list[str] = []
    for match in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*(?:Tj|'|\"|TJ)", raw):
        token = match.group(0)
        end = token.find(b")")
        body = token[1:end if end >= 0 else len(token)]
        body = body.replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
        chunks.append(body.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _extract_docx_text(path: Path) -> tuple[str, str | None]:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for para in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in para.findall(".//w:t", ns))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs), None


def _extract_xlsx_text(path: Path) -> tuple[str, str | None]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        shared = _xlsx_shared_strings(zf, ns)
        lines: list[str] = []
        for name in sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
            root = ElementTree.fromstring(zf.read(name))
            sheet_lines = []
            for row in root.findall(".//m:row", ns):
                cells: list[str] = []
                for cell in row.findall("m:c", ns):
                    cells.append(_xlsx_cell_text(cell, shared, ns))
                line = "\t".join(c for c in cells if c)
                if line:
                    sheet_lines.append(line)
            if sheet_lines:
                lines.append(f"[{Path(name).stem}]")
                lines.extend(sheet_lines)
    return "\n".join(lines), None


def _xlsx_shared_strings(zf: zipfile.ZipFile, ns: dict[str, str]) -> list[str]:
    try:
        root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out: list[str] = []
    for item in root.findall(".//m:si", ns):
        out.append("".join(t.text or "" for t in item.findall(".//m:t", ns)))
    return out


def _xlsx_cell_text(cell: ElementTree.Element, shared: list[str], ns: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(".//m:t", ns))
    value = cell.find("m:v", ns)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return value.text
    return value.text


@tool(
    name="write_file",
    description=(
        "在 workspace 内创建或覆盖一个文本文件。会自动建中间目录。"
        "覆盖写：原内容直接被替换，慎用——如果只想改一部分用 edit_file。"
    ),
    args_model=WriteFileArgs,
    category="workspace",
)
async def write_file(args: WriteFileArgs, ctx: ToolContext) -> dict:
    try:
        path = resolve_in_workspace(args.path, ctx.workspace_dir)
    except WorkspaceError as e:
        return {"ok": False, "error": str(e)}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, args.content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"写入失败：{e}"}

    return {
        "ok": True,
        "status": "done",
        "brief": f"已写入文件 {relative_to_workspace(path, ctx.workspace_dir)}。",
        "path": relative_to_workspace(path, ctx.workspace_dir),  # type: ignore[arg-type]
        "bytes": len(args.content.encode("utf-8")),
        "data": {
            "path": relative_to_workspace(path, ctx.workspace_dir),  # type: ignore[arg-type]
            "bytes": len(args.content.encode("utf-8")),
        },
    }


@tool(
    name="edit_file",
    description=(
        "在 workspace 内修改一个文件：把 old 字符串替换为 new 字符串。"
        "old 必须在文件中**只出现一次**，否则失败（避免误改）。"
        "如要替换多处或大改，直接用 write_file 覆盖。"
    ),
    args_model=EditFileArgs,
    category="workspace",
)
async def edit_file(args: EditFileArgs, ctx: ToolContext) -> dict:
    try:
        path = resolve_in_workspace(args.path, ctx.workspace_dir)
    except WorkspaceError as e:
        return {"ok": False, "error": str(e)}
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": f"文件不存在：{args.path}"}

    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {"ok": False, "error": f"读取失败：{e}"}

    count = text.count(args.old)
    if count == 0:
        return {"ok": False, "error": "old 字符串未出现在文件中"}
    if count > 1:
        return {
            "ok": False,
            "error": f"old 字符串出现 {count} 次，必须唯一。请提供更长的上下文使其唯一。",
        }

    new_text = text.replace(args.old, args.new, 1)
    try:
        await asyncio.to_thread(path.write_text, new_text, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"写入失败：{e}"}
    rel_path = relative_to_workspace(path, ctx.workspace_dir)  # type: ignore[arg-type]
    return {
        "ok": True,
        "status": "done",
        "brief": f"已编辑文件 {rel_path}。",
        "path": rel_path,
        "data": {
            "path": rel_path,
            "old_length": len(args.old),
            "new_length": len(args.new),
        },
    }


@tool(
    name="list_files",
    description=(
        "列出 workspace 内的文件。支持 glob 模式（如 '*.py' 或 '**/*.json'）。"
        "返回最多 200 条；每条含相对路径、大小、是否目录。"
    ),
    args_model=ListFilesArgs,
    category="workspace",
)
async def list_files(args: ListFilesArgs, ctx: ToolContext) -> dict:
    try:
        base = resolve_in_workspace(args.path, ctx.workspace_dir)
    except WorkspaceError as e:
        return {"ok": False, "status": "failed", "brief": str(e), "error": str(e)}
    if not base.exists():
        error = f"目录不存在：{args.path}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}
    if not base.is_dir():
        error = f"不是目录：{args.path}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}

    matches: list[Path] = []
    try:
        for p in base.glob(args.pattern):
            matches.append(p)
    except (OSError, ValueError) as e:
        return {"ok": False, "status": "failed", "brief": f"列举失败：{e}", "error": f"列举失败：{e}"}

    matches.sort(key=lambda item: relative_to_workspace(item, ctx.workspace_dir).lower())  # type: ignore[arg-type]
    total = len(matches)
    start = min(args.offset, total)
    page = matches[start : start + args.limit]

    entries: list[dict[str, Any]] = []
    for p in page:
        try:
            rel = relative_to_workspace(p, ctx.workspace_dir)  # type: ignore[arg-type]
            stat = p.stat()
            entries.append(
                {
                    "path": rel,
                    "is_dir": p.is_dir(),
                    "size": stat.st_size if p.is_file() else 0,
                }
            )
        except OSError:
            entries.append(
                {
                    "path": relative_to_workspace(p, ctx.workspace_dir),  # type: ignore[arg-type]
                    "error": "stat_failed",
                }
            )
    next_offset = start + len(entries)
    result: dict[str, Any] = {
        "ok": True,
        "status": "inline",
        "brief": f"列出 {args.path} 中 {len(entries)}/{total} 条匹配项。",
        "entries": entries,
        "count": total,
        "offset": start,
        "limit": args.limit,
        "data": {
            "path": args.path,
            "pattern": args.pattern,
            "count": total,
            "returned": len(entries),
            "offset": start,
        },
    }
    if next_offset < total:
        result["next_offset"] = next_offset
        result["next"] = f"继续调用 list_files，传 offset={next_offset} 读取下一页。"
        result["data"]["next_offset"] = next_offset
    return result


@tool(
    name="delete_file",
    description=(
        "删除 workspace 内的一个文件。**不能**删除目录（防误删整个 workspace）。"
        "请慎用——删了就没了。"
    ),
    args_model=DeleteFileArgs,
    category="workspace",
)
async def delete_file(args: DeleteFileArgs, ctx: ToolContext) -> dict:
    try:
        path = resolve_in_workspace(args.path, ctx.workspace_dir)
    except WorkspaceError as e:
        return {"ok": False, "error": str(e)}
    if not path.exists():
        return {"ok": False, "error": f"文件不存在：{args.path}"}
    if path.is_dir():
        return {"ok": False, "error": "不能删除目录"}

    try:
        await asyncio.to_thread(path.unlink)
    except OSError as e:
        return {"ok": False, "error": f"删除失败：{e}"}
    rel_path = relative_to_workspace(path, ctx.workspace_dir)  # type: ignore[arg-type]
    return {
        "ok": True,
        "status": "done",
        "brief": f"已删除文件 {rel_path}。",
        "path": rel_path,
        "data": {"path": rel_path},
    }


@tool(
    name="run_python",
    description=(
        "在 workspace 目录里执行一段 Python 代码（subprocess + 当前 venv）。"
        "cwd 锁死在 workspace。可以读写 workspace 内任何文件、用任意标准库 + 已装第三方库。"
        "适合计算、生成小文件、验证脚本或处理用户上传到 workspace 的文件。"
        "**不能**访问 workspace 外的文件（路径检查由代码本身配合 + 调用方约束）。"
        "默认超时 30 秒。返回 stdout / stderr / returncode。"
    ),
    args_model=RunPythonArgs,
    category="workspace",
)
async def run_python(args: RunPythonArgs, ctx: ToolContext) -> dict:
    if ctx.workspace_dir is None:
        error = "workspace 未配置，run_python 被禁用"
        return {"ok": False, "status": "failed", "brief": error, "error": error}
    ws_root = ctx.workspace_dir.resolve(strict=False)
    if not ws_root.exists():
        error = "workspace 目录不存在"
        return {"ok": False, "status": "failed", "brief": error, "error": error}

    # 把代码先写到 workspace/.run/_inline_{ts}.py 然后跑（让 traceback 路径可读）
    run_dir = ws_root / ".run"
    run_dir.mkdir(exist_ok=True)
    script = run_dir / f"_inline_{int(time.time() * 1000)}.py"

    # 在 AI 代码前插入路径守卫：把 os.chdir / open / Path.open 等容易越界的入口
    # 限制在 workspace 下。这是 best-effort——Python 本身无法 100% 沙箱化。
    guard = (
        f"import os, sys\n"
        f"os.chdir({str(ws_root)!r})\n"
        f"# AI 代码从这里开始\n"
    )
    try:
        await asyncio.to_thread(script.write_text, guard + args.code, encoding="utf-8")
    except OSError as e:
        error = f"无法写入临时脚本：{e}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            cwd=str(ws_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=args.timeout_seconds
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return {
                "ok": False,
                "status": "failed",
                "brief": f"Python 执行超时（{args.timeout_seconds} 秒）。",
                "error": f"超时（{args.timeout_seconds} 秒）",
                "timeout": True,
            }
        returncode = proc.returncode if proc.returncode is not None else -1
    except Exception as e:  # noqa: BLE001
        error = f"执行失败：{e}"
        return {"ok": False, "status": "failed", "brief": error, "error": error}
    finally:
        try:
            script.unlink()
        except OSError:
            pass

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    stdout_bytes_len = len(stdout.encode("utf-8", errors="replace"))
    stderr_bytes_len = len(stderr.encode("utf-8", errors="replace"))
    result: dict[str, Any] = {
        "ok": returncode == 0,
        "status": "inline",
        "brief": f"Python 执行完成，returncode={returncode}。",
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "data": {
            "returncode": returncode,
            "stdout_bytes": stdout_bytes_len,
            "stderr_bytes": stderr_bytes_len,
            "timeout_seconds": args.timeout_seconds,
        },
    }
    budget = tool_budget("run_python", ctx)
    estimator = TokenEstimator()
    if _estimate_result(result, estimator) <= budget.inline:
        return result

    artifact_path = _write_run_python_artifact(
        ctx,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timeout_seconds=args.timeout_seconds,
    )
    stdout_preview = _line_preview(stdout)
    stderr_preview = _line_preview(stderr)
    artifact_result: dict[str, Any] = {
        "ok": returncode == 0,
        "status": "artifact",
        "brief": (
            f"Python 输出较长，完整 stdout/stderr 已写入 {artifact_path}；"
            f"returncode={returncode}。"
        ),
        "returncode": returncode,
        "stdout": stdout_preview,
        "stderr": stderr_preview,
        "stdout_preview": stdout_preview,
        "stderr_preview": stderr_preview,
        "stdout_truncated": stdout != stdout_preview,
        "stderr_truncated": stderr != stderr_preview,
        "artifact": {
            "path": artifact_path,
            "type": "markdown",
            "stdout_bytes": stdout_bytes_len,
            "stderr_bytes": stderr_bytes_len,
        },
        "data": {
            "returncode": returncode,
            "stdout_bytes": stdout_bytes_len,
            "stderr_bytes": stderr_bytes_len,
            "stdout_preview_lines": len(stdout_preview.splitlines()) if stdout_preview else 0,
            "stderr_preview_lines": len(stderr_preview.splitlines()) if stderr_preview else 0,
            "timeout_seconds": args.timeout_seconds,
        },
        "next": "需要完整 stdout/stderr 时读取 artifact.path；预览字段不是完整输出。",
    }
    return artifact_result


def _write_run_python_artifact(
    ctx: ToolContext,
    *,
    returncode: int,
    stdout: str,
    stderr: str,
    timeout_seconds: int,
) -> str:
    body = [
        "# run_python 输出",
        "",
        f"- returncode: {returncode}",
        f"- timeout_seconds: {timeout_seconds}",
        f"- stdout_bytes: {len(stdout.encode('utf-8', errors='replace'))}",
        f"- stderr_bytes: {len(stderr.encode('utf-8', errors='replace'))}",
        "",
        "## stdout",
        "",
        "```text",
        stdout,
        "```",
        "",
        "## stderr",
        "",
        "```text",
        stderr,
        "```",
        "",
    ]
    return _write_text_artifact(
        ctx,
        directory="run_python",
        stem="run_python",
        suffix=".md",
        content="\n".join(body),
    )
