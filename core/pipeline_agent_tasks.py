"""Background agent-task helpers for MessagePipeline.

This module is a mechanical split from ``core.message_pipeline``. Keep behavior
equivalent; do not change child-agent task creation, source materialization, or
result handling while moving methods.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from adapters.types import Target
from agents.base import AgentRunResult

from .agent_task_helpers import (
    _agent_record_matches,
    _agent_task_dedupe_key,
    _agent_task_partial_text,
    _agent_task_prompt_hash,
    _agent_task_source_hash,
    _agent_task_timeout_seconds,
    _clamp_agent_task_max_loops,
    _file_head_tail_preview,
    _first_meaningful_line,
    _is_within,
    _record_has_message_id,
    _resolve_agent_workspace_path,
    _safe_agent_task_filename,
    _summarize_agent_task_manifest,
    _workspace_rel,
)

logger = logging.getLogger(__name__)


def _message_pipeline_global(name: str, fallback):
    module = sys.modules.get("core.message_pipeline")
    if module is None:
        return fallback
    return getattr(module, name, fallback)


class PipelineAgentTasksMixin:
    def _same_workspace_path(self, left: Any, right: Any) -> bool:
        left_s = str(left or "").strip()
        right_s = str(right or "").strip()
        if not left_s or not right_s:
            return False
        if self.workspace_dir is None:
            return left_s.replace("\\", "/") == right_s.replace("\\", "/")
        try:
            left_path = _resolve_agent_workspace_path(left_s, self.workspace_dir)
            right_path = _resolve_agent_workspace_path(right_s, self.workspace_dir)
            return left_path.resolve(strict=False) == right_path.resolve(strict=False)
        except Exception:
            return left_s.replace("\\", "/") == right_s.replace("\\", "/")

    async def _start_agent_task(
        self,
        payload: dict[str, Any],
        *,
        conversation_id: str | None,
        default_target: Target | None,
    ) -> dict[str, Any]:
        """运行资料处理子 Agent，并把结果作为当前工具结果返回。"""
        if self.workspace_dir is None:
            return {"ok": False, "error": "workspace 未配置，无法启动后台子 Agent 任务"}

        task_id = f"agent-{int(time.time() * 1000)}-{len(self._agent_task_meta) + 1}"
        source_hash = str(payload.get("_source_hash") or _agent_task_source_hash(payload.get("sources") or []))
        prompt_hash = str(payload.get("_prompt_hash") or _agent_task_prompt_hash(payload))
        output_name = str(payload.get("output_name") or "")
        dedupe_key = str(
            payload.get("_dedupe_key")
            or _agent_task_dedupe_key(
                source_hash=source_hash,
                prompt_hash=prompt_hash,
                output_name=output_name,
            )
        )
        self._agent_task_meta[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "conversation_id": conversation_id,
            "source_hash": source_hash,
            "prompt_hash": prompt_hash,
            "dedupe_key": dedupe_key,
        }
        self.mark_activity()
        return await self._run_agent_task(
            task_id,
            payload,
            conversation_id=conversation_id,
            default_target=default_target,
        )

    async def _run_agent_task(
        self,
        task_id: str,
        payload: dict[str, Any],
        *,
        conversation_id: str | None,
        default_target: Target | None,
    ) -> dict[str, Any]:
        task_dir = self.workspace_dir / "agent_tasks" / task_id if self.workspace_dir else None
        try:
            if task_dir is None:
                raise RuntimeError("workspace 未配置")
            task_dir.mkdir(parents=True, exist_ok=True)
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise RuntimeError("后台子 Agent 任务缺少 prompt")

            output_format = str(payload.get("output_format") or "markdown")
            suffix = {"markdown": ".md", "json": ".json", "text": ".txt"}.get(output_format, ".md")
            output_name = _safe_agent_task_filename(
                str(payload.get("output_name") or f"result{suffix}"),
                default=f"result{suffix}",
                suffix=suffix,
            )
            output_path = task_dir / output_name
            max_loops = _clamp_agent_task_max_loops(
                payload.get("max_loops"),
                int(getattr(getattr(self.chat_agent, "cfg", None), "max_loops", 25) or 25),
            )
            first_token_timeout = float(
                getattr(getattr(self.chat_agent, "cfg", None), "first_token_timeout_seconds", 30.0)
                or 30.0
            )
            timeout_seconds = _message_pipeline_global(
                "_agent_task_timeout_seconds",
                _agent_task_timeout_seconds,
            )(
                payload.get("timeout_seconds"),
                max_loops=max_loops,
                first_token_timeout=first_token_timeout,
            )
            source_manifest = await self._materialize_agent_task_sources(
                payload.get("sources") or [],
                task_dir,
            )
            manifest_path = task_dir / "sources.json"
            manifest_path.write_text(
                json.dumps(source_manifest, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            self._agent_task_meta.setdefault(task_id, {"task_id": task_id})
            self._agent_task_meta[task_id].update(
                {
                    "status": "running",
                    "output_path": _workspace_rel(output_path, self.workspace_dir),
                    "result_file": _workspace_rel(output_path, self.workspace_dir),
                    "manifest_path": _workspace_rel(manifest_path, self.workspace_dir),
                    "manifest_summary": _summarize_agent_task_manifest(source_manifest),
                    "timeout_seconds": timeout_seconds,
                }
            )
            logger.info(
                "后台子 Agent 任务启动 task_id=%s max_loops=%s timeout=%.1fs output=%s",
                task_id,
                max_loops,
                timeout_seconds,
                _workspace_rel(output_path, self.workspace_dir),
            )

            from tools import ToolContext, ToolRegistry, get_default_specs

            allowed = {
                "no_action",
                "tool_search",
                "read_file",
                "list_files",
                "write_file",
                "run_python",
                "get_forward_msg",
                "get_recent_chat_messages",
                "recall_history",
            }
            if self.vision is not None:
                allowed.add("describe_image")
            sub_registry = ToolRegistry(
                [spec for spec in get_default_specs() if spec.name in allowed]
            )
            sub_ctx = ToolContext(
                adapter=self.adapter,
                history=self.history,
                archive=self.archive,
                vision=self.vision,
                workspace_dir=self.workspace_dir,
                conversation_id=conversation_id,
                default_history_fetch_count=self.behavior_cfg.default_history_fetch_count,
                tool_result_default_budget_tokens=self.behavior_cfg.context.tool_result_default_budget_tokens,
                tool_result_default_hard_cap_tokens=self.behavior_cfg.context.tool_result_default_hard_cap_tokens,
                tool_result_budgets=dict(self.behavior_cfg.context.tool_result_budgets),
                tool_result_soft_limit_tokens=self.behavior_cfg.context.tool_result_soft_limit_tokens,
                tool_result_hard_cap_tokens=self.behavior_cfg.context.tool_result_hard_cap_tokens,
                tool_result_soft_overrides=dict(self.behavior_cfg.context.tool_result_soft_overrides),
                activity_cb=self.mark_activity,
                extras={
                    "chat_timeline": self.chat_timeline,
                    "tool_registry": sub_registry,
                    "tool_search_approved_tools": set(),
                },
            )
            output_rel = _workspace_rel(output_path, self.workspace_dir)
            manifest_rel = _workspace_rel(manifest_path, self.workspace_dir)
            sub_executor = sub_registry.get_executor(sub_ctx)

            async def _sub_executor(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
                result = await sub_executor(tool_name, args)
                if (
                    tool_name == "write_file"
                    and result.get("ok", False)
                    and self._same_workspace_path(result.get("path"), output_rel)
                ):
                    result = dict(result)
                    result["stop_after_tool"] = True
                    result["next"] = (
                        "目标结果文件已写出，本后台任务将立即结束并把结果返回给主 Agent。"
                    )
                return result

            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是后台资料处理子 Agent。你只处理资料整理、提取、转换和分析任务，"
                        "不要联系用户，不要发送消息，不要改记忆。\n"
                        "你可以读取 workspace 文件、检索本地历史、读取合并转发、运行 workspace 内的 Python，"
                        "并用 write_file 写出结果。\n"
                        f"必须把完整结果写入 workspace 文件：{output_rel}。\n"
                        "写完后调用 no_action 结束。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"task_id: {task_id}\n"
                        f"输出格式: {output_format}\n"
                        f"资料清单文件: {manifest_rel}\n\n"
                        f"任务说明：\n{prompt}"
                    ),
                },
            ]
            timeout_with_existing_output = False
            try:
                result = await asyncio.wait_for(
                    self.chat_agent.run(
                        messages,
                        tools=sub_registry.get_schemas(),
                        tool_executor=_sub_executor,
                        task_contract=f"后台资料处理任务 {task_id}",
                        max_loops=max_loops,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "后台子 Agent 任务超时 task_id=%s timeout=%.1fs output_exists=%s",
                    task_id,
                    timeout_seconds,
                    output_path.exists(),
                )
                if output_path.exists():
                    timeout_with_existing_output = True
                    result = AgentRunResult(
                        final_content="后台子 Agent 超时，但目标结果文件已经写出。",
                        records=[],
                        loop_count=max_loops,
                        finish_reason="tool_stop",
                    )
                else:
                    raise RuntimeError(
                        f"后台子 Agent 任务超过 {timeout_seconds:.0f}s 仍未产出目标结果文件"
                    ) from None
            status = (
                "partial"
                if result.finish_reason in {"max_loops", "tool_loop_finalized"}
                else "completed"
            )
            if result.finish_reason == "api_error":
                status = "failed"
            if not output_path.exists():
                fallback = (result.final_content or "").strip()
                if status == "partial":
                    fallback = _agent_task_partial_text(
                        task_id=task_id,
                        prompt=prompt,
                        result=result,
                        output_rel=output_rel,
                        max_loops=max_loops,
                    )
                elif status == "failed":
                    fallback = "后台子 Agent 调用失败，未产出可用结果。"
                elif not fallback:
                    fallback = "后台子 Agent 已结束，但没有写出结果内容。"
                output_path.write_text(fallback, encoding="utf-8")

            self._agent_task_meta.setdefault(task_id, {"task_id": task_id})
            self._agent_task_meta[task_id].update(
                {
                    "status": status,
                    "result_file": _workspace_rel(output_path, self.workspace_dir),
                    "finish_reason": result.finish_reason,
                    "loop_count": result.loop_count,
                    "timeout_with_existing_output": timeout_with_existing_output,
                }
            )
            error_text = (
                "后台任务超时，但目标结果文件已写出，已按现有结果返回。"
                if timeout_with_existing_output
                else "达到工具循环最终收尾条件，已产出部分结果。"
                if status == "partial"
                else ""
            )
            content = output_path.read_text(encoding="utf-8", errors="replace")
            rel_path = _workspace_rel(output_path, self.workspace_dir)
            preview = _file_head_tail_preview(output_path)
            summary = _first_meaningful_line(content) or "后台子 Agent 已写出结果"
            return {
                "ok": status != "failed",
                "status": status,
                "brief": f"后台子 Agent 任务已结束：{summary}",
                "task_id": task_id,
                "result_file": rel_path,
                "path": rel_path,
                "content": content,
                "summary": summary,
                "error": error_text,
                "preview": preview,
                "data": {
                    "task_id": task_id,
                    "status": status,
                    "result_file": rel_path,
                    "summary": summary,
                    "manifest_file": manifest_rel,
                    "manifest_summary": _summarize_agent_task_manifest(source_manifest),
                    "max_loops": max_loops,
                    "timeout_seconds": timeout_seconds,
                    "loop_count": result.loop_count,
                    "finish_reason": result.finish_reason,
                    "timeout_with_existing_output": timeout_with_existing_output,
                },
                "next": (
                    "结果已作为本工具返回；如果 content 被截断，可用 read_file 读取 result_file。"
                ),
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("后台子 Agent 任务失败 task_id=%s", task_id)
            error_path = None
            if task_dir is not None:
                try:
                    task_dir.mkdir(parents=True, exist_ok=True)
                    error_path = task_dir / "error.txt"
                    error_path.write_text(str(e), encoding="utf-8")
                except Exception:
                    error_path = None
            self._agent_task_meta.setdefault(task_id, {"task_id": task_id})
            self._agent_task_meta[task_id].update(
                {
                    "status": "failed",
                    "result_file": _workspace_rel(error_path, self.workspace_dir)
                    if error_path
                    else "",
                    "error": str(e),
                }
            )
            rel_path = _workspace_rel(error_path, self.workspace_dir) if error_path else ""
            content = (
                error_path.read_text(encoding="utf-8", errors="replace")
                if error_path and error_path.exists()
                else str(e)
            )
            return {
                "ok": False,
                "status": "failed",
                "brief": f"后台子 Agent 任务失败：{str(e)[:160]}",
                "task_id": task_id,
                "result_file": rel_path,
                "path": rel_path,
                "content": content,
                "summary": "后台子 Agent 任务失败",
                "error": str(e),
                "data": {
                    "task_id": task_id,
                    "status": "failed",
                    "result_file": rel_path,
                    "summary": "后台子 Agent 任务失败",
                },
                "next": "请根据 error 决定是否重试或改用更小的资料范围。",
            }

    async def _materialize_agent_task_sources(
        self,
        sources: Any,
        task_dir: Path,
    ) -> dict[str, Any]:
        """把多种 source 解析为 workspace 文件，避免大材料经过工具结果通道。"""
        source_items = sources if isinstance(sources, list) else []
        manifest: dict[str, Any] = {"count": 0, "sources": []}
        for idx, raw in enumerate(source_items, start=1):
            if not isinstance(raw, dict):
                continue
            source_type = str(raw.get("type") or "")
            item: dict[str, Any] = {"index": idx, "type": source_type}
            try:
                if source_type in {"workspace_path", "tool_result_file", "image_ref"}:
                    value = str(raw.get("value") or "").strip()
                    if source_type == "image_ref" and value.startswith(("http://", "https://")):
                        raise ValueError("image_ref 暂不支持直接传 URL")
                    path = _resolve_agent_workspace_path(value, self.workspace_dir)
                    item["path"] = _workspace_rel(path, self.workspace_dir)
                    item["exists"] = path.exists()
                elif source_type == "workspace_glob":
                    pattern = str(raw.get("value") or "*").strip() or "*"
                    root = self.workspace_dir.resolve(strict=False)
                    matches = [
                        _workspace_rel(p, self.workspace_dir)
                        for p in root.glob(pattern)
                        if p.is_file() and _is_within(p, root)
                    ][:500]
                    item["paths"] = matches
                    item["count"] = len(matches)
                elif source_type == "directory":
                    path = _resolve_agent_workspace_path(str(raw.get("value") or "."), self.workspace_dir)
                    entries = []
                    if path.is_dir():
                        for child in sorted(path.iterdir())[:500]:
                            entries.append(
                                {
                                    "path": _workspace_rel(child, self.workspace_dir),
                                    "type": "dir" if child.is_dir() else "file",
                                    "size": child.stat().st_size if child.is_file() else None,
                                }
                            )
                    item["path"] = _workspace_rel(path, self.workspace_dir)
                    item["entries"] = entries
                elif source_type == "inline_text":
                    text_path = task_dir / f"source_{idx}.txt"
                    text_path.write_text(str(raw.get("value") or ""), encoding="utf-8")
                    item["path"] = _workspace_rel(text_path, self.workspace_dir)
                elif source_type == "inline_json":
                    json_path = task_dir / f"source_{idx}.json"
                    json_path.write_text(
                        json.dumps(raw.get("data"), ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(json_path, self.workspace_dir)
                elif source_type == "forward_id":
                    if self.adapter is None:
                        raise ValueError("adapter 未就绪，无法读取合并转发")
                    from tools.platform_tools import (
                        build_forward_tree,
                        summarize_forward_tree,
                        write_forward_artifact,
                    )

                    forward_id = str(raw.get("value") or "").strip()
                    tree = await build_forward_tree(
                        self.adapter,
                        forward_id,
                        recursive=True,
                        max_depth=3,
                    )
                    forward_path = write_forward_artifact(
                        self.workspace_dir,
                        tree,
                        output="json",
                        prefix=f"agent_source_{idx}",
                    )
                    summary = summarize_forward_tree(tree)
                    item["path"] = _workspace_rel(forward_path, self.workspace_dir)
                    item["message_count"] = summary["message_count"]
                    item["nested_forward_count"] = summary["nested_forward_count"]
                    item["expired_forward_count"] = summary["expired_forward_count"]
                    item["image_count"] = summary["image_count"]
                elif source_type == "conversation_history":
                    records = await self._agent_task_history_records(raw)
                    history_path = task_dir / f"history_{idx}.json"
                    history_path.write_text(
                        json.dumps(records, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(history_path, self.workspace_dir)
                    item["record_count"] = len(records)
                elif source_type == "message_id":
                    records = await self._agent_task_message_records(str(raw.get("value") or ""))
                    msg_path = task_dir / f"message_{idx}.json"
                    msg_path.write_text(
                        json.dumps(records, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(msg_path, self.workspace_dir)
                    item["record_count"] = len(records)
                elif source_type == "tool_call_id":
                    records = await self._agent_task_tool_records(str(raw.get("value") or ""))
                    tool_path = task_dir / f"tool_call_{idx}.json"
                    tool_path.write_text(
                        json.dumps(records, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8",
                    )
                    item["path"] = _workspace_rel(tool_path, self.workspace_dir)
                    item["record_count"] = len(records)
                else:
                    item["error"] = f"不支持的 source type: {source_type}"
            except Exception as e:
                item["error"] = str(e)
            manifest["sources"].append(item)
        manifest["count"] = len(manifest["sources"])
        return manifest

    async def _agent_task_history_records(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        conversation_id = source.get("conversation_id")
        keyword = source.get("keyword")
        time_range = source.get("time_range")
        limit = max(1, min(int(source.get("limit") or 50), 500))
        records: list[dict[str, Any]] = []
        if self.archive is not None:
            records.extend(
                await self.archive.search(
                    conversation_id=conversation_id,
                    keyword=keyword,
                    time_range=time_range,
                    limit=limit,
                )
            )
        if self.history is not None:
            for record in await self.history.records():
                if _agent_record_matches(
                    record,
                    conversation_id=conversation_id,
                    keyword=keyword,
                    time_range=time_range,
                ):
                    records.append(record)
            records = records[-limit:]
        return records

    async def _agent_task_message_records(self, message_id: str) -> list[dict[str, Any]]:
        if not message_id:
            return []
        records = await self._all_history_records()
        return [record for record in records if _record_has_message_id(record, message_id)]

    async def _agent_task_tool_records(self, tool_call_id: str) -> list[dict[str, Any]]:
        if not tool_call_id:
            return []
        records = await self._all_history_records()
        return [
            record
            for record in records
            if str(record.get("tool_call_id") or "") == tool_call_id
        ]

    async def _all_history_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self.archive is not None:
            records.extend(await self.archive.records())
        if self.history is not None:
            records.extend(await self.history.records())
        return records
