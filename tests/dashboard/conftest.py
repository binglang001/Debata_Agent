"""Dashboard 测试共享 fixture。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
QApplication = QtWidgets.QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])
