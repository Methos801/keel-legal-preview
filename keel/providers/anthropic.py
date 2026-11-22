"""Anthropic Messages API adapter (§6.1).

Covers:
  - Claude 3 / 3.5 / 3.7 models
  - System prompt separation + prompt cache breakpoints (`cache_control`)
  - Extended thinking normalization to ("reasoning", ...)
  - Tool calling conversion (`tool_use` <-> `tool_result`)
  - Streaming SSE parser
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncIterator

import httpx

from keel.providers.base import (
    BaseProvider,
    CompletionResult,
    ModelSpec,
    StreamEvent,
    ToolCall,
    Usage,
)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class AnthropicMessagesProvider(BaseProvider):
    def __init__(
        self,
        spec: ModelSpec,
        client: httpx.AsyncClient | None = None,
        *,
        max_retries: int = 3,
        base_timeout: float = 120.0,
    ) -> None:
        super().__init__(spec)
        self._client = client
        self.max_retries = max_retries
        self.base_timeout = base_timeout

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.spec.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            **self.spec.extra_headers,
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            base_url=self.spec.base_url.rstrip("/"),
            headers=self._headers(),
            timeout=httpx.Timeout(self.base_timeout, connect=15.0),
        )

    def _convert_messages(
        self, messages: list[dict[str, Any]], enable_cache_control: bool = True
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Separates system prompt and formats conversation for Anthropic Messages."""
        system_blocks: list[dict[str, Any]] = []
        conv: list[dict[str, Any]] = []

        for m in messages:
            role = m["role"]
            content = m.get("content")

            if role == "system":
                if isinstance(content, str):
                    system_blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    system_blocks.extend(content)
                continue

            if role == "tool":
                tool_res = {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                }
                if conv and conv[-1]["role"] == "user" and isinstance(conv[-1]["content"], list):
                    conv[-1]["content"].append(tool_res)
                else:
                    conv.append({"role": "user", "content": [tool_res]})
                continue

            if role == "assistant" and m.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    parsed_args = json.loads(args) if isinstance(args, str) else args
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": parsed_args,
                    })
                conv.append({"role": "assistant", "content": blocks})
                continue

            if isinstance(content, str):
                conv.append({"role": role, "content": content})
            elif isinstance(content, list):
                conv.append({"role": role, "content": content})

        # Add cache_control breakpoint to the system block if enabled (§6.1, §8)
        if enable_cache_control and system_blocks:
            system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        return system_blocks, conv

    def _convert_tools(
        self, tools: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if not tools or self.spec.tool_mode != "native":
            return None
        anth_tools: list[dict[str, Any]] = []
        for t in tools:
            fn = t.get("function", {})
            anth_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object"}),
            })
        return anth_tools

    async def _post_with_retry(
        self, client: httpx.AsyncClient, path: str, body: dict[str, Any]
    ) -> httpx.Response:
        attempts = 0
        while True:
            attempts += 1
            try:
                res = await client.post(path, json=body, headers=self._headers())
                if res.status_code in _RETRYABLE_STATUS and attempts <= self.max_retries:
                    retry_after = res.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else (0.5 * (2 ** (attempts - 1)))
                    delay += random.uniform(0.05, 0.2)
                    await asyncio.sleep(delay)
                    continue

                res.raise_for_status()
                return res
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout):
                if attempts > self.max_retries:
                    raise
                delay = 0.5 * (2 ** (attempts - 1)) + random.uniform(0.05, 0.2)
                await asyncio.sleep(delay)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> CompletionResult:
        sys_blocks, conv = self._convert_messages(messages)
        atools = self._convert_tools(tools)

        body: dict[str, Any] = {
            "model": self.spec.model_id,
            "messages": conv,
            "max_tokens": max_tokens or self.spec.max_output,
        }
        if sys_blocks:
            body["system"] = sys_blocks
        if atools:
            body["tools"] = atools
        if temperature is not None:
            body["temperature"] = temperature
        if stop:
            body["stop_sequences"] = stop

        for k, v in kwargs.items():
            if v is not None:
                body[k] = v

        should_close = self._client is None
        client = self._get_client()
        try:
            res = await self._post_with_retry(client, "/v1/messages", body)
            data = res.json()

            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[ToolCall] = []

            for block in data.get("content", []):
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                elif btype == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            name=block.get("name", ""),
                            arguments=json.dumps(block.get("input", {})),
                        )
                    )

            u = data.get("usage", {})
            usage = Usage(
                prompt_tokens=u.get("input_tokens", 0),
                completion_tokens=u.get("output_tokens", 0),
                cached_tokens=u.get("cache_read_input_tokens", 0),
                total_tokens=u.get("input_tokens", 0) + u.get("output_tokens", 0),
            )

            return CompletionResult(
                content="".join(text_parts),
                reasoning="".join(reasoning_parts) if self.spec.reasoning_handling == "keep" else "",
                tool_calls=tool_calls,
                usage=usage,
                finish_reason=data.get("stop_reason") or "stop",
                model=data.get("model", self.spec.model_id),
                provider=self.spec.provider,
            )
        finally:
            if should_close:
                await client.aclose()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        sys_blocks, conv = self._convert_messages(messages)
        atools = self._convert_tools(tools)

        body: dict[str, Any] = {
            "model": self.spec.model_id,
            "messages": conv,
            "max_tokens": max_tokens or self.spec.max_output,
            "stream": True,
        }
        if sys_blocks:
            body["system"] = sys_blocks
        if atools:
            body["tools"] = atools
        if temperature is not None:
            body["temperature"] = temperature
        if stop:
            body["stop_sequences"] = stop

        should_close = self._client is None
        client = self._get_client()
        try:
            req = client.build_request("POST", "/v1/messages", json=body, headers=self._headers())
            res = await client.send(req, stream=True)
            res.raise_for_status()

            blocks: dict[int, dict[str, Any]] = {}
            usage = Usage()
            finish_reason = "stop"

            async for line in res.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                try:
                    event_data = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue

                etype = event_data.get("type")
                if etype == "message_start":
                    msg = event_data.get("message", {})
                    u = msg.get("usage", {})
                    usage.prompt_tokens = u.get("input_tokens", 0)
                    usage.cached_tokens = u.get("cache_read_input_tokens", 0)
                    yield ("usage", usage)

                elif etype == "content_block_start":
                    idx = event_data.get("index", 0)
                    cb = event_data.get("content_block", {})
                    blocks[idx] = {
                        "type": cb.get("type"),
                        "id": cb.get("id", ""),
                        "name": cb.get("name", ""),
                        "json": "",
                    }

                elif etype == "content_block_delta":
                    idx = event_data.get("index", 0)
                    delta = event_data.get("delta", {})
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        yield ("text", delta.get("text", ""))
                    elif dtype == "thinking_delta":
                        if self.spec.reasoning_handling == "keep":
                            yield ("reasoning", delta.get("thinking", ""))
                    elif dtype == "input_json_delta":
                        if idx in blocks:
                            blocks[idx]["json"] += delta.get("partial_json", "")

                elif etype == "message_delta":
                    delta = event_data.get("delta", {})
                    if delta.get("stop_reason"):
                        finish_reason = delta["stop_reason"]
                    u = event_data.get("usage", {})
                    if u.get("output_tokens"):
                        usage.completion_tokens = u["output_tokens"]
                        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
                        yield ("usage", usage)

            tool_calls = [
                ToolCall(
                    id=b["id"],
                    name=b["name"],
                    arguments=b["json"] or "{}",
                )
                for _, b in sorted(blocks.items())
                if b.get("type") == "tool_use"
            ]
            if tool_calls:
                yield ("tool_calls", tool_calls)

            yield ("usage", usage)
            yield ("done", finish_reason)

        except Exception as exc:
            yield ("error", str(exc))
            raise
        finally:
            if should_close:
                await client.aclose()

    async def list_models(self) -> list[str]:
        return ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"]
