"""Azure OpenAI / Foundry adapter (§6.1).

Covers:
  - Azure deployment mapping ({base_url}/openai/deployments/{deployment}/chat/completions)
  - api-version query string setting
  - api-key header authentication
"""

from __future__ import annotations

from typing import Any

import httpx

from keel.providers.base import ModelSpec
from keel.providers.openai_compat import OpenAICompatProvider


class AzureOpenAIProvider(OpenAICompatProvider):
    def __init__(
        self,
        spec: ModelSpec,
        client: httpx.AsyncClient | None = None,
        *,
        deployment_name: str | None = None,
        api_version: str = "2024-08-01-preview",
        max_retries: int = 3,
        base_timeout: float = 120.0,
    ) -> None:
        super().__init__(spec, client, max_retries=max_retries, base_timeout=base_timeout)
        self.deployment_name = deployment_name or spec.model_id
        self.api_version = api_version

    def _headers(self) -> dict[str, str]:
        headers = dict(self.spec.extra_headers)
        if self.spec.api_key:
            headers["api-key"] = self.spec.api_key
        return headers

    def _endpoint_path(self) -> str:
        return f"openai/deployments/{self.deployment_name}/chat/completions?api-version={self.api_version}"

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kwargs: Any) -> Any:
        body = self._build_body(messages, tools, stream=False, **kwargs)
        should_close = self._client is None
        client = self._get_client()
        try:
            res = await self._post_with_retry(client, self._endpoint_path(), body)
            data = res.json()
            choice = data["choices"][0]
            msg = choice.get("message", {})

            from keel.providers.base import CompletionResult, ToolCall, Usage
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", "{}"),
                )
                for tc in msg.get("tool_calls") or []
            ]
            return CompletionResult(
                content=msg.get("content") or "",
                reasoning=msg.get("reasoning_content") or "",
                tool_calls=tool_calls,
                usage=Usage.from_dict(data.get("usage")),
                finish_reason=choice.get("finish_reason") or "stop",
                model=data.get("model", self.deployment_name),
                provider=self.spec.provider,
            )
        finally:
            if should_close:
                await client.aclose()
