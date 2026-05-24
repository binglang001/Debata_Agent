"""火山引擎 Doubao 视觉模型 API —— 图片 URL → base64 → 描述"""

import base64
import os
import tempfile

import httpx
from nonebot.log import logger
from openai import AsyncOpenAI

VISION_MODEL = os.getenv("VOLCENGINE_VISION_MODEL", "doubao-seed-1-6-vision-250815")
VISION_PROMPT = "用中文简要描述这张图片的内容。包括：画面主体、文字内容、人物表情动作、场景氛围。2-5句话，只输出描述，不要额外文字。"

_client: AsyncOpenAI | None = None
_disabled: bool = False


def _get_client() -> AsyncOpenAI:
    global _client, _disabled
    if _client is None:
        api_key = os.getenv("VOLCENGINE_API_KEY", "")
        if not api_key:
            _disabled = True
            logger.warning("VOLCENGINE_API_KEY 未设置，视觉理解不可用")
        else:
            _client = AsyncOpenAI(
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                api_key=api_key,
            )
    return _client


async def _download_image(url: str) -> bytes:
    """下载图片，返回二进制数据。"""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def describe_image(image_url: str, prompt: str = "") -> str:
    """下载图片并调用豆包视觉模型获取描述。prompt 可选，不填用默认。"""
    if _disabled:
        return "[视觉功能未配置]"
    client = _get_client()
    if client is None:
        return "[视觉功能未配置]"
    tmp_path = ""
    try:
        data = await _download_image(image_url)
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", prefix="diana_img_", delete=False
        ) as f:
            f.write(data)
            tmp_path = f.name

        b64 = base64.b64encode(data).decode()
        data_uri = f"data:image/jpeg;base64,{b64}"

        logger.info(f"视觉理解: 图片已下载 {len(data)} bytes")

        completion = await client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt or VISION_PROMPT},
                ],
            }],
            max_tokens=32768,
            temperature=0.3,
            timeout=30,
        )
        description = (completion.choices[0].message.content or "").strip()
        logger.info(f"视觉理解完成: {description[:60]}")

        return description or "[空描述]"

    except Exception as e:
        logger.error(f"视觉理解失败: {e}")
        return "[图片无法识别]"
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
