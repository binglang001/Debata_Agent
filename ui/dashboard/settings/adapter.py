"""Adapter utility helpers for SettingsPage.

This module is a mechanical split from ``ui.dashboard.settings_page``. Keep
behavior equivalent; do not change adapter connection probing or port binding
logic while moving methods.
"""

from __future__ import annotations

import asyncio
import socket


class SettingsAdapterMixin:
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
