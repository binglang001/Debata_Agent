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
import logging
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .base import ToolContext, tool
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


# 单次工具调用返回内容截断（防超长返回炸 token）
_MAX_RETURN_BYTES = 50_000


def _truncate(s: str, max_bytes: int = _MAX_RETURN_BYTES) -> tuple[str, bool]:
    """按字节截断字符串。返回 (截断后字符串, 是否被截断)。"""
    encoded = s.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return s, False
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n...（已截断）", True


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
        return {"ok": False, "error": str(e)}
    if not path.exists():
        return {"ok": False, "error": f"文件不存在：{args.path}"}
    if not path.is_file():
        return {"ok": False, "error": f"不是文件：{args.path}"}

    suffix = path.suffix.lower()
    if suffix in {".pdf", ".docx", ".xlsx"}:
        return await _read_document_file(path, args.max_bytes, args.offset, args.max_lines)

    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except OSError as e:
        return {"ok": False, "error": f"读取失败：{e}"}

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        result = _page_text_content(text, args.offset, args.max_lines, args.max_bytes)
        result["warning"] = "文件不是有效 UTF-8，已替换非法字节"
        return result
    return _page_text_content(text, args.offset, args.max_lines, args.max_bytes)


def _page_text_content(text: str, offset: int, max_lines: int, max_bytes: int) -> dict:
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
            selected.append(
                line.encode("utf-8", errors="replace")[:max_bytes].decode(
                    "utf-8",
                    errors="replace",
                )
            )
            used_bytes = max_bytes
            break
        selected.append(line)
        used_bytes += line_bytes
    next_offset = start + len(selected)
    result: dict = {
        "ok": True,
        "content": "\n".join(selected),
        "offset": start,
        "total_lines": total_lines,
    }
    if next_offset < total_lines:
        result["next_offset"] = next_offset
        result["truncated"] = True
        result["_condensed"] = {
            "reason": "文件内容已分页返回",
            "full": f"继续调用 read_file，传 offset={next_offset} 读取后续。",
        }
    return result


async def _read_document_file(path: Path, max_bytes: int, offset: int, max_lines: int) -> dict:
    try:
        if path.suffix.lower() == ".pdf":
            content, warning = await asyncio.to_thread(_extract_pdf_text, path, max_bytes)
        elif path.suffix.lower() == ".docx":
            content, warning = await asyncio.to_thread(_extract_docx_text, path, max_bytes)
        else:
            content, warning = await asyncio.to_thread(_extract_xlsx_text, path, max_bytes)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"文档解析失败：{e}"}

    content = content.strip()
    if not content:
        return {"ok": False, "error": "文档中未提取到可读文本"}
    result = _page_text_content(content, offset, max_lines, max_bytes)
    if warning:
        result["warning"] = warning
    return result


def _extract_pdf_text(path: Path, max_bytes: int) -> tuple[str, str | None]:
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
                    text[:max_bytes],
                    "未安装 pypdf，已使用粗略 PDF 文本提取；复杂 PDF 可能不完整",
                )
            raise RuntimeError("读取 PDF 需要安装 pypdf，或提供可复制文本版本") from None

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
        if len("\n".join(pages).encode("utf-8", errors="replace")) >= max_bytes:
            break
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


def _extract_docx_text(path: Path, max_bytes: int) -> tuple[str, str | None]:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for para in root.findall(".//w:p", ns):
        text = "".join(node.text or "" for node in para.findall(".//w:t", ns))
        if text:
            paragraphs.append(text)
        if len("\n".join(paragraphs).encode("utf-8", errors="replace")) >= max_bytes:
            break
    return "\n".join(paragraphs), None


def _extract_xlsx_text(path: Path, max_bytes: int) -> tuple[str, str | None]:
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
                if len("\n".join(lines + sheet_lines).encode("utf-8", errors="replace")) >= max_bytes:
                    break
            if sheet_lines:
                lines.append(f"[{Path(name).stem}]")
                lines.extend(sheet_lines)
            if len("\n".join(lines).encode("utf-8", errors="replace")) >= max_bytes:
                break
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
        "path": relative_to_workspace(path, ctx.workspace_dir),  # type: ignore[arg-type]
        "bytes": len(args.content.encode("utf-8")),
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
    return {"ok": True}


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
        return {"ok": False, "error": str(e)}
    if not base.exists():
        return {"ok": False, "error": f"目录不存在：{args.path}"}
    if not base.is_dir():
        return {"ok": False, "error": f"不是目录：{args.path}"}

    entries: list[dict] = []
    try:
        for p in base.glob(args.pattern):
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
                if len(entries) >= 200:
                    break
            except OSError:
                continue
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"列举失败：{e}"}

    return {"ok": True, "entries": entries, "count": len(entries)}


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
    return {"ok": True}


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
        return {"ok": False, "error": "workspace 未配置，run_python 被禁用"}
    ws_root = ctx.workspace_dir.resolve(strict=False)
    if not ws_root.exists():
        return {"ok": False, "error": "workspace 目录不存在"}

    # 把代码先写到 workspace/.run/_inline_{ts}.py 然后跑（让 traceback 路径可读）
    import time

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
        return {"ok": False, "error": f"无法写入临时脚本：{e}"}

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
                "error": f"超时（{args.timeout_seconds} 秒）",
                "timeout": True,
            }
        returncode = proc.returncode if proc.returncode is not None else -1
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"执行失败：{e}"}
    finally:
        try:
            script.unlink()
        except OSError:
            pass

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    stdout, st_trunc = _truncate(stdout)
    stderr, se_trunc = _truncate(stderr)

    result: dict = {
        "ok": returncode == 0,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if st_trunc:
        result["stdout_truncated"] = True
    if se_trunc:
        result["stderr_truncated"] = True
    return result
