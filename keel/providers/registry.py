"""Model registry and configuration loader (§6.3).

Reads config/models.yaml, manages role bindings:
  - main
  - aux_fast (sub-calls, titles)
  - aux_compaction (summaries)
  - aux_vision
  - embed
  - image_gen
with fallback chains and governance constraints (§6.4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from keel.providers.base import BaseProvider, ModelSpec
from keel.providers.openai_compat import OpenAICompatProvider
from keel.providers.anthropic import AnthropicMessagesProvider
from keel.providers.azure import AzureOpenAIProvider

ROLES = ("main", "aux_fast", "aux_compaction", "aux_vision", "embed", "image_gen")


class ModelRegistry:
    def __init__(self) -> None:
        self.models: dict[str, ModelSpec] = {}
        self.roles: dict[str, list[str]] = {r: [] for r in ROLES}
        self.defaults: dict[str, Any] = {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelRegistry":
        reg = cls()
        reg.defaults = data.get("defaults", {})

        for mid, mdata in (data.get("models") or {}).items():
            spec = ModelSpec(
                model_id=mdata.get("model_id", mid),
                provider=mdata.get("provider", "local"),
                provider_type=mdata.get("provider_type", "openai"),
                base_url=mdata.get("base_url", "http://127.0.0.1:1234/v1"),
                api_key=mdata.get("api_key", ""),
                context_length=int(mdata.get("context_length", 4096)),
                max_output=int(mdata.get("max_output", 2048)),
                tool_mode=mdata.get("tool_mode", "native"),
                reasoning_handling=mdata.get("reasoning_handling", "keep"),
                supports_vision=bool(mdata.get("supports_vision", False)),
                is_local=bool(mdata.get("is_local", True)),
                input_cost_per_m=float(mdata.get("input_cost_per_m", 0.0)),
                output_cost_per_m=float(mdata.get("output_cost_per_m", 0.0)),
                cached_cost_per_m=float(mdata.get("cached_cost_per_m", 0.0)),
            )
            reg.models[mid] = spec

        for role, chain in (data.get("roles") or {}).items():
            if role in reg.roles:
                reg.roles[role] = [chain] if isinstance(chain, str) else list(chain)

        return reg

    @classmethod
    def from_yaml(cls, path: Path | str) -> "ModelRegistry":
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        return cls.from_dict(data)

    def register_model(self, mid: str, spec: ModelSpec) -> None:
        self.models[mid] = spec

    def bind_role(self, role: str, model_chain: list[str]) -> None:
        if role not in self.roles:
            raise ValueError(f"unknown role: {role!r}; expected one of {ROLES}")
        self.roles[role] = list(model_chain)

    def get_provider(self, model_id: str) -> BaseProvider:
        if model_id not in self.models:
            raise KeyError(f"model {model_id!r} not registered in models.yaml")
        spec = self.models[model_id]
        pt = spec.provider_type
        if pt == "anthropic":
            return AnthropicMessagesProvider(spec)
        if pt == "azure":
            return AzureOpenAIProvider(spec)
        return OpenAICompatProvider(spec)
