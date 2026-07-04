"""State/save helpers for SettingsPage.

This module is a mechanical split from ``ui.dashboard.settings_page``. Keep
behavior equivalent; do not change save, baseline, restart, or change-count
logic while moving methods.
"""

from __future__ import annotations

import logging
from copy import deepcopy

from PySide6.QtWidgets import QApplication

from app_config.loader import save_config

from ...widgets import show_message

logger = logging.getLogger("ui.dashboard.settings_page")


class SettingsStateMixin:
    def _cfg(self):
        return self._runtime.config

    def _save_now(self, *, needs_restart: bool, change_desc: str = "") -> None:
        try:
            save_config(self._runtime.paths, self._cfg())
            if change_desc:
                logger.info(f"设置已保存: {change_desc}")
            self._baseline = deepcopy(self._cfg())
            self._status.set_changes(self._count_changes(), needs_restart=needs_restart)
        except Exception as e:  # noqa: BLE001
            logger.exception("保存设置失败")
            self._status.mark_error(f"保存失败：{e}")

    def _restore_opened_config(self) -> None:
        try:
            self._runtime.config = deepcopy(self._opened_snapshot)
            save_config(self._runtime.paths, self._runtime.config)
            self._baseline = deepcopy(self._runtime.config)
            self._status.set_changes(0, needs_restart=True)
            self.refresh()
            logger.info("设置已恢复到打开设置页时的配置")
        except Exception as e:  # noqa: BLE001
            logger.exception("恢复设置失败")
            self._status.mark_error(f"恢复失败：{e}")

    def _count_changes(self) -> int:
        """比对当前配置与基线，返回字段级差异数。"""
        try:
            import json
            cur = json.loads(self._cfg().model_dump_json(exclude_none=True))
            base = json.loads(self._baseline.model_dump_json(exclude_none=True))
        except Exception:
            return 0

        missing = object()

        def _leaf_count(value) -> int:
            if value is missing:
                return 0
            if isinstance(value, dict):
                return sum(_leaf_count(v) for v in value.values()) or 1
            if isinstance(value, list):
                return sum(_leaf_count(v) for v in value) or 1
            return 1

        def _diff(a, b) -> int:
            if a == b:
                return 0
            if a is missing:
                return _leaf_count(b)
            if b is missing:
                return _leaf_count(a)
            if isinstance(a, dict) and isinstance(b, dict):
                n = 0
                for k in set(a.keys()) | set(b.keys()):
                    n += _diff(a.get(k, missing), b.get(k, missing))
                return n
            if isinstance(a, list) and isinstance(b, list):
                n = 0
                for i in range(max(len(a), len(b))):
                    va = a[i] if i < len(a) else missing
                    vb = b[i] if i < len(b) else missing
                    n += _diff(va, vb)
                return n
            return 1

        return _diff(cur, base)

    def _set_secret(self, sid: str, value: str) -> None:
        if not value:
            return
        try:
            self._runtime.secrets.set(sid, value)
        except Exception as e:  # noqa: BLE001
            logger.exception("写入 secrets 失败")
            self._status.mark_error(f"密钥写入失败：{e}")

    def _on_restart_clicked(self) -> None:
        app = QApplication.instance()
        focus = app.focusWidget() if app is not None else None
        if focus is not None:
            focus.clearFocus()
        ok = show_message(
            self,
            "重启 Debata 服务",
            "将停止当前 Runtime 并重新启动，使所有需要重启生效的修改生效。\n\n"
            "短暂期间 NapCat 会断开几秒钟，没收到的消息会在重连后补上。",
            confirm_text="重启",
            cancel_text="再想想",
        )
        if ok:
            self._status.mark_busy("正在重启 Debata 服务……")
            self.restart_runtime_requested.emit()

    def on_runtime_restart_finished(self, ok: bool, message: str = "") -> None:
        if ok:
            self._status.mark_restart_done()
            return
        self._status.mark_error(message or "重启失败，请查看日志。")
