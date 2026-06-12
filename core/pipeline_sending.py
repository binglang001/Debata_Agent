"""Sending helper methods for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change collected-send, send-receipt, scheduled-send, or
outbound timeline semantics while moving methods.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from adapters.types import Target
from utils import get_time

from .pipeline_context import _make_runtime_context_record

logger = logging.getLogger(__name__)


class PipelineSendingMixin:
    async def _execute_collected(
        self,
        collected: list[dict],
    ) -> bool:
        """逐条发送遗留 collected 动作。

        Phase 0 起，send_private_messages / send_group_message / send_voice_message
        都在工具调用内即时发送。该函数只保留给定时直接发送、旧测试与未来少量
        兼容动作使用；不再检查 batch、不中断、不丢弃剩余动作。
        """
        if not collected:
            return False

        sent = 0
        for i, action in enumerate(collected):
            # 真实发送
            msg_id = await self._do_send(action)

            # 写入 system_note（已发送记录）
            label = action.get("label", "")
            if label:
                await self.history.add_system_note(
                    f"{get_time()} msg_id={msg_id} → {label}"
                )

            sent += 1

            # 单条延迟
            delay = action.get("delay", 0.0)
            if delay > 0 and i < len(collected) - 1:
                await asyncio.sleep(delay)

        return False

    async def _consume_send_receipts(
        self,
        conversation_id: str,
    ) -> list[dict[str, Any]]:
        """把待投递发送回执转换成本轮可追加的上下文记录。"""
        receipts = self._send_manager.pop_pending_receipts(conversation_id)
        if not receipts:
            return []

        records: list[dict[str, Any]] = []
        interrupted_items = await self.batch.drain_conversation(conversation_id)
        if interrupted_items:
            records.append(self._build_user_record(interrupted_items))
            new_messages = [
                {
                    "conversation_id": item.conversation_id,
                    "seq": item.inbound_seq,
                    "time": get_time(),
                    "nickname": item.nickname,
                    "user_id": item.user_id,
                    "text": item.text,
                    "msg_id": item.message_id,
                    "qq_visible": True,
                }
                for item in interrupted_items
            ]
            for receipt in receipts:
                known = {m.get("msg_id") for m in receipt.get("new_messages", [])}
                receipt.setdefault("new_messages", []).extend(
                    m for m in new_messages if m["msg_id"] not in known
                )

        for receipt in receipts:
            records.append(
                {
                    "role": "user",
                    "content": self._format_send_receipt(receipt),
                    "conversation_id": conversation_id,
                }
            )

        self._send_manager.mark_receipts_delivered(conversation_id)
        return records

    async def _record_clean_send_receipt(self, receipt: dict[str, Any]) -> None:
        """清洁发送完成只静默入历史，不触发模型。"""
        conversation_id = str(receipt.get("conversation_id") or "")
        sent = receipt.get("sent") or []
        if not sent:
            return
        msg_ids = ", ".join(
            str(item.get("msg_id")) for item in sent if item.get("msg_id") is not None
        )
        record = _make_runtime_context_record(
            f"{get_time()} 发送完成（全部消息已发出）"
            f" send_id={receipt.get('send_id')} msg_ids=[{msg_ids}]",
            kind="send_done_snapshot",
            tag="send_status",
            conversation_id=conversation_id or None,
        )
        if record is not None:
            await self.history.add_records([record], conversation_id=conversation_id or None)

    def _schedule_send_receipt_turn(self, conversation_id: str) -> None:
        task = self._send_receipt_tasks.get(conversation_id)
        if task is not None and not task.done():
            return
        self._send_receipt_tasks[conversation_id] = asyncio.create_task(
            self._run_send_receipt_turn(conversation_id)
        )

    async def _run_send_receipt_turn(self, conversation_id: str) -> None:
        """Case B：模型已收尾后收到打断/失败回执，触发新轮处理。"""
        async with self.reply_lock:
            receipt_records = await self._consume_send_receipts(conversation_id)
            if not receipt_records:
                return
            await self.history.add_records(receipt_records, conversation_id=conversation_id)
            receipt_block = "\n".join(
                r.get("content", "")
                for r in receipt_records
                if "<send_receipt>" in str(r.get("content") or "")
            )
            task_context = (
                "<send_receipt_task priority=\"high\">\n"
                "处理下面的运行时发送回执，按回执摘要判断：\n"
                f"{receipt_block}\n"
                "未发出的消息不要原样自动补发，先结合新消息判断；仍需回应时发送调整后的消息。\n"
                "</send_receipt_task>"
            )
            target = self._target_from_conversation_id(conversation_id)
            await self.run_one_turn(
                task_context,
                lock_already_held=True,
                default_target=target,
                conversation_id=conversation_id,
                task_contract="处理发送回执和新消息",
                task_phase="send_receipt",
            )

    def _format_send_receipt(self, receipt: dict[str, Any]) -> str:
        sent = self._receipt_dicts(receipt.get("sent"))
        unsent = self._receipt_dicts(receipt.get("unsent"))
        new_messages = self._receipt_dicts(receipt.get("new_messages"))
        recalled = self._receipt_dicts(receipt.get("recalled_messages"))
        errors = self._receipt_items(receipt.get("errors"))

        lines = [
            "<send_receipt>",
            f"发送回执：{self._receipt_value(receipt.get('send_id'))}",
            f"会话：{self._receipt_value(receipt.get('conversation_id'))}",
            (
                "状态："
                f"{self._send_receipt_status(receipt, sent, unsent, recalled, errors)}"
            ),
        ]
        lines.extend(self._format_receipt_delivery_items("已发送", sent, 3))
        lines.extend(
            self._format_receipt_delivery_items(
                "未发送",
                unsent,
                5,
                receipt_send_id=receipt.get("send_id"),
                include_send_id=True,
            )
        )
        lines.extend(self._format_receipt_new_messages(new_messages))
        lines.extend(self._format_receipt_keyed_items("撤回消息", recalled))
        lines.extend(self._format_receipt_errors(errors))
        lines.extend(
            [
                (
                    "处理要求：不要重发已发送内容；未发送内容不要机械补发，"
                    "先结合新消息判断；如果新消息只是约定干扰，可以 no_action。"
                ),
                "</send_receipt>",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _receipt_items(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    @staticmethod
    def _receipt_dicts(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _receipt_value(value: Any) -> str:
        if value is None or value == "":
            return "无"
        return str(value).replace("\r", " ").replace("\n", " ").strip()

    @classmethod
    def _receipt_text(cls, value: Any, *, max_chars: int | None = None) -> str:
        text = cls._receipt_value(value)
        if max_chars is not None and len(text) > max_chars:
            suffix = "..."
            if max_chars <= len(suffix):
                return text[:max_chars]
            return f"{text[: max_chars - len(suffix)]}{suffix}"
        return text

    def _send_receipt_status(
        self,
        receipt: dict[str, Any],
        sent: list[dict[str, Any]],
        unsent: list[dict[str, Any]],
        recalled: list[dict[str, Any]],
        errors: list[Any],
    ) -> str:
        interrupted = bool(receipt.get("interrupted"))
        parts: list[str] = []
        if interrupted:
            if sent and unsent:
                parts.append("部分发送")
            elif unsent:
                parts.append("未发送")
            elif sent:
                parts.append("已发送")
            else:
                parts.append("发送未开始")
            parts.append("发送期间被新消息打断")
        elif errors:
            parts.append("发送异常")
        elif unsent:
            parts.append("有未发送内容")
        elif sent:
            parts.append("已完成")
        else:
            parts.append("无发送动作")
        if recalled:
            parts.append("有撤回消息")
        if errors:
            parts.append("有发送错误")
        interrupted_text = "true" if interrupted else "false"
        return f"{'；'.join(parts)}（interrupted={interrupted_text}）。"

    def _format_receipt_delivery_items(
        self,
        title: str,
        items: list[dict[str, Any]],
        limit: int,
        *,
        receipt_send_id: Any = None,
        include_send_id: bool = False,
    ) -> list[str]:
        lines = [f"{title} {len(items)} 条："]
        if not items:
            lines.append("- 无")
            return lines
        for index, item in enumerate(items[:limit], 1):
            content = self._receipt_text(
                item.get("content") or item.get("label"),
                max_chars=160,
            )
            parts = [content]
            if item.get("order") is not None:
                parts.append(f"order={self._receipt_value(item.get('order'))}")
            if item.get("msg_id") is not None:
                parts.append(f"msg_id={self._receipt_value(item.get('msg_id'))}")
            if include_send_id:
                send_id = item.get("send_id") or receipt_send_id
                parts.append(f"send_id={self._receipt_value(send_id)}")
            if item.get("conversation_id"):
                parts.append(
                    f"conversation_id={self._receipt_value(item.get('conversation_id'))}"
                )
            lines.append(f"{index}. {'；'.join(parts)}")
        remaining = len(items) - limit
        if remaining > 0:
            lines.append(f"... 另有 {remaining} 条未列出。")
        return lines

    def _format_receipt_new_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[str]:
        lines = [f"新消息 {len(messages)} 条："]
        if not messages:
            lines.append("- 无")
            return lines

        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for message in messages:
            conversation_id = self._receipt_value(message.get("conversation_id"))
            user_id = self._receipt_value(message.get("user_id"))
            nickname = self._receipt_value(message.get("nickname"))
            key = (conversation_id, user_id, nickname)
            group = grouped.setdefault(
                key,
                {
                    "count": 0,
                    "sample": "",
                    "latest": message,
                    "priority_reasons": [],
                },
            )
            group["count"] += 1
            if not group["sample"] and message.get("text"):
                group["sample"] = self._receipt_text(message.get("text"), max_chars=120)
            group["latest"] = self._newer_receipt_message(group["latest"], message)
            reasons = message.get("priority_reasons") or []
            if isinstance(reasons, list):
                group["priority_reasons"].extend(str(reason) for reason in reasons)
            reason = message.get("priority_reason")
            if reason:
                group["priority_reasons"].append(str(reason))

        group_limit = 6
        grouped_items = list(grouped.items())
        for (conversation_id, user_id, nickname), group in grouped_items[:group_limit]:
            latest = group["latest"]
            label = nickname if nickname != "无" else user_id
            latest_parts = [
                f"seq={self._receipt_value(latest.get('seq'))}",
                f"time={self._receipt_value(latest.get('time'))}",
                f"msg_id={self._receipt_value(latest.get('msg_id'))}",
            ]
            line_parts = [
                f"{label}（{conversation_id}；user_id={user_id}）{group['count']} 条",
                f"样例：\"{group['sample'] or '无'}\"",
                f"最新 {'/'.join(latest_parts)}",
            ]
            priority_reasons = list(dict.fromkeys(group["priority_reasons"]))
            if priority_reasons:
                line_parts.append(f"priority_reasons={','.join(priority_reasons)}")
            lines.append(f"- {'；'.join(line_parts)}")
        remaining_groups = len(grouped_items) - group_limit
        if remaining_groups > 0:
            lines.append(f"... 另有 {remaining_groups} 组未列出。")
        return lines

    @staticmethod
    def _newer_receipt_message(
        current: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        current_seq = current.get("seq")
        candidate_seq = candidate.get("seq")
        if isinstance(current_seq, int) and isinstance(candidate_seq, int):
            return candidate if candidate_seq >= current_seq else current
        return candidate

    def _format_receipt_keyed_items(
        self,
        title: str,
        items: list[dict[str, Any]],
    ) -> list[str]:
        lines = [f"{title} {len(items)} 条："]
        if not items:
            lines.append("- 无")
            return lines
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {self._format_receipt_mapping(item)}")
        return lines

    def _format_receipt_errors(self, errors: list[Any]) -> list[str]:
        lines = [f"错误 {len(errors)} 条："]
        if not errors:
            lines.append("- 无")
            return lines
        for index, error in enumerate(errors, 1):
            if isinstance(error, dict):
                lines.append(f"{index}. {self._format_receipt_mapping(error)}")
            else:
                lines.append(f"{index}. {self._receipt_text(error)}")
        return lines

    def _format_receipt_mapping(self, item: dict[str, Any]) -> str:
        preferred = [
            "msg_id",
            "conversation_id",
            "time",
            "note",
            "error",
            "order",
            "send_id",
            "user_id",
            "nickname",
            "text",
            "reply_to",
            "priority_reasons",
            "qq_visible",
        ]
        keys = [key for key in preferred if key in item]
        keys.extend(sorted(key for key in item if key not in keys))
        parts = []
        for key in keys:
            value = item.get(key)
            if isinstance(value, list):
                value = ",".join(str(part) for part in value)
            elif isinstance(value, dict):
                value = self._format_receipt_mapping(value)
            parts.append(f"{key}={self._receipt_value(value)}")
        return "；".join(parts)

    def _target_from_conversation_id(self, conversation_id: str) -> Target | None:
        if ":" not in conversation_id:
            return None
        scope, target_id = conversation_id.split(":", 1)
        if scope not in {"private", "group"}:
            return None
        return Target(adapter=self.adapter.name, scope=scope, target_id=target_id)  # type: ignore[arg-type]

    async def _do_send(self, action: dict) -> str | None:
        """把单个 collected action 真实发送出去。

        失败时写一条 system_note 到历史，让 Agent 下次能看到"哪条没发出去"，
        避免 AI 误以为消息已成功传达。
        """
        scope = action.get("action", "")
        target_id = action.get("target", "")
        content = action.get("content", "")
        label = action.get("label", "")
        kind = action.get("kind", "text")
        if kind != "voice" and not content:
            return None

        # collected 字典里 "action" 字段历史上叫法是 "private"|"group"，与 Target.scope 一致
        if scope not in ("private", "group"):
            await self.history.add_system_note(
                f"⚠ 发送被丢弃：未知 scope={scope!r} target_id={target_id}"
            )
            return None

        target = Target(
            adapter=self.adapter.name,
            scope=scope,
            target_id=target_id,
        )

        try:
            if kind == "voice":
                audio_path = action.get("audio_path")
                if not audio_path:
                    await self.history.add_system_note(
                        f"⚠ 发送失败 → {label or target_id}（缺少语音文件路径）"
                    )
                    return None
                send_voice = getattr(self.adapter, "send_voice", None)
                if send_voice is None:
                    await self.history.add_system_note(
                        f"⚠ 发送失败 → {label or target_id}（当前适配器不支持发送语音）"
                    )
                    return None
                msg_id = await send_voice(target, Path(audio_path))
            else:
                msg_id = await self.adapter.send_text(target, content)
        except Exception as e:
            logger.error(f"发送失败 {target}: {e}")
            await self.history.add_system_note(
                f"⚠ 发送失败 → {label or target_id}（{type(e).__name__}: {e}）"
            )
            return None

        self.mark_activity()
        if msg_id is not None:
            try:
                await self._record_successful_outbound(
                    action,
                    conversation_id=f"{scope}:{target_id}",
                    msg_id=str(msg_id),
                )
            except Exception as e:
                raise RuntimeError(
                    "QQ 消息已发送，但 qq_message_sent 事件持久化失败："
                    f"scope={scope} target_id={target_id} msg_id={msg_id} "
                    f"error={type(e).__name__}: {e}"
                ) from e
        return msg_id

    async def _send_scheduled_message(
        self,
        target: dict[str, Any],
        message_text: str,
    ) -> None:
        """执行 mode=send_message 的定时发送，不经过模型。"""
        target_type = target.get("target_type")
        target_id = target.get("target_id")
        if target_type not in {"private", "group"} or target_id is None:
            return
        content = (message_text or "").strip()
        if not content:
            return
        action = {
            "action": target_type,
            "target": str(target_id),
            "content": content,
            "label": content,
            "delay": 0.0,
        }
        await self._execute_collected([action])
