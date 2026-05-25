"""NapCat 适配器配置。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..components import (
    ApiKeyInput,
    SectionCard,
    WhitelistEditor,
)
from ..context import BaseStepView, WizardContext
from ..copy import COPY
from ...theme import Spacing


class AdapterStepView(BaseStepView):
    """连接模式 + 地址端口 + token + 白名单。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(title="把 NapCat 接上", subtitle=COPY["adapter.intro"])
        outer.addWidget(card)

        # 连接方式
        mode_title = QLabel(COPY["adapter.mode_title"])
        mode_title.setProperty("role", "title-3")
        card.add_content(mode_title)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._rb_client = self._mk_mode_card(
            "client",
            COPY["adapter.mode_client"],
            COPY["adapter.mode_client_desc"],
        )
        self._rb_server = self._mk_mode_card(
            "server",
            COPY["adapter.mode_server"],
            COPY["adapter.mode_server_desc"],
        )
        card.add_content(self._rb_client)
        card.add_content(self._rb_server)

        # 地址 / 端口 / 路径 / token
        form = QFormLayout()
        form.setSpacing(Spacing.SM)
        self._host_edit = QLineEdit("127.0.0.1")
        form.addRow(QLabel("地址"), self._host_edit)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(3001)
        form.addRow(QLabel("端口"), self._port_spin)
        self._path_edit = QLineEdit("/")
        form.addRow(QLabel("WebSocket 路径"), self._path_edit)
        self._token_input = ApiKeyInput(
            placeholder="可选；NapCat 那边设了 token 才填",
            test_button_text="测试连接",
        )
        self._token_input.test_requested.connect(self._on_test_token)
        form.addRow(QLabel(COPY["adapter.token_label"]), self._token_input)
        card.add_layout(form)

        token_hint = QLabel(COPY["adapter.token_hint"])
        token_hint.setProperty("role", "secondary")
        card.add_content(token_hint)

        # 进程托管
        self._manage_check = QCheckBox(COPY["adapter.manage_process_label"])
        card.add_content(self._manage_check)
        process_hint = QLabel(COPY["adapter.manage_process_hint"])
        process_hint.setProperty("role", "secondary")
        process_hint.setWordWrap(True)
        card.add_content(process_hint)
        self._process_path_edit = QLineEdit()
        self._process_path_edit.setPlaceholderText("如 D:/NapCat/NapCatWinBootMain.exe")
        self._manage_check.toggled.connect(self._process_path_edit.setVisible)
        self._process_path_edit.setVisible(False)
        card.add_content(self._process_path_edit)

        # 白名单
        sep = QFrame()
        sep.setProperty("role", "separator")
        card.add_content(sep)

        wl_title = QLabel(COPY["adapter.whitelist_title"])
        wl_title.setProperty("role", "title-3")
        card.add_content(wl_title)

        self._whitelist = WhitelistEditor(
            initial=self.context.adapter.whitelist,
            on_open_confirm=self._confirm_open,
        )
        card.add_content(self._whitelist)

        outer.addStretch(1)

    def _mk_mode_card(self, value: str, title: str, desc: str) -> QFrame:
        wrapper = QFrame()
        wrapper.setObjectName("Card")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        rb = QRadioButton(title)
        rb.setProperty("mode_value", value)
        self._mode_group.addButton(rb)
        wl.addWidget(rb)
        d = QLabel(desc)
        d.setProperty("role", "secondary")
        d.setWordWrap(True)
        d.setContentsMargins(24, 0, 0, 0)
        wl.addWidget(d)
        return wrapper

    def _confirm_open(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(COPY["warning.whitelist_all_title"])
        box.setText(COPY["warning.whitelist_all_body"])
        confirm = box.addButton(
            COPY["warning.whitelist_all_confirm"], QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(COPY["warning.whitelist_all_cancel"], QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is confirm

    def _on_test_token(self, token: str) -> None:
        """实测 NapCat 连接。

        client 模式：起 WS 客户端连过去，3 秒内连上即 success；
        server 模式：监听端口等 NapCat 反向连入，3 秒内未连入则给中性提示（端口可用但需 NapCat 主动连）。
        """
        import asyncio

        mode = "client"
        for rb in self._mode_group.buttons():
            if isinstance(rb, QRadioButton) and rb.isChecked():
                mode = rb.property("mode_value") or "client"
                break
        host = self._host_edit.text().strip() or "127.0.0.1"
        port = self._port_spin.value()
        path = self._path_edit.text().strip() or "/"

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._token_input.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            from adapters.napcat.connection import (
                ForwardWSConnection,
                ReverseWSConnection,
            )

            conn = None
            try:
                if mode == "client":
                    ws_url = f"ws://{host}:{port}{path}"
                    conn = ReverseWSConnection(
                        ws_url=ws_url,
                        access_token=token or None,
                        reconnect_interval=1.0,
                        max_reconnect_attempts=1,
                        reconnect_backoff_max=1.0,
                        ping_interval=20.0,
                        ping_timeout=20.0,
                        initial_connect_timeout=3.0,
                    )
                    await conn.start()
                    # 短等以容忍 connect/握手抖动
                    for _ in range(8):
                        if conn.is_connected:
                            break
                        await asyncio.sleep(0.25)
                    if conn.is_connected:
                        self._token_input.set_test_state(
                            "success", f"已连上 NapCat（{ws_url}）"
                        )
                    else:
                        self._token_input.set_test_state(
                            "error",
                            "连不上。检查 NapCat 是否已启动、正向 WS 是否对应同一 host/port/path。",
                        )
                else:  # server
                    conn = ForwardWSConnection(
                        host=host,
                        port=port,
                        path=path,
                        access_token=token or None,
                        ping_interval=20.0,
                        ping_timeout=20.0,
                    )
                    try:
                        await conn.start()
                    except OSError as e:
                        self._token_input.set_test_state(
                            "error", f"端口 {port} 起不来：{e}"
                        )
                        return
                    # 监听 3 秒看 NapCat 有没有真的连入
                    for _ in range(12):
                        if conn.is_connected:
                            break
                        await asyncio.sleep(0.25)
                    if conn.is_connected:
                        self._token_input.set_test_state(
                            "success", f"NapCat 已连入 ws://{host}:{port}{path}"
                        )
                    else:
                        self._token_input.set_test_state(
                            "success",
                            (
                                f"端口可用，已在 {host}:{port}{path} 监听。"
                                "NapCat 还没连入；请去 NapCat 端配置反向 WS 指向这里再触发一次。"
                            ),
                        )
            except Exception as e:  # noqa: BLE001
                self._token_input.set_test_state("error", f"未能完成：{e}")
            finally:
                if conn is not None:
                    try:
                        await conn.stop()
                    except Exception:  # noqa: BLE001
                        pass

        loop.create_task(_do_test())

    def refresh(self) -> None:
        a = self.context.adapter
        rbs = self._mode_group.buttons()
        for rb in rbs:
            if isinstance(rb, QRadioButton):
                rb.setChecked(rb.property("mode_value") == a.mode)
        self._host_edit.setText(a.host)
        self._port_spin.setValue(a.port)
        self._path_edit.setText(a.path)
        if a.token:
            self._token_input.set_text(a.token)
        self._manage_check.setChecked(a.manage_process)
        self._process_path_edit.setText(a.process_path)
        self._whitelist.set_state(a.whitelist)

    def save(self) -> bool:
        mode = "client"
        for rb in self._mode_group.buttons():
            if isinstance(rb, QRadioButton) and rb.isChecked():
                mode = rb.property("mode_value") or "client"
                break
        host = self._host_edit.text().strip()
        path = self._path_edit.text().strip() or "/"
        if not host:
            self.invalid_input.emit("请填一下地址")
            return False
        if self._manage_check.isChecked() and not self._process_path_edit.text().strip():
            self.invalid_input.emit("勾了「托管 NapCat 进程」就要填可执行文件路径")
            return False

        a = self.context.adapter
        a.mode = mode  # type: ignore[assignment]
        a.host = host
        a.port = self._port_spin.value()
        a.path = path
        a.token = self._token_input.text()
        a.manage_process = self._manage_check.isChecked()
        a.process_path = self._process_path_edit.text().strip()
        a.whitelist = self._whitelist.state()
        return True
