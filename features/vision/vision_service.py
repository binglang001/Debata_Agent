"""图像理解 service —— 通过 LLM provider 的多模态接口描述图片。

适用于支持图片输入的模型：GLM-4V / Qwen-VL / DouBao-Vision-1.5 / GPT-4o 等。
传入 image_url + 可选 prompt，返回文字描述。
"""

from __future__ import annotations

import logging

from providers.base import IProvider, ProviderError

logger = logging.getLogger(__name__)


class VisionService:
    """图像理解服务。"""

    DEFAULT_PROMPT = (
        "请描述这张图片的内容。如有文字请准确转写；如是表情或动作请说明含义；"
        "如是截图请说明界面看起来是什么应用、显示了什么。"
    )

    def __init__(
        self,
        provider: IProvider,
        model: str,
        *,
        max_tokens: int = 1024,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    async def describe(self, image_url: str, prompt: str = "") -> str:
        """返回图片的文字描述。"""
        if not image_url:
            return "（图片地址为空）"

        effective_prompt = prompt.strip() or self.DEFAULT_PROMPT
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": effective_prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]
        try:
            result = await self.provider.chat_completion(
                messages,
                model=self.model,
                tools=None,
                temperature=0.3,
                max_tokens=self.max_tokens,
                stream=False,
                timeout=self.timeout_seconds,
                first_token_timeout=self.timeout_seconds,
            )
        except ProviderError as e:
            logger.warning(f"vision describe 失败: {e}")
            return f"（图片识别失败：{e}）"

        return (result.content or "").strip() or "（模型未返回内容）"
