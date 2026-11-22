"""OpenAI-compatible Chat Completions adapter (§6.1).

Covers:
  - LM Studio (including loaded arete-27b-splash, deepseek reasoning, etc.)
  - llama.cpp llama-server
  - Ollama (/v1)
  - vLLM
  - OpenRouter / Groq / OpenAI-compatible gateways

Features:
  - Streaming SSE with real-time text and reasoning deltas
  - Tool call reassembly from streaming chunk deltas
  - Jittered exponential backoff for 429/5xx and Retry-After
  - Per-model parameter rejection cache (auto-recovers from HTTP 400s)
  - Supports custom httpx.AsyncClient (for synthetic test fixtures)
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
_STANDARD_PARAMS = (
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "stop",
    "max_tokens",
    "response_format",
)


class OpenAICompatProvider(BaseProvider):
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
        headers = dict(self.spec.extra_headers)
        if self.spec.api_key:
            headers["Authorization"] = f"Bearer {self.spec.api_key}"
        return headers

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            base_url=self.spec.base_url.rstrip("/"),
            headers=self._headers(),
            timeout=httpx.Timeout(self.base_timeout, connect=15.0),
        )

    def _build_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.spec.model_id,
            "messages": messages,
        }
        if stream:
            body["stream"] = True
            if "stream_options" not in self.spec.rejected_params:
                body["stream_options"] = {"include_usage": True}

        if tools and self.spec.tool_mode == "native":
            body["tools"] = [
                {"type": "function", "function": t} if (isinstance(t, dict) and "function" not in t) else t
                for t in tools
            ]

        for k, v in kwargs.items():
            if v is not None and k not in self.spec.rejected_params:
                body[k] = v

        for p in self.spec.rejected_params:
            body.pop(p, None)

        return body

    def _check_rejection(self, status: int, text: str, body: dict[str, Any]) -> bool:
        if status != 400:
            return False
        changed = False
        for param in ("stream_options", "reasoning_effort", "tools", "tool_choice") + _STANDARD_PARAMS:
            if param in body and param in text:
                self.spec.rejected_params.add(param)
                body.pop(param, None)
                changed = True
        if not changed and "tools" in body and ("function" in text or "tool" in text.lower()):
            self.spec.rejected_params.add("tools")
            body.pop("tools", None)
            changed = True
        return changed

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

                if self._check_rejection(res.status_code, res.text, body) and attempts <= self.max_retries:
                    continue

                res.raise_for_status()
                return res
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
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
        body = self._build_body(
            messages,
            tools,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            **kwargs,
        )

        should_close = self._client is None
        client = self._get_client()
        try:
            res = await self._post_with_retry(client, "chat/completions", body)
            data = res.json()
            choice = data["choices"][0]
            msg = choice.get("message", {})

            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""

            tool_calls: list[ToolCall] = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id", ""),
                        name=fn.get("name", ""),
                        arguments=fn.get("arguments", "{}"),
                    )
                )

            usage = Usage.from_dict(data.get("usage"))
            finish_reason = choice.get("finish_reason") or "stop"

            return CompletionResult(
                content=content,
                reasoning=reasoning if self.spec.reasoning_handling == "keep" else "",
                tool_calls=tool_calls,
                usage=usage,
                finish_reason=finish_reason,
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
        body = self._build_body(
            messages,
            tools,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            **kwargs,
        )

        should_close = self._client is None
        client = self._get_client()
        try:
            req = client.build_request("POST", "chat/completions", json=body, headers=self._headers())
            res = await client.send(req, stream=True)

            if res.status_code == 400:
                txt = (await res.aread()).decode("utf-8", "replace")
                if self._check_rejection(400, txt, body):
                    # Retry once with cleaned body
                    req2 = client.build_request("POST", "chat/completions", json=body, headers=self._headers())
                    res = await client.send(req2, stream=True)

            res.raise_for_status()

            acc_tools: dict[int, dict[str, str]] = {}
            usage = Usage()
            finish_reason = "stop"

            async for line in res.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    usage = Usage.from_dict(chunk["usage"])
                    yield ("usage", usage)

                for ch in chunk.get("choices", []):
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]

                    delta = ch.get("delta") or {}
                    text_chunk = delta.get("content")
                    if text_chunk:
                        yield ("text", text_chunk)

                    reasoning_chunk = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning_chunk and self.spec.reasoning_handling == "keep":
                        yield ("reasoning", reasoning_chunk)

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        entry = acc_tools.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            entry["name"] += fn["name"]
                        if fn.get("arguments"):
                            entry["arguments"] += fn["arguments"]

            if acc_tools:
                calls = [
                    ToolCall(
                        id=v["id"] or f"call_{i}",
                        name=v["name"],
                        arguments=v["arguments"] or "{}",
                    )
                    for i, v in sorted(acc_tools.items())
                ]
                yield ("tool_calls", calls)

            yield ("usage", usage)
            yield ("done", finish_reason)

        except Exception as exc:
            yield ("error", str(exc))
            raise
        finally:
            if should_close:
                await client.aclose()

    async def list_models(self) -> list[str]:
        should_close = self._client is None
        client = self._get_client()
        try:
            res = await client.get("/models")
            res.raise_for_status()
            data = res.json()
            return [m["id"] for m in data.get("data", []) if "id" in m]
        except Exception:
            return []
        finally:
            if should_close:
                await client.aclose()
