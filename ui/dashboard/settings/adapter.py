"""Adapter utility helpers for SettingsPage.

This module is a mechanical split from ``ui.dashboard.settings_page``. Keep
behavior equivalent; do not change adapter connection probing or port binding
logic while moving methods.
"""

from __future__ import annotations

import asyncio
import socket

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_config.schema import NapCatAdapterConfig, WhitelistConfig

from ...theme import Spacing
from ...widgets import show_message
from ...wizard.components import SectionCard, WhitelistEditor, WhitelistState
from ..copy import DASHBOARD_COPY
from .helpers import _progress_slot
from .widgets import CollapsibleSection


class SettingsAdapterMixin:
    # ============================================================
    # 渠道节：adapter 全部可改 + 测试连接
    # ============================================================

    def _rebuild_adapter_form(self) -> None:
        """清空并重建 adapter 节表单（切换 adapter 时调用）。"""
        # 清空旧控件
        while self._adapter_container.count():
            item = self._adapter_container.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._adapter_container.addWidget(self._build_adapter_section())
        self._schedule_settings_content_sync()

    def _build_adapter_section(self) -> SectionCard:
        card = SectionCard(
            title=DASHBOARD_COPY["settings.section_adapter"],
            subtitle="NapCat 连接、白名单。白名单立即生效；其它字段改完需重启。",
        )

        adapter_names = list(self._cfg().adapters.keys())
        if not adapter_names:
            card.add_content(QLabel("未配置任何 adapter。"))
            return card

        # 多 adapter 选择器
        if len(adapter_names) > 1:
            sel_row = QHBoxLayout()
            sel_row.addWidget(QLabel("配置的 Adapter"))
            adapter_combo = QComboBox()
            for aname in adapter_names:
                adapter_combo.addItem(aname, aname)
            # 回填当前选中
            cur_idx = adapter_combo.findData(self._adapter_name)
            if cur_idx >= 0:
                adapter_combo.setCurrentIndex(cur_idx)
            sel_row.addWidget(adapter_combo, 1)
            card.add_layout(sel_row)

            def _on_adapter_switch():
                new_name = adapter_combo.currentData()
                if new_name and new_name != self._adapter_name:
                    self._adapter_name = new_name
                    self._rebuild_adapter_form()

            adapter_combo.currentIndexChanged.connect(_on_adapter_switch)
        else:
            self._adapter_name = adapter_names[0]

        # 总是动态取当前 adapter 的配置
        def _cfg():
            return self._cfg().adapters[self._adapter_name]

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        # 模式
        mode_combo = QComboBox()
        mode_combo.addItem("client（程序连 NapCat 正向 WS）", "client")
        mode_combo.addItem("server（程序监听，NapCat 反向连入）", "server")
        idx = mode_combo.findData(_cfg().mode)
        if idx >= 0:
            mode_combo.setCurrentIndex(idx)
        mode_combo.currentIndexChanged.connect(
            lambda *_: self._on_adapter_field_changed(_cfg(), "mode", mode_combo.currentData())
        )
        form.addRow(QLabel("模式"), mode_combo)

        host_edit = QLineEdit(_cfg().host)
        host_edit.setPlaceholderText("client: NapCat 地址；server: 监听地址，跨设备用 0.0.0.0")
        host_edit.editingFinished.connect(
            lambda h=host_edit: self._on_adapter_field_changed(
                _cfg(),
                "host",
                h.text().strip()
                or ("0.0.0.0" if mode_combo.currentData() == "server" else "127.0.0.1"),
            )
        )
        form.addRow(QLabel("地址"), host_edit)

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(_cfg().port)
        port_spin.editingFinished.connect(
            lambda p=port_spin: self._on_adapter_field_changed(_cfg(), "port", p.value())
        )
        form.addRow(QLabel("端口"), port_spin)

        path_edit = QLineEdit(_cfg().path)
        path_edit.editingFinished.connect(
            lambda e=path_edit: self._on_adapter_field_changed(_cfg(), "path", e.text().strip() or "/")
        )
        form.addRow(QLabel("WebSocket 路径"), path_edit)

        # token 替换
        tok_edit = QLineEdit()
        tok_edit.setEchoMode(QLineEdit.EchoMode.Password)
        tok_edit.setPlaceholderText(
            f"留空 = 保留现有（id={_cfg().access_token_id or '未设'}）；填写则替换"
        )
        tok_edit.editingFinished.connect(lambda e=tok_edit: self._on_adapter_token_changed(_cfg(), e))
        form.addRow(QLabel("Access Token"), tok_edit)

        # 进程托管
        manage_chk = QCheckBox("由 Debata 托管 NapCat 进程")
        manage_chk.setChecked(_cfg().manage_process)
        proc_edit = QLineEdit(_cfg().process_path)
        proc_edit.setPlaceholderText("如 D:/NapCat/start.bat 或 NapCatWinBootMain.exe")
        proc_edit.setVisible(_cfg().manage_process)

        def _on_manage(on: bool) -> None:
            if self._suppress_signals:
                return
            proc_edit.setVisible(on)
            _cfg().manage_process = on
            self._save_now(needs_restart=True, change_desc="adapter.manage_process")

        manage_chk.toggled.connect(_on_manage)
        proc_edit.editingFinished.connect(
            lambda e=proc_edit: self._on_adapter_field_changed(_cfg(), "process_path", e.text().strip())
        )
        manage_row = QVBoxLayout()
        manage_row.addWidget(manage_chk)
        manage_row.addWidget(proc_edit)
        manage_wrap = QWidget()
        manage_wrap.setLayout(manage_row)
        form.addRow(QLabel("进程"), manage_wrap)

        # 测试连接按钮
        test_row = QHBoxLayout()
        test_btn = QPushButton("测试连接")
        test_btn.setProperty("role", "secondary")
        self._adapter_test_status = QLabel("")
        self._adapter_test_status.setProperty("role", "secondary")
        self._adapter_test_progress = QProgressBar()
        self._adapter_test_progress.setRange(0, 100)
        self._adapter_test_progress.setTextVisible(False)
        self._adapter_test_progress.setVisible(False)
        test_btn.clicked.connect(lambda: self._on_test_adapter(_cfg(), test_btn))
        test_row.addWidget(test_btn)
        test_row.addWidget(self._adapter_test_status, 1)
        test_wrap = QWidget()
        test_wrap_layout = QVBoxLayout(test_wrap)
        test_wrap_layout.setContentsMargins(0, 0, 0, 0)
        test_wrap_layout.setSpacing(Spacing.XS)
        test_wrap_layout.addLayout(test_row)
        test_wrap_layout.addWidget(_progress_slot(self._adapter_test_progress))
        form.addRow(QLabel(""), test_wrap)

        card.add_layout(form)

        advanced = CollapsibleSection(
            "NapCat 连接高级参数",
            "这些参数通常不需要改。只有在首条消息经常等待、断线重连异常或托管进程启动过慢时再调整。",
            expanded=False,
        )
        adv_form = QFormLayout()
        adv_form.setSpacing(Spacing.SM)

        startup_timeout = QDoubleSpinBox()
        startup_timeout.setRange(0.0, 30.0)
        startup_timeout.setSingleStep(0.5)
        startup_timeout.setValue(_cfg().startup_connect_timeout_seconds)
        startup_timeout.setSuffix(" 秒")
        startup_timeout.setToolTip("Runtime 启动时最多等待 NapCat 首次连接的时间；超过后后台继续重连。")
        startup_timeout.editingFinished.connect(
            lambda s=startup_timeout: self._on_adapter_field_changed(
                _cfg(), "startup_connect_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("启动等待连接"), startup_timeout)

        api_wait = QDoubleSpinBox()
        api_wait.setRange(0.0, 30.0)
        api_wait.setSingleStep(0.5)
        api_wait.setValue(_cfg().api_wait_connected_timeout_seconds)
        api_wait.setSuffix(" 秒")
        api_wait.setToolTip("调用 OneBot API 前等待连接建立的最长时间。0 表示不等待。")
        api_wait.editingFinished.connect(
            lambda s=api_wait: self._on_adapter_field_changed(
                _cfg(), "api_wait_connected_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("API 前等待连接"), api_wait)

        api_timeout = QDoubleSpinBox()
        api_timeout.setRange(1.0, 300.0)
        api_timeout.setSingleStep(5.0)
        api_timeout.setValue(_cfg().api_timeout_seconds)
        api_timeout.setSuffix(" 秒")
        api_timeout.setToolTip("单次 OneBot API 调用的超时。")
        api_timeout.editingFinished.connect(
            lambda s=api_timeout: self._on_adapter_field_changed(
                _cfg(), "api_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("API 超时"), api_timeout)

        fast_attempts = QSpinBox()
        fast_attempts.setRange(0, 50)
        fast_attempts.setValue(_cfg().fast_reconnect_attempts)
        fast_attempts.setToolTip("断线后的快速重试次数。")
        fast_attempts.editingFinished.connect(
            lambda s=fast_attempts: self._on_adapter_field_changed(
                _cfg(), "fast_reconnect_attempts", s.value()
            )
        )
        adv_form.addRow(QLabel("快速重试次数"), fast_attempts)

        fast_interval = QDoubleSpinBox()
        fast_interval.setRange(0.0, 10.0)
        fast_interval.setSingleStep(0.1)
        fast_interval.setValue(_cfg().fast_reconnect_interval_seconds)
        fast_interval.setSuffix(" 秒")
        fast_interval.setToolTip("快速重试阶段每次等待多久。")
        fast_interval.editingFinished.connect(
            lambda s=fast_interval: self._on_adapter_field_changed(
                _cfg(), "fast_reconnect_interval_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("快速重试间隔"), fast_interval)

        reconnect_interval = QDoubleSpinBox()
        reconnect_interval.setRange(0.1, 120.0)
        reconnect_interval.setSingleStep(0.5)
        reconnect_interval.setValue(_cfg().reconnect_interval_seconds)
        reconnect_interval.setSuffix(" 秒")
        reconnect_interval.setToolTip("快速重试结束后的指数退避起点。")
        reconnect_interval.editingFinished.connect(
            lambda s=reconnect_interval: self._on_adapter_field_changed(
                _cfg(), "reconnect_interval_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("慢速重连起点"), reconnect_interval)

        backoff_max = QDoubleSpinBox()
        backoff_max.setRange(1.0, 600.0)
        backoff_max.setSingleStep(5.0)
        backoff_max.setValue(_cfg().reconnect_backoff_max_seconds)
        backoff_max.setSuffix(" 秒")
        backoff_max.setToolTip("指数退避的最大等待时间。")
        backoff_max.editingFinished.connect(
            lambda s=backoff_max: self._on_adapter_field_changed(
                _cfg(), "reconnect_backoff_max_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("退避上限"), backoff_max)

        jitter = QDoubleSpinBox()
        jitter.setRange(0.0, 10.0)
        jitter.setSingleStep(0.1)
        jitter.setValue(_cfg().reconnect_jitter_seconds)
        jitter.setSuffix(" 秒")
        jitter.setToolTip("慢速重连等待的随机抖动上限，避免固定节拍重试。")
        jitter.editingFinished.connect(
            lambda s=jitter: self._on_adapter_field_changed(
                _cfg(), "reconnect_jitter_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("重连抖动"), jitter)

        max_reconnect = QSpinBox()
        max_reconnect.setRange(-1, 100000)
        max_reconnect.setValue(_cfg().max_reconnect_attempts)
        max_reconnect.setSpecialValueText("无限")
        max_reconnect.setToolTip("-1 表示无限重连。")
        max_reconnect.editingFinished.connect(
            lambda s=max_reconnect: self._on_adapter_field_changed(
                _cfg(), "max_reconnect_attempts", s.value()
            )
        )
        adv_form.addRow(QLabel("最大重连次数"), max_reconnect)

        ping_interval = QDoubleSpinBox()
        ping_interval.setRange(1.0, 300.0)
        ping_interval.setSingleStep(5.0)
        ping_interval.setValue(_cfg().ping_interval_seconds)
        ping_interval.setSuffix(" 秒")
        ping_interval.setToolTip("WebSocket ping 间隔。")
        ping_interval.editingFinished.connect(
            lambda s=ping_interval: self._on_adapter_field_changed(
                _cfg(), "ping_interval_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("Ping 间隔"), ping_interval)

        ping_timeout = QDoubleSpinBox()
        ping_timeout.setRange(1.0, 300.0)
        ping_timeout.setSingleStep(5.0)
        ping_timeout.setValue(_cfg().ping_timeout_seconds)
        ping_timeout.setSuffix(" 秒")
        ping_timeout.setToolTip("WebSocket ping 未响应多久判定断线。")
        ping_timeout.editingFinished.connect(
            lambda s=ping_timeout: self._on_adapter_field_changed(
                _cfg(), "ping_timeout_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("Ping 超时"), ping_timeout)

        warmup = QDoubleSpinBox()
        warmup.setRange(0.0, 60.0)
        warmup.setSingleStep(0.5)
        warmup.setValue(_cfg().process_warmup_seconds)
        warmup.setSuffix(" 秒")
        warmup.setToolTip("托管 NapCat 进程启动后，连接前等待的时间。")
        warmup.editingFinished.connect(
            lambda s=warmup: self._on_adapter_field_changed(
                _cfg(), "process_warmup_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("托管进程预热"), warmup)

        voice_delay = QDoubleSpinBox()
        voice_delay.setRange(0.0, 8.0)
        voice_delay.setSingleStep(0.5)
        voice_delay.setValue(_cfg().voice_fetch_delay_seconds)
        voice_delay.setSuffix(" 秒")
        voice_delay.setToolTip("调用 NapCat 语音转文字前先等待多久。")
        voice_delay.editingFinished.connect(
            lambda s=voice_delay: self._on_adapter_field_changed(
                _cfg(), "voice_fetch_delay_seconds", s.value()
            )
        )
        adv_form.addRow(QLabel("语音转写等待"), voice_delay)

        advanced.add_layout(adv_form)
        card.add_content(advanced)

        # 白名单（hot，立即生效）
        sep = QFrame()
        sep.setProperty("role", "separator")
        card.add_content(sep)
        wl_title = QLabel("白名单（立即生效）")
        wl_title.setProperty("role", "title-3")
        card.add_content(wl_title)

        cfg_snapshot = _cfg()
        current = WhitelistState(
            mode=cfg_snapshot.whitelist.mode,
            qq_ids=[str(x) for x in cfg_snapshot.whitelist.qq_ids],
            group_ids=[str(x) for x in cfg_snapshot.whitelist.group_ids],
        )
        wl_editor = WhitelistEditor(
            initial=current,
            on_open_confirm=lambda: bool(show_message(
                self, "对所有人开放？",
                "陌生人也能让 Debata 回复，可能产生意外的 API 费用。",
                confirm_text="我清楚了", cancel_text="算了", is_danger=True,
            )),
        )

        def _on_wl(state: WhitelistState) -> None:
            if self._suppress_signals:
                return
            wl = WhitelistConfig(
                mode=state.mode,
                qq_ids=[int(x) for x in state.qq_ids if x.isdigit()],
                group_ids=[int(x) for x in state.group_ids if x.isdigit()],
            )
            _cfg().whitelist = wl
            self._save_now(needs_restart=False, change_desc="adapter.whitelist (hot)")

        wl_editor.state_changed.connect(_on_wl)
        card.add_content(wl_editor)
        return card

    def _on_adapter_field_changed(self, cfg: NapCatAdapterConfig, field: str, value) -> None:
        if self._suppress_signals:
            return
        if getattr(cfg, field) == value:
            return
        setattr(cfg, field, value)
        self._save_now(needs_restart=True, change_desc=f"adapter.{field}")

    def _on_adapter_token_changed(self, cfg: NapCatAdapterConfig, edit: QLineEdit) -> None:
        if self._suppress_signals:
            return
        new = edit.text()
        if not new:
            return
        sid = cfg.access_token_id or "napcat_default_token"
        cfg.access_token_id = sid
        self._set_secret(sid, new)
        edit.clear()
        self._save_now(needs_restart=True, change_desc="adapter.access_token")

    def _on_test_adapter(
        self,
        cfg: NapCatAdapterConfig,
        button: QPushButton | None = None,
    ) -> None:
        """非侵入式测试渠道状态。

        设置页不能创建第二条 NapCat WebSocket：NapCat/OneBot 端常见单连接，
        临时连接会挤掉正在收消息的主连接，随后 stop 临时连接会让后续收不到消息。
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._adapter_test_status.setText("⚠ 事件循环未就绪")
            return

        self._adapter_test_status.setText("正在测试……")
        self._adapter_test_progress.setVisible(True)
        self._adapter_test_progress.setRange(0, 0)
        if button is not None:
            button.setEnabled(False)
            button.setText("测试中")

        async def _do_test() -> None:
            try:
                running = self._running_adapter_for_current_page()
                if running is not None:
                    if getattr(running, "is_connected", False):
                        self._adapter_test_status.setText("✓ 当前 Runtime 渠道已连接")
                    else:
                        self._adapter_test_status.setText("⚠ 当前 Runtime 渠道未连接，后台会继续重连")
                elif cfg.mode == "client":
                    ok = await self._probe_tcp_port(cfg.host, cfg.port)
                    if ok:
                        self._adapter_test_status.setText(
                            f"✓ TCP 端口可达 ws://{cfg.host}:{cfg.port}{cfg.path}（未建立 WS 会话）"
                        )
                    else:
                        self._adapter_test_status.setText("✗ TCP 端口不可达，检查 NapCat 是否启动 / 地址端口")
                else:
                    available = await asyncio.to_thread(
                        self._can_bind_adapter_port, cfg.host, cfg.port
                    )
                    if available:
                        self._adapter_test_status.setText(
                            f"✓ 本机端口可监听 ws://{cfg.host}:{cfg.port}{cfg.path}；保存重启后等待 NapCat 连入"
                        )
                    else:
                        self._adapter_test_status.setText(
                            "⚠ 本机端口已被占用；如果 Debata 正在运行，这是正常的"
                        )
            except Exception as e:  # noqa: BLE001
                self._adapter_test_status.setText(f"✗ 未能完成：{e}")
            finally:
                self._adapter_test_progress.setRange(0, 100)
                self._adapter_test_progress.setValue(100)
                self._adapter_test_progress.setVisible(False)
                if button is not None:
                    button.setEnabled(True)
                    button.setText("测试连接")

        loop.create_task(_do_test())

    def _running_adapter_for_current_page(self):
        adapter = getattr(self._runtime, "adapter", None)
        if adapter is None:
            return None
        if getattr(adapter, "name", "") != getattr(self, "_adapter_name", ""):
            return None
        return adapter

    @staticmethod
    async def _probe_tcp_port(host: str, port: int, *, timeout: float = 2.0) -> bool:
        writer = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            return True
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    @staticmethod
    def _can_bind_adapter_port(host: str, port: int) -> bool:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
                return True
        except OSError:
            return False
