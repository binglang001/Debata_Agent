"""图像理解 service —— 通过 LLM provider 的多模态接口描述图片。

适用于支持图片输入的模型：GLM-4V / Qwen-VL / DouBao-Vision-1.5 / GPT-4o 等。
传入 image_url + 可选 prompt，返回文字描述。
"""

from __future__ import annotations

import logging
import json

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

    async def describe(self, image_url: str, prompt: str = "") -> dict[str, str]:
        """返回图片摘要和完整描述。"""
        if not image_url:
            return {"summary": "图片地址为空", "description": "（图片地址为空）"}

        question = prompt.strip()
        effective_prompt = (
            f"{self.DEFAULT_PROMPT}\n\n用户特别想知道：{question}"
            if question
            else self.DEFAULT_PROMPT
        )
        effective_prompt += (
            "\n\n请只返回 JSON 对象，不要包 markdown："
            '{"summary":"60字以内一句话概括","description":"完整描述或针对问题的完整回答"}'
        )
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
            text = f"（图片识别失败：{e}）"
            return {"summary": "图片识别失败", "description": text}

        content = (result.content or "").strip() or "（模型未返回内容）"
        parsed = _parse_json_object(content)
        if parsed:
            summary = str(parsed.get("summary") or "").strip()
            description = str(parsed.get("description") or "").strip()
            if description:
                return {
                    "summary": summary or description[:60],
                    "description": description,
                }
        return {"summary": content[:60], "description": content}


def _parse_json_object(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
