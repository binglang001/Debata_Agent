"""日志页回归测试。"""

from __future__ import annotations

import logging
import sys

from ui.dashboard.logs_page import LogsPage, _format_record


def test_log_detail_format_includes_exception():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("tests.demo").makeRecord(
            "tests.demo",
            logging.ERROR,
            __file__,
            1,
            "failed: %s",
            ("x",),
            exc_info=sys.exc_info(),
        )

    text = _format_record(record, single_line=False)

    assert "模块：tests.demo" in text
    assert "RuntimeError: boom" in text


def test_logs_page_trims_visible_items_to_max_buffer(qapp):
    page = LogsPage(None)
    logger = logging.getLogger("tests.dashboard.logs")
    old_level = logger.level
    old_propagate = logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(page._handler)
    try:
        for i in range(3000):
            logger.info("dense log %s", i)
        for _ in range(10):
            qapp.processEvents()

        assert page._list.count() <= page.MAX_BUFFER
        assert len(page._records) <= page.MAX_BUFFER
        assert "dense log 1000" in page._list.item(0).text()
        assert "dense log 2999" in page._list.item(page._list.count() - 1).text()
    finally:
        logger.removeHandler(page._handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate
        page.close()
        page.deleteLater()
