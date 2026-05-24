"""pytest 全局 fixture。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 让测试能 import 项目根的包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class FakeKeyringBackend:
    """内存版 keyring 后端，避免污染系统密钥环。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.priority = 100

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture
def fake_keyring(monkeypatch):
    """用内存 keyring 替换系统 keyring。每个测试独立。"""
    import keyring

    fake = FakeKeyringBackend()
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    yield fake


@pytest.fixture
def tmp_paths(tmp_path):
    """提供一个 AppPaths 实例，所有路径都在 tmp_path 下。"""
    from app_config.paths import AppPaths

    paths = AppPaths(project_root=tmp_path)
    paths.ensure_data_dirs()
    return paths
