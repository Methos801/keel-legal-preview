"""Base provider interfaces, normalized event streams, and model specs (§6.1).

Normalizes all provider streams (OpenAI, Anthropic, Azure, local) to one typed
event stream:
  * ("text", str)
  * ("reasoning", str)
  * ("tool_calls", list[ToolCall])
  * ("usage", Usage)
  * ("done", str finish_reason)
  * ("error", str error_message)
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Literal


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Usage":
        if not d:
            return cls()
        pt = int(d.get("prompt_tokens") or d.get("input_tokens") or 0)
        ct = int(d.get("completion_tokens") or d.get("output_tokens") or 0)
        cached = int(
            (d.get("prompt_tokens_details") or {}).get("cached_tokens")
            or (d.get("cache_read_input_tokens"))
            or d.get("cached_tokens")
            or 0
        )
        tot = int(d.get("total_tokens") or (pt + ct))
        return cls(prompt_tokens=pt, completion_tokens=ct, cached_tokens=cached, total_tokens=tot)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string

    def to_dict(self) -> dict[str, Any]:
        args_str = json.dumps(self.arguments) if isinstance(self.arguments, dict) else str(self.arguments)
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": args_str},
        }


@dataclass
class CompletionResult:
    content: str
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    model: str = ""
    provider: str = ""


ProviderType = Literal["openai", "openai_responses", "anthropic", "azure", "local"]


@dataclass
class ModelSpec:
    model_id: str
    provider: str
    provider_type: ProviderType = "local"
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = ""
    context_length: int = 4096
    max_output: int = 2048
    tool_mode: Literal["native", "fenced"] = "native"
    reasoning_handling: Literal["keep", "strip"] = "keep"
    supports_vision: bool = False
    is_local: bool = True
    input_cost_per_m: float = 0.0
    output_cost_per_m: float = 0.0
    cached_cost_per_m: float = 0.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    rejected_params: set[str] = field(default_factory=set)


StreamEvent = tuple[Literal["text", "reasoning", "tool_calls", "usage", "done", "error"], Any]


class BaseProvider(ABC):
    """Abstract base class for all provider adapters (§6.1)."""

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec

    @abstractmethod
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
        """Non-streaming chat completion."""
        ...

    @abstractmethod
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
        """True SSE streaming yielding normalized events."""
        ...

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Query provider endpoint for available model IDs."""
        ...
