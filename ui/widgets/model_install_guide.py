"""本地模型手动安装指引。"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from plugins import PluginRecord

from .window_chrome import FramelessDialog

logger = logging.getLogger(__name__)

_IMPORT_NAME_OVERRIDES = {
    "sentence-transformers": "sentence_transformers",
}
_PIP_SOURCES = {
    "tsinghua": ("清华源", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    "aliyun": ("阿里源", "https://mirrors.aliyun.com/pypi/simple"),
    "official": ("官方源", ""),
}
_RUNNING_DEPENDENCY_INSTALLS: dict[str, QProcess] = {}
_OPEN_GUIDE_DIALOGS: list["ModelInstallGuideDialog"] = []


def required_model_paths(record: PluginRecord) -> list[Path]:
    """返回该模型安装完成后应存在的文件路径。"""
    base = record.meta.resolve_model_dir()
    sources = [s for s in record.meta.download_sources if s.required]
    if not sources:
        return [base]
    return [base / s.dest_filename for s in sources]


def build_model_install_markdown(record: PluginRecord) -> str:
    """生成可给用户看的图文安装指引 Markdown。"""
    meta = record.meta
    target_dir = meta.resolve_model_dir()
    required = required_model_paths(record)
    files = "\n".join(f"- `{p}`" for p in required)
    repo_url = meta.download_url or "该模型未提供下载页面"
    extra = _extra_install_markdown(record)
    return f"""# {meta.display_name} 安装指引

## 先做这两步

1. 打开模型页面：`{repo_url}`
2. 下载完整仓库后，把文件夹拖进模型管理页。

目标目录：`{target_dir}`

## 需要识别到的文件

{files}

## 操作

拖入后 Debata 会复制到正确目录。复制完成后点「重新扫描」，状态变成「已下载」即可。
{extra}
"""


def missing_python_deps(record: PluginRecord) -> list[str]:
    """返回当前 Python 环境缺失的插件运行依赖。"""
    missing: list[str] = []
    for dep in record.meta.python_deps:
        import_name = _requirement_to_import_name(dep)
        if import_name and importlib.util.find_spec(import_name) is None:
            missing.append(dep)
    return missing


def start_dependency_install_if_needed(
    parent: QWidget | None,
    record: PluginRecord,
    *,
    source_key: str = "tsinghua",
    on_output: Callable[[str], None] | None = None,
    on_finished: Callable[[int, str], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> list[str]:
    """后台安装缺失的插件运行依赖，返回本次触发安装的依赖列表。"""
    missing = missing_python_deps(record)
    if not missing:
        return []

    key = record.meta.name
    running = _RUNNING_DEPENDENCY_INSTALLS.get(key)
    if running is not None and running.state() != QProcess.ProcessState.NotRunning:
        return missing

    process = QProcess()
    process.setProgram(sys.executable)
    process.setArguments(_pip_install_args(missing, source_key))
    process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
    process.setProcessEnvironment(_dependency_install_env())
    _RUNNING_DEPENDENCY_INSTALLS[key] = process
    output_parts: list[str] = []

    def _read_output() -> str:
        chunk = bytes(process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if chunk:
            output_parts.append(chunk)
            if on_output is not None:
                on_output(chunk)
        return chunk

    def _on_ready_read() -> None:
        _read_output()

    def _on_finished(exit_code: int, _status) -> None:
        _read_output()
        output = "".join(output_parts).strip()
        _RUNNING_DEPENDENCY_INSTALLS.pop(key, None)
        if exit_code == 0:
            logger.info(
                "模型依赖安装完成：%s deps=%s",
                record.meta.name,
                missing,
            )
        else:
            logger.warning(
                "模型依赖安装失败：%s exit=%s deps=%s output=%s",
                record.meta.name,
                exit_code,
                missing,
                output[-2000:],
            )
        if on_finished is not None:
            on_finished(exit_code, output)

    def _on_error(error) -> None:
        _RUNNING_DEPENDENCY_INSTALLS.pop(key, None)
        logger.warning(
            "模型依赖安装进程启动失败：%s error=%s deps=%s",
            record.meta.name,
            error,
            missing,
        )
        if on_error is not None:
            on_error(str(error))

    process.readyReadStandardOutput.connect(_on_ready_read)
    process.finished.connect(_on_finished)
    process.errorOccurred.connect(_on_error)
    process.start()
    return missing


class ModelInstallGuideDialog(FramelessDialog):
    """非自动下载的模型安装浮窗。"""

    def __init__(self, record: PluginRecord, parent: QWidget | None = None) -> None:
        # 这个窗口需要和主窗口独立，否则拖动浮窗时会把主窗口一起激活到前台。
        super().__init__(f"安装指引 · {record.meta.display_name}", None)
        self._record = record
        self._missing_deps = missing_python_deps(record)
        self.setObjectName("ModelInstallGuideDialog")
        self.setMinimumSize(620, 500)
        self.resize(660, 540)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.body_layout().setContentsMargins(18, 12, 18, 14)
        self.body_layout().setSpacing(10)

        hero = QFrame()
        hero.setObjectName("ModelGuideHero")
        hero_lay = QVBoxLayout(hero)
        hero_lay.setContentsMargins(16, 14, 16, 14)
        hero_lay.setSpacing(8)

        title = QLabel(record.meta.display_name)
        title.setProperty("role", "title-2")
        title.setWordWrap(True)
        hero_lay.addWidget(title)

        intro = QLabel("浏览器和目标目录已打开。下载完整仓库后，把文件夹拖进模型管理页。")
        intro.setProperty("role", "secondary")
        intro.setWordWrap(True)
        hero_lay.addWidget(intro)

        target = QLabel(str(record.meta.resolve_model_dir()))
        target.setProperty("role", "mono")
        target.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        target.setWordWrap(True)
        hero_lay.addWidget(target)
        self.body_layout().addWidget(hero)

        browser = QTextBrowser()
        browser.setObjectName("ModelGuideBrowser")
        browser.setOpenExternalLinks(True)
        browser.document().setDocumentMargin(12)
        browser.document().setDefaultStyleSheet(
            """
            h1 { font-size: 20px; margin: 0 0 10px 0; }
            h2 { font-size: 17px; margin: 12px 0 6px 0; }
            p { margin: 4px 0 8px 0; line-height: 1.35; }
            li { margin: 3px 0; }
            code { font-family: Consolas, monospace; }
            a { color: #6FA39A; }
            """
        )
        browser.setMarkdown(build_model_install_markdown(record))
        self.body_layout().addWidget(browser, 1)

        self._build_dependency_panel()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        open_web = QPushButton("打开模型页面")
        open_web.setProperty("role", "secondary")
        open_web.clicked.connect(lambda: open_model_web_page(record))
        btn_row.addWidget(open_web)

        open_dir = QPushButton("打开目标目录")
        open_dir.setProperty("role", "secondary")
        open_dir.clicked.connect(lambda: open_model_target_dir(record))
        btn_row.addWidget(open_dir)

        close = QPushButton("知道了")
        close.setProperty("role", "primary")
        close.clicked.connect(self.accept)
        btn_row.addWidget(close)
        self.body_layout().addLayout(btn_row)

    def _build_dependency_panel(self) -> None:
        self._dep_panel = QFrame()
        self._dep_panel.setObjectName("ModelGuideDependencyPanel")
        self._dep_panel.setProperty("role", "muted-block")
        lay = QVBoxLayout(self._dep_panel)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        self._dep_status = QLabel("")
        self._dep_status.setWordWrap(True)
        lay.addWidget(self._dep_status)

        self._dep_progress = QProgressBar()
        self._dep_progress.setRange(0, 100)
        self._dep_progress.setValue(0)
        lay.addWidget(self._dep_progress)

        self._dep_output = QLabel("")
        self._dep_output.setProperty("role", "small")
        self._dep_output.setWordWrap(True)
        lay.addWidget(self._dep_output)

        row = QHBoxLayout()
        self._pip_source = QComboBox()
        for key, (label, url) in _PIP_SOURCES.items():
            suffix = f" · {url}" if url else ""
            self._pip_source.addItem(label + suffix, key)
        row.addWidget(self._pip_source, 1)

        self._dep_install_btn = QPushButton("后台安装依赖")
        self._dep_install_btn.setProperty("role", "secondary")
        self._dep_install_btn.clicked.connect(self._on_dependency_install_clicked)
        row.addWidget(self._dep_install_btn)
        lay.addLayout(row)

        self.body_layout().addWidget(self._dep_panel)
        self._render_dependency_state()

    def _render_dependency_state(self) -> None:
        running = _running_dependency_process(self._record)
        if not self._missing_deps and running is None:
            self._dep_panel.setVisible(False)
            return
        self._dep_panel.setVisible(True)
        if running is not None:
            self._dep_status.setText(
                "运行依赖正在后台安装。安装完成后，请重新扫描模型或重启服务再启用本地功能。"
            )
            self._dep_progress.setRange(0, 0)
            self._pip_source.setEnabled(False)
            self._dep_install_btn.setEnabled(False)
            self._dep_install_btn.setText("安装中")
            return
        deps = "\n".join(f"- {dep}" for dep in self._missing_deps)
        self._dep_status.setText(
            "检测到该本地模型缺少运行依赖。请选择 pip 源，然后后台安装：\n"
            f"{deps}"
        )
        self._dep_progress.setRange(0, 100)
        self._dep_progress.setValue(0)
        self._pip_source.setEnabled(True)
        self._dep_install_btn.setEnabled(True)
        self._dep_install_btn.setText("后台安装依赖")

    def _on_dependency_install_clicked(self) -> None:
        if not self._missing_deps:
            self._render_dependency_state()
            return
        source_key = self._pip_source.currentData() or "tsinghua"
        source_label = _PIP_SOURCES.get(source_key, _PIP_SOURCES["tsinghua"])[0]
        self._dep_status.setText(
            f"正在用{source_label}后台安装运行依赖。安装完成后，请重新扫描模型或重启服务再启用本地功能。"
        )
        self._dep_output.setText("启动 pip...")
        self._dep_progress.setRange(0, 0)
        self._pip_source.setEnabled(False)
        self._dep_install_btn.setEnabled(False)
        self._dep_install_btn.setText("安装中")

        start_dependency_install_if_needed(
            self,
            self._record,
            source_key=source_key,
            on_output=self._on_dependency_output,
            on_finished=self._on_dependency_finished,
            on_error=self._on_dependency_error,
        )

    def _on_dependency_output(self, chunk: str) -> None:
        lines = [line.strip() for line in chunk.replace("\r", "\n").splitlines() if line.strip()]
        if not lines:
            return
        self._dep_output.setText(lines[-1][-240:])

    def _on_dependency_finished(self, exit_code: int, output: str) -> None:
        self._dep_progress.setRange(0, 100)
        self._dep_progress.setValue(100 if exit_code == 0 else 0)
        self._pip_source.setEnabled(True)
        self._dep_install_btn.setEnabled(True)
        if exit_code == 0:
            self._missing_deps = missing_python_deps(self._record)
            self._dep_status.setText("运行依赖安装完成。请重新扫描模型或重启服务再启用本地功能。")
            self._dep_install_btn.setText("重新检查")
            self._dep_output.setText("pip install 完成")
        else:
            self._dep_status.setText("运行依赖安装失败。请换一个 pip 源重试，或在终端手动安装。")
            self._dep_install_btn.setText("重试安装")
            tail = output[-240:] if output else "pip install 失败"
            self._dep_output.setText(tail)

    def _on_dependency_error(self, error: str) -> None:
        self._dep_progress.setRange(0, 100)
        self._dep_progress.setValue(0)
        self._pip_source.setEnabled(True)
        self._dep_install_btn.setEnabled(True)
        self._dep_install_btn.setText("重试安装")
        self._dep_status.setText("运行依赖安装进程启动失败。")
        self._dep_output.setText(error)


def open_model_web_page(record: PluginRecord) -> None:
    if record.meta.download_url:
        QDesktopServices.openUrl(QUrl(record.meta.download_url))


def open_model_target_dir(record: PluginRecord) -> None:
    target = record.meta.resolve_model_dir()
    target.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


def show_model_install_guide(parent: QWidget | None, record: PluginRecord) -> None:
    """打开浏览器 + 目标目录 + 浮窗指引。"""
    open_model_web_page(record)
    open_model_target_dir(record)
    dlg = ModelInstallGuideDialog(record, parent)
    _OPEN_GUIDE_DIALOGS.append(dlg)

    def _forget(*_args) -> None:
        if dlg in _OPEN_GUIDE_DIALOGS:
            _OPEN_GUIDE_DIALOGS.remove(dlg)

    dlg.finished.connect(_forget)
    dlg.destroyed.connect(_forget)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()


def find_matching_record_for_folder(
    folder: Path,
    records: list[PluginRecord],
) -> tuple[PluginRecord, Path] | None:
    """根据拖入文件夹内容匹配模型。

    返回 (record, source_root)。source_root 是应复制到 meta.resolve_model_dir() 的目录。
    """
    if not folder.is_dir():
        return None
    candidates: list[tuple[int, int, PluginRecord, Path]] = []
    for record in records:
        required = [s.dest_filename for s in record.meta.download_sources if s.required]
        if not required:
            continue
        direct = _match_score(folder, required)
        if direct == len(required):
            candidates.append((direct, len(folder.parts), record, folder))
        best_nested: tuple[int, Path] = (0, folder)
        for child in folder.rglob("*"):
            if child.is_dir():
                score = _match_score(child, required)
                if score > best_nested[0]:
                    best_nested = (score, child)
        if best_nested[0] == len(required):
            candidates.append(
                (best_nested[0], len(best_nested[1].parts), record, best_nested[1])
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2], candidates[0][3]


def install_model_folder(
    source_root: Path,
    record: PluginRecord,
    progress: Callable[[int, int, Path], None] | None = None,
) -> None:
    """把识别出的模型目录复制到插件目标目录。

    支持两种常见拖入方式：
    - 拖入仓库根目录：目录中已包含 download_sources 的相对路径。
    - 拖入模型子目录：目录中直接包含 model.bin/config.json 等文件。
    """
    target = record.meta.resolve_model_dir()
    target.mkdir(parents=True, exist_ok=True)
    target_prefix = _infer_target_prefix(source_root, record)
    files = [src for src in source_root.rglob("*") if src.is_file()]
    total = len(files)
    for idx, src in enumerate(files, start=1):
        rel = src.relative_to(source_root)
        dst = target / target_prefix / rel
        try:
            if src.resolve() == dst.resolve():
                if progress is not None:
                    progress(idx, total, src)
                continue
        except OSError:
            pass
        dst.parent.mkdir(parents=True, exist_ok=True)
        if progress is not None:
            progress(idx - 1, total, src)
        shutil.copy2(src, dst)
        if progress is not None:
            progress(idx, total, src)


def _infer_target_prefix(source_root: Path, record: PluginRecord) -> Path:
    required = [Path(s.dest_filename) for s in record.meta.download_sources if s.required]
    if not required:
        return Path()
    if all((source_root / rel).is_file() for rel in required):
        return Path()
    if not all((source_root / rel.name).is_file() for rel in required):
        return Path()
    parents = [rel.parent for rel in required]
    first = parents[0]
    if first == Path("."):
        return Path()
    if all(parent == first for parent in parents):
        return first
    return Path()


def _match_score(root: Path, required: list[str]) -> int:
    score = 0
    for rel in required:
        rel_path = Path(rel)
        if (root / rel_path).is_file():
            score += 1
        elif (root / rel_path.name).is_file():
            score += 1
    return score


def _requirement_to_import_name(requirement: str) -> str:
    name = requirement.split(";", 1)[0].strip()
    if "[" in name:
        name = name.split("[", 1)[0].strip()
    for marker in (">=", "==", "~=", "<=", "!=", ">", "<"):
        if marker in name:
            name = name.split(marker, 1)[0].strip()
            break
    return _IMPORT_NAME_OVERRIDES.get(name, name.replace("-", "_"))


def _extra_install_markdown(record: PluginRecord) -> str:
    if record.meta.name != "voxcpm2":
        return ""
    ffmpeg_dir = Path.cwd() / "data" / "tools" / "ffmpeg" / "bin"
    return f"""

## VoxCPM2 降噪需要 FFmpeg

开启降噪时必须准备 Windows **full-shared** 版 FFmpeg。静态版只有 exe，不够用。

下载：

- [FFmpeg 官方下载页](https://ffmpeg.org/download.html)
- [gyan.dev Windows builds](https://www.gyan.dev/ffmpeg/builds/)：选 `full-shared`，解压后复制 `bin`

放到这里：

`{ffmpeg_dir}`

目录里应包含：

- `ffmpeg.exe`
- `avutil-*.dll`
- `avcodec-*.dll`
- `avformat-*.dll`
"""


def _pip_install_args(deps: list[str], source_key: str) -> list[str]:
    args = ["-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    _label, index_url = _PIP_SOURCES.get(source_key, _PIP_SOURCES["tsinghua"])
    if index_url:
        args.extend(["-i", index_url])
    args.extend(deps)
    return args


def _dependency_install_env() -> QProcessEnvironment:
    env = QProcessEnvironment.systemEnvironment()
    env.insert("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.insert("PIP_NO_INPUT", "1")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env.remove(name)
    return env


def _running_dependency_process(record: PluginRecord) -> QProcess | None:
    process = _RUNNING_DEPENDENCY_INSTALLS.get(record.meta.name)
    if process is None or process.state() == QProcess.ProcessState.NotRunning:
        return None
    return process


__all__ = [
    "ModelInstallGuideDialog",
    "build_model_install_markdown",
    "find_matching_record_for_folder",
    "install_model_folder",
    "missing_python_deps",
    "required_model_paths",
    "show_model_install_guide",
    "start_dependency_install_if_needed",
]
