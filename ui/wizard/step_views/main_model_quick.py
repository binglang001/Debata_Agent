"""主模型 - 推荐路径：DeepSeek 一键。"""

from __future__ import annotations

import asyncio

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ...theme import Spacing
from ..components import ApiKeyInput, SectionCard
from ..context import BaseStepView, WizardContext
from ..copy import COPY

_DEEPSEEK_TUTORIAL_MD = """
## 怎么拿到 DeepSeek API 密钥

1. 打开 [platform.deepseek.com](https://platform.deepseek.com/) 并注册 / 登录
2. 左侧菜单 → **API Keys** → **创建新 API Key**
3. 给密钥取个名字（如 "Debata"），创建后**立刻复制**（页面关闭后看不到完整 key）
4. 回到本向导粘贴进密钥框

## 默认模型与计费

- 默认走 `deepseek-v4-flash`（DeepSeek V4 系列轻量版，1M 上下文）
- 缓存未命中输入约 ¥1 / 百万 token，输出约 ¥2 / 百万 token，命中缓存几乎免费
- 第一次使用建议先充值 5 元，够测试很久
- 充值入口：左侧菜单 → 充值
- 老模型 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 后停用

## 如果连接失败

- 提示「此密钥不被接受」：确认密钥复制完整（开头 `sk-`，长度约 30+）
- 提示「网络似乎不通」：检查代理设置，或换个网络重试
- 提示「账户余额不足」：去控制台充值
"""


class MainModelQuickStepView(BaseStepView):
    """DeepSeek 一键配置：密钥输入 + 测试连接 + 教程链接。"""

    def __init__(self, context: WizardContext, parent=None) -> None:
        super().__init__(context, parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.MD)

        card = SectionCard(
            title="选个主模型",
            subtitle=(
                "推荐使用 DeepSeek——中文表现好、价格亲民。\n"
                "从官网获取一个 API 密钥粘贴在下方。"
            ),
        )
        outer.addWidget(card)

        # 密钥输入
        label = QLabel(COPY["main_model_quick.api_key_label"])
        label.setProperty("role", "title-3")
        card.add_content(label)

        self._key_input = ApiKeyInput(
            placeholder=COPY["main_model_quick.api_key_placeholder"],
        )
        self._key_input.test_requested.connect(self._on_test)
        card.add_content(self._key_input)

        # 教程 / 帮助文字
        help_lbl = QLabel(COPY["main_model_quick.help_text"])
        help_lbl.setProperty("role", "secondary")
        help_lbl.setWordWrap(True)
        card.add_content(help_lbl)

        # 文字按钮：前往领取 / 查看教程
        link_row = QHBoxLayout()
        link_row.setSpacing(Spacing.MD)

        self._get_key_btn = QPushButton(COPY["button.get_api_key"])
        self._get_key_btn.setProperty("role", "text")
        self._get_key_btn.clicked.connect(self._open_get_key_page)
        link_row.addWidget(self._get_key_btn)

        self._tutorial_btn = QPushButton(COPY["button.view_tutorial"])
        self._tutorial_btn.setProperty("role", "text")
        self._tutorial_btn.clicked.connect(self._open_tutorial)
        link_row.addWidget(self._tutorial_btn)

        link_row.addStretch(1)
        card.add_layout(link_row)

        outer.addStretch(1)

    def refresh(self) -> None:
        if self.context.main.api_key:
            self._key_input.set_text(self.context.main.api_key)

    def save(self) -> bool:
        key = self._key_input.text().strip()
        if not key:
            self.invalid_input.emit("先填一下 DeepSeek API 密钥")
            return False
        self.context.main.preset = "deepseek"
        self.context.main.display_name = "DeepSeek"
        self.context.main.api_key = key
        self.context.main.model = "deepseek-v4-flash"
        self.context.main.temperature = 0.6
        self.context.main.max_tokens = 16384
        return True

    async def validate_before_next(self) -> bool:
        if self._key_input.is_test_success():
            return True
        key = self._key_input.text().strip()
        self._key_input.set_test_state("testing", "正在自动测试主模型连接……")
        ok, message = await self._test_key(key)
        self._key_input.set_test_state("success" if ok else "error", message)
        if not ok:
            self.invalid_input.emit(message)
        return ok

    # ---- 内部 ----

    async def _test_key(self, key: str) -> tuple[bool, str]:
        try:
            from providers import probe_provider_endpoint

            result = await probe_provider_endpoint(
                protocol="openai_compat",
                base_url="https://api.deepseek.com/v1",
                api_key=key,
                model="deepseek-v4-flash",
                timeout_seconds=8.0,
            )
            if result.status == "ok":
                return True, COPY["main_model_quick.test_success"]
            return False, result.message
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "401" in msg or "unauthorized" in msg or "invalid" in msg:
                text = COPY["main_model_quick.test_fail_401"]
            elif "402" in msg or "balance" in msg or "insufficient" in msg:
                text = COPY["main_model_quick.test_fail_balance"]
            elif "timeout" in msg or "network" in msg or "connect" in msg:
                text = COPY["main_model_quick.test_fail_network"]
            else:
                text = f"未能完成：{e}"
            return False, text

    def _on_test(self, key: str) -> None:
        """启动一个 asyncio 任务测试 DeepSeek 连接。

        通过 qasync 的事件循环来跑（GUI 启动时已经把 loop 装好了）。
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._key_input.set_test_state("error", "事件循环未就绪")
            return

        async def _do_test() -> None:
            ok, message = await self._test_key(key)
            self._key_input.set_test_state("success" if ok else "error", message)

        loop.create_task(_do_test())

    def _open_get_key_page(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl("https://platform.deepseek.com/api_keys"))

    def _open_tutorial(self) -> None:
        from ..components import TutorialDialog

        dlg = TutorialDialog("DeepSeek API 密钥获取教程", _DEEPSEEK_TUTORIAL_MD, self)
        dlg.exec()
