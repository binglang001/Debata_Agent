"""OpenAI 兼容协议 —— DeepSeek/GLM/Moonshot/Qwen/零一万物/OpenRouter 等共用。

策略：
    - 用 openai-python SDK 与服务端通信
    - 默认开启流式，带首 token 超时
    - reasoning 配置通过 extra_body={"thinking": {"type": "enabled" | "disabled"}}
      （DeepSeek/GLM 等约定）
    - 子类化由 protocol layer 处理，不再硬编码 base_url

不同厂商的差异化：
    - DeepSeek reasoner: 思考内容在 delta.reasoning_content
    - GLM: 类似 DeepSeek
    - OpenAI o1/o3: 思考由 max_completion_tokens 控制，不直接暴露 reasoning_content
    本协议尝试兼容所有这些情况：reasoning_content 不可用时退回空字符串。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from openai import APIError, AsyncOpenAI, AuthenticationError, RateLimitError

from providers.base import (
    CompletionResult,
    IProvider,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ReasoningConfig,
    ToolCall,
    Usage,
    normalize_messages,
)

logger = logging.getLogger(__name__)

_RAW_PROMPT_DUMP_ENV = "DIANA_KV_RAW_PROMPT_DUMP"
_RAW_PROMPT_DUMP_DIR_ENV = "DIANA_KV_RAW_PROMPT_DUMP_DIR"
_RAW_PROMPT_DUMP_DEFAULT_DIR = Path("data/logs/kv_raw_prompts")
_REPLAY_REASONING_ENV = "DIANA_OPENAI_COMPAT_REPLAY_REASONING_CONTENT"


class OpenAICompatProvider(IProvider):
    """通用 OpenAI 兼容客户端。"""

    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float = 120.0,
        extra_headers: dict[str, str] | None = None,
        reasoning_style: str = "thinking_extra_body",
        known_model_ids: set[str] | None = None,
        reasoning_model_ids: set[str] | None = None,
    ) -> None:
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.reasoning_style = reasoning_style
        self.known_model_ids = set(known_model_ids or ())
        self.reasoning_model_ids = set(reasoning_model_ids or ())
        """如何向 API 传达 reasoning 配置：
            'thinking_extra_body'  —— DeepSeek/GLM 风格：extra_body={"thinking": {"type": ...}}
            'openai_native'        —— 不传（OpenAI o1/o3 隐式启用）
            'qwen_enable_thinking' —— 通义千问：extra_body={"enable_thinking": True}
        """

        # openai SDK 拒绝空 api_key；未配置时传占位符，调用时由 API 返回 401 触发明确错误
        sdk_api_key = self.api_key or "NO_API_KEY_CONFIGURED"
        self._client = AsyncOpenAI(
            api_key=sdk_api_key,
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            default_headers=self.extra_headers or None,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        top_p: float = 1.0,
        max_tokens: int = 16384,
        reasoning: ReasoningConfig | None = None,
        stream: bool = True,
        timeout: float = 120.0,
        first_token_timeout: float | None = 30.0,
        extra: dict[str, Any] | None = None,
    ) -> CompletionResult:
        reasoning_enabled = bool(reasoning and reasoning.enabled)
        replay_reasoning_content = self._should_replay_reasoning_content(
            reasoning, model=model
        )
        messages_norm = normalize_messages(
            messages,
            preserve_reasoning_content=replay_reasoning_content,
        )
        if reasoning_enabled and replay_reasoning_content:
            self._ensure_assistant_reasoning_content(messages_norm)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages_norm,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": stream,
            "timeout": timeout,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if stream:
            kwargs["stream_options"] = {"include_usage": True}

        # === 思考配置翻译 ===
        extra_body = self._build_reasoning_extra_body(reasoning, model=model)
        if extra:
            extra_body.update(extra)
        if extra_body:
            kwargs["extra_body"] = extra_body

        _dump_raw_prompt_request(provider=self.name, kwargs=kwargs)

        try:
            if stream:
                return await self._chat_stream(kwargs, first_token_timeout)
            return await self._chat_nonstream(kwargs)
        except AuthenticationError as e:
            raise ProviderAuthError(f"{self.name}: 认证失败: {e}") from e
        except RateLimitError as e:
            raise ProviderRateLimitError(f"{self.name}: 限流: {e}") from e
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            raise ProviderTimeoutError(f"{self.name}: 超时: {e}") from e
        except APIError as e:
            raise ProviderError(f"{self.name}: API 错误: {e}") from e

    # ============================================================
    # 内部：流式 / 非流式
    # ============================================================

    async def _chat_nonstream(self, kwargs: dict[str, Any]) -> CompletionResult:
        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        content = msg.content or ""
        reasoning_content = getattr(msg, "reasoning_content", "") or ""
        tool_calls = self._extract_tool_calls_object(getattr(msg, "tool_calls", None))

        usage = self._extract_usage(getattr(response, "usage", None))

        return CompletionResult(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            finish_reason=getattr(choice, "finish_reason", "") or "",
            usage=usage,
            model=getattr(response, "model", "") or kwargs.get("model", ""),
            raw=response,
        )

    async def _chat_stream(
        self, kwargs: dict[str, Any], first_token_timeout: float | None
    ) -> CompletionResult:
        stream = await self._client.chat.completions.create(**kwargs)

        chunks: list[Any] = []
        iterator = stream.__aiter__()

        try:
            if first_token_timeout is not None:
                first_chunk = await asyncio.wait_for(
                    anext(iterator), timeout=first_token_timeout
                )
            else:
                first_chunk = await anext(iterator)
        except asyncio.TimeoutError:
            raise ProviderTimeoutError(
                f"{self.name}: 首 token 超时 ({first_token_timeout}s)"
            ) from None
        except StopAsyncIteration:
            first_chunk = None

        if first_chunk is not None:
            chunks.append(first_chunk)
            async for chunk in iterator:
                chunks.append(chunk)

        if not chunks:
            raise ProviderError(f"{self.name}: 流式输出无数据")

        return self._assemble_stream_chunks(chunks, kwargs.get("model", ""))

    def _assemble_stream_chunks(self, chunks: list[Any], model: str) -> CompletionResult:
        content = ""
        reasoning_content = ""
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage_obj = None

        for chunk in chunks:
            if getattr(chunk, "usage", None):
                usage_obj = chunk.usage
            if not chunk.choices:
                continue
            choice0 = chunk.choices[0]
            delta = getattr(choice0, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "content", None):
                content += delta.content
            r = getattr(delta, "reasoning_content", None)
            if r:
                reasoning_content += r
            if getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    idx = getattr(tc, "index", 0)
                    record = tool_calls_by_index.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if getattr(tc, "id", None):
                        record["id"] = tc.id
                    func = getattr(tc, "function", None)
                    if func is not None:
                        if getattr(func, "name", None):
                            record["name"] += func.name
                        if getattr(func, "arguments", None):
                            record["arguments"] += func.arguments
            if getattr(choice0, "finish_reason", None):
                finish_reason = choice0.finish_reason

        tool_calls = [
            ToolCall(id=v["id"], name=v["name"], arguments=v["arguments"])
            for _, v in sorted(tool_calls_by_index.items())
            if v["name"]  # 跳过空的
        ]

        return CompletionResult(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
            usage=self._extract_usage(usage_obj),
            model=model,
            raw=None,
        )

    # ============================================================
    # 辅助
    # ============================================================

    def _build_reasoning_extra_body(
        self, reasoning: ReasoningConfig | None, *, model: str = ""
    ) -> dict[str, Any]:
        if not self._model_allows_reasoning_controls(model, reasoning):
            return {}

        if self.reasoning_style == "thinking_extra_body":
            enabled = bool(reasoning and reasoning.enabled)
            return {
                "thinking": {"type": "enabled" if enabled else "disabled"}
            }
        if self.reasoning_style == "qwen_enable_thinking":
            return {"enable_thinking": bool(reasoning and reasoning.enabled)}
        # 默认（如 openai_native）不传特殊参数
        return {}

    def _should_replay_reasoning_content(
        self, reasoning: ReasoningConfig | None, *, model: str = ""
    ) -> bool:
        if not _replay_reasoning_content_enabled():
            return False
        return bool(reasoning and reasoning.enabled)

    def _model_allows_reasoning_controls(
        self,
        model: str,
        reasoning: ReasoningConfig | None,
    ) -> bool:
        if not self.known_model_ids:
            return True
        if model in self.reasoning_model_ids:
            return True
        if model in self.known_model_ids:
            return False
        # 未知模型不默认发送关闭参数，避免普通 OpenAI-compatible 服务报未知字段；
        # 但用户显式开启 reasoning 时仍允许透传给新增的思考模型。
        return bool(reasoning and reasoning.enabled)

    @staticmethod
    def _ensure_assistant_reasoning_content(
        messages: list[dict[str, Any]]
    ) -> None:
        for msg in messages:
            if msg.get("role") == "assistant" and "reasoning_content" not in msg:
                msg["reasoning_content"] = ""

    @staticmethod
    def _extract_tool_calls_object(tool_calls_raw: Any) -> list[ToolCall]:
        if not tool_calls_raw:
            return []
        result: list[ToolCall] = []
        for tc in tool_calls_raw:
            func = getattr(tc, "function", None)
            if func is None:
                continue
            result.append(
                ToolCall(
                    id=getattr(tc, "id", "") or "",
                    name=getattr(func, "name", "") or "",
                    arguments=getattr(func, "arguments", "") or "",
                )
            )
        return result

    @staticmethod
    def _extract_usage(usage_obj: Any) -> Usage:
        if usage_obj is None:
            return Usage()
        # 不同 SDK 字段命名略有差异
        prompt = (
            getattr(usage_obj, "prompt_tokens", None)
            or getattr(usage_obj, "input_tokens", None)
            or 0
        )
        completion = (
            getattr(usage_obj, "completion_tokens", None)
            or getattr(usage_obj, "output_tokens", None)
            or 0
        )
        total = getattr(usage_obj, "total_tokens", None) or (prompt + completion)
        reasoning = 0
        details = getattr(usage_obj, "completion_tokens_details", None)
        if details is not None:
            reasoning = getattr(details, "reasoning_tokens", 0) or 0
        cached = 0
        prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
        if prompt_details is not None:
            cached = getattr(prompt_details, "cached_tokens", 0) or 0
        cached = cached or getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0
        cache_creation = getattr(usage_obj, "prompt_cache_miss_tokens", 0) or 0
        return Usage(
            prompt_tokens=int(prompt),
            completion_tokens=int(completion),
            reasoning_tokens=int(reasoning),
            cached_tokens=int(cached),
            cache_creation_tokens=int(cache_creation),
            total_tokens=int(total),
        )


def _dump_raw_prompt_request(*, provider: str, kwargs: dict[str, Any]) -> Path | None:
    """临时保存 OpenAI-compatible 最终请求体，用于 KV 缓存排障。"""
    if not _raw_prompt_dump_enabled():
        return None

    try:
        payload = _raw_prompt_dump_payload(provider=provider, kwargs=kwargs)
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        root = _raw_prompt_dump_dir()
        root.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(payload["ts"]))
        model = _safe_filename(str(payload.get("model") or "model"))
        req_hash = _short_hash(
            json.dumps(
                payload.get("request") or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        )
        path = root / f"{ts}-{_safe_filename(provider)}-{model}-{req_hash}.json"
        path.write_text(text, encoding="utf-8")
        (root / "latest.json").write_text(text, encoding="utf-8")
        return path
    except Exception:
        logger.debug("写入原始提示词 KV 诊断失败", exc_info=True)
        return None


def _raw_prompt_dump_payload(*, provider: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    request_keys = {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "stream",
        "stream_options",
        "extra_body",
    }
    request = {key: value for key, value in kwargs.items() if key in request_keys}
    request_text = json.dumps(request, ensure_ascii=False, sort_keys=True, default=str)
    messages = request.get("messages") or []
    tools = request.get("tools") or []
    return {
        "ts": time.time(),
        "provider": provider,
        "model": str(kwargs.get("model") or ""),
        "request_hash": _short_hash(request_text),
        "request_chars": len(request_text),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "request": request,
    }


def _raw_prompt_dump_enabled() -> bool:
    value = os.getenv(_RAW_PROMPT_DUMP_ENV)
    return bool(value and value.strip().lower() not in {"0", "false", "no", "off"})


def _replay_reasoning_content_enabled() -> bool:
    value = os.getenv(_REPLAY_REASONING_ENV)
    return bool(value and value.strip().lower() not in {"0", "false", "no", "off"})


def _raw_prompt_dump_dir() -> Path:
    value = os.getenv(_RAW_PROMPT_DUMP_DIR_ENV)
    return Path(value).expanduser() if value else _RAW_PROMPT_DUMP_DEFAULT_DIR


def _safe_filename(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    return safe[:80] or "unknown"


def _short_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
