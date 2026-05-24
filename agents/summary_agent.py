"""记忆总结 Agent —— 历史超长时调用小模型提取重要信息并截断。

V1 行为：
    当 history >= SUMMARIZE_AT（默认 20000）时触发：
    1. 取前 SUMMARIZE_RANGE_END（默认 11000）条 + 现存重要记忆
    2. 让模型选 cut_point（在 [range_start, range_end] 之间的语义完整点）
    3. 提取新增重要信息
    4. 调用方据此调用 history.truncate_head(cut_point) + important.replace_all(new_important)

V2 改进：
    - 总结的提示词模板独立可调
    - 与 LLM 解耦：summarize() 返回 dict，由调用方持久化
    - 错误处理更明确
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app_config.schema import AgentConfig, SummarizeConfig
from providers.base import IProvider, ProviderError, ReasoningConfig

logger = logging.getLogger(__name__)


class SummaryAgent:
    """对话历史总结器。"""

    def __init__(
        self,
        provider: IProvider,
        cfg: AgentConfig,
        summarize_cfg: SummarizeConfig,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.summarize_cfg = summarize_cfg

    async def summarize(
        self,
        history_slice: list[dict[str, Any]],
        existing_important_text: str,
    ) -> dict[str, Any] | None:
        """返回 {"cut_point": int, "new_important": [{"timestamp", "content"}, ...]} 或 None。"""
        if not history_slice:
            return None

        history_text = "\n".join(
            f"[{m.get('role', '?')}] {m.get('content', '') or ''}"
            for m in history_slice
        )
        range_start = self.summarize_cfg.range_start_messages
        range_end = self.summarize_cfg.range_end_messages

        prompt = (
            f"你是当前角色的记忆管理系统。以下是该角色的前 {len(history_slice)} 条对话历史。\n\n"
            f"<现存重要记忆>\n{existing_important_text or '（无）'}\n</现存重要记忆>\n\n"
            f"<对话历史>\n{history_text}\n</对话历史>\n\n"
            f"<任务>\n"
            f"1. 从对话历史中提取新增的重要信息（人名、关系、约定、秘密、偏好等），"
            f"不要存日常闲聊。\n"
            f"2. 检查历史中第 {range_start}~{range_end} 条之间，"
            f"找一个语义完整的位置（话说完、话题转换处）作为截断点。\n"
            f"3. 合并现存重要记忆和新提取的记忆，去重，形成新一份重要记忆。\n"
            f"</任务>\n\n"
            f"返回 JSON：\n"
            f"```json\n"
            f'{{"cut_point": <数字>, "new_important": '
            f'[{{"timestamp": "时间", "content": "一句话描述"}}, ...]}}\n'
            f"```\n\n"
            f"注意：cut_point 必须在 {range_start}~{range_end} 之间。"
        )

        try:
            # 总结一次性吐 8k+ tokens，整体 timeout 比首 token 宽得多
            result = await self.provider.chat_completion(
                [
                    {
                        "role": "system",
                        "content": "你是当前角色的记忆管理系统。"
                        "从对话历史中提取重要信息并确定截断点。",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=self.cfg.max_tokens,
                reasoning=self._to_provider_reasoning(),
                stream=False,
                timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
            )
        except ProviderError as e:
            logger.error(f"记忆总结失败（API）: {e}")
            return None
        except Exception as e:
            logger.exception(f"记忆总结异常: {e}")
            return None

        text = (result.content or "").strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end <= start:
            logger.error(f"总结返回无 JSON：{text[:200]}")
            return None

        try:
            parsed = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            logger.error(f"总结 JSON 解析失败: {e}, raw={text[start:end][:200]}")
            return None

        cut_point = parsed.get("cut_point")
        if not isinstance(cut_point, int) or cut_point < 0:
            logger.warning(f"总结返回的 cut_point 无效: {cut_point}")
            return None
        # 限制在范围内
        cut_point = max(range_start, min(cut_point, range_end))

        new_important = parsed.get("new_important", [])
        if not isinstance(new_important, list):
            new_important = []

        return {
            "cut_point": cut_point,
            "new_important": new_important,
        }

    def _to_provider_reasoning(self) -> ReasoningConfig | None:
        if self.cfg.reasoning is None:
            return None
        return ReasoningConfig(
            enabled=self.cfg.reasoning.enabled,
            budget=self.cfg.reasoning.budget,
            max_tokens=self.cfg.reasoning.max_tokens,
        )


class DuplicateChecker:
    """重要记忆去重判定器（用小模型）。

    旧版的 check_important_memory_duplicate。
    """

    def __init__(
        self,
        provider: IProvider,
        cfg: AgentConfig,
    ) -> None:
        self.provider = provider
        self.cfg = cfg

    async def __call__(self, existing: list[dict[str, Any]], new_text: str) -> bool:
        if not existing or not new_text:
            return False

        existing_list = "\n".join(f"- {m.get('content', '')}" for m in existing)
        prompt = (
            f"现有重要记忆：\n{existing_list}\n\n"
            f"新记忆：{new_text}\n\n"
            f"新记忆的信息是否已被现有记忆覆盖？回复 JSON："
            f'{{"duplicate": true/false}}'
        )

        try:
            # 去重判定只要一个布尔结果，用 cfg 的温度和窗口
            result = await self.provider.chat_completion(
                [{"role": "user", "content": prompt}],
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=64,
                reasoning=None,
                stream=False,
                timeout=self.cfg.first_token_timeout_seconds,
                first_token_timeout=self.cfg.first_token_timeout_seconds,
            )
        except Exception as e:
            logger.warning(f"去重检查失败: {e}，默认不跳过")
            return False

        text = (result.content or "").strip().lower()
        return '"duplicate": true' in text or '"duplicate":true' in text
