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

from .base import StatusCallback, UsageRecorder

logger = logging.getLogger(__name__)


class SummaryAgent:
    """对话历史总结器。"""

    def __init__(
        self,
        provider: IProvider,
        cfg: AgentConfig,
        summarize_cfg: SummarizeConfig,
        *,
        usage_recorder: UsageRecorder | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.summarize_cfg = summarize_cfg
        self.usage_recorder = usage_recorder
        self.status_callback = status_callback

    async def summarize_rolling(
        self,
        history_slice: list[dict[str, Any]],
        existing_summary_text: str,
        existing_important_text: str,
    ) -> dict[str, Any] | None:
        """把一段旧历史并入全局滚动摘要。

        返回 {"summary_text": str, "new_important": [{"timestamp","content"}, ...]}。
        """
        if not history_slice:
            return None

        history_text = "\n".join(_format_history_line(m) for m in history_slice)
        prompt = (
            "你是当前角色的记忆管理系统。请把一段旧对话历史并入全局滚动摘要。\n\n"
            f"<现有滚动摘要>\n{existing_summary_text or '（无）'}\n</现有滚动摘要>\n\n"
            f"<现存重要记忆>\n{existing_important_text or '（无）'}\n</现存重要记忆>\n\n"
            f"<待归档对话>\n{history_text}\n</待归档对话>\n\n"
            "<任务>\n"
            "1. 输出合并后的滚动摘要，保留人物关系、长期约定、未完成事项、关键决定和跨会话背景。\n"
            "2. 不要逐条流水复述，不要保留一次性工具执行细节。\n"
            "3. 根据待归档对话提取少量值得长期保存的新重要记忆补充候选；没有则返回空数组。\n"
            "</任务>\n\n"
            "<重要记忆规范>\n"
            "1. 现存重要记忆是已经保存的事实，不要重复保存；new_important 只是补充候选，不是完整替换列表。\n"
            "2. new_important 最多返回 3-5 条候选；只保存长期稳定事实：人物身份、偏好、稳定约定、长期目标、"
            "管理员反馈或系统行为改进。\n"
            "3. 每条 content 必须客观、完整、有明确主语；已有同主体事实应补充或合并，不要重复新增。\n"
            "4. 禁止保存系统消息、task_context、send_receipt、工具结果、no_action、临时 URL、密钥、token、"
            "cookie、rkey、clientkey 等运行时噪声。\n"
            "5. scope 可选，只能是 global、user:QQ、group:群号；不能判断就省略，让调用方保持旧行为。\n"
            "6. 历史里的 conversation_id=private:QQ 如果用于 scope，应写成 user:QQ；"
            "conversation_id=group:群号 应写成 group:群号；不要把 private:QQ 当 scope 返回。\n"
            "7. pinned 可选，只用于任何场景都必须常驻的极稳定事实；普通偏好不要设置 pinned。\n"
            "</重要记忆规范>\n\n"
            "返回 JSON：\n"
            "```json\n"
            '{"summary_text": "合并后的滚动摘要", '
            '"new_important": [{"content": "一句话描述", "scope": "user:123456", "pinned": false}, ...]}\n'
            "```"
        )

        try:
            self._emit_status("thinking", "滚动摘要生成中")
            result = await self.provider.chat_completion(
                [
                    {
                        "role": "system",
                        "content": "你是当前角色的记忆管理系统。负责滚动压缩旧对话。",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.cfg.model,
                tools=None,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_tokens=self.cfg.max_tokens,
                reasoning=self._to_provider_reasoning(),
                stream=True,
                timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
            )
            await self._record_usage(result.usage, operation="rolling_summary")
        except ProviderError as e:
            logger.error(f"滚动摘要失败（API）: {e}")
            self._emit_status("error", "滚动摘要失败")
            return None
        except Exception as e:
            logger.exception(f"滚动摘要异常: {e}")
            self._emit_status("error", "滚动摘要异常")
            return None

        parsed = _parse_json_object(result.content or "")
        if not parsed:
            logger.error(f"滚动摘要返回无 JSON：{(result.content or '')[:200]}")
            return None
        summary_text = str(parsed.get("summary_text") or "").strip()
        new_important = parsed.get("new_important", [])
        if not isinstance(new_important, list):
            new_important = []
        return {"summary_text": summary_text, "new_important": new_important}

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
            self._emit_status("thinking", "历史总结生成中")
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
                stream=True,
                timeout=self.cfg.first_token_timeout_seconds * 6 + 60.0,
                first_token_timeout=self.cfg.first_token_timeout_seconds * 2,
            )
            await self._record_usage(result.usage, operation="history_summary")
        except ProviderError as e:
            logger.error(f"记忆总结失败（API）: {e}")
            self._emit_status("error", "历史总结失败")
            return None
        except Exception as e:
            logger.exception(f"记忆总结异常: {e}")
            self._emit_status("error", "历史总结异常")
            return None

        parsed = _parse_json_object(result.content or "")
        if not parsed:
            logger.error(f"总结返回无 JSON：{(result.content or '')[:200]}")
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

    async def _record_usage(self, usage, **metadata: Any) -> None:
        if self.usage_recorder is None:
            return
        try:
            await self.usage_recorder(
                usage,
                {
                    "provider": self.provider.name,
                    "model": self.cfg.model,
                    "agent": "历史总结",
                    **metadata,
                },
            )
        except Exception:
            logger.debug("记录总结模型用量失败", exc_info=True)

    def _emit_status(self, state: str, text: str) -> None:
        if self.status_callback is None:
            return
        try:
            self.status_callback(
                {
                    "state": state,
                    "text": text,
                    "model": self.cfg.model,
                    "agent": "历史总结",
                }
            )
        except Exception:
            logger.debug("更新总结模型状态失败", exc_info=True)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        logger.error(f"总结 JSON 解析失败: {e}, raw={text[start:end][:200]}")
        return None
    return parsed if isinstance(parsed, dict) else None


def _format_history_line(record: dict[str, Any]) -> str:
    role = record.get("role", "?")
    conversation_id = str(record.get("conversation_id") or "").strip()
    scope_hint = f" conversation_id={conversation_id}" if conversation_id else ""
    return f"[{role}{scope_hint}] {record.get('content', '') or ''}"
