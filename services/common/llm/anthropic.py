"""Anthropic Claude (Haiku) provider: Messages API with structured JSON output.

Verified 2026-11-14 against platform.claude.com docs:
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs
  `POST https://api.anthropic.com/v1/messages`, `x-api-key` header,
  `output_config.format = {"type": "json_schema", "schema": ...}`; the JSON arrives
  in the `text` content block. Unsupported (400): minimum/maximum/multipleOf,
  minLength/maxLength, array limits beyond minItems 0/1; every object needs
  `additionalProperties: false`. `api_schema()` strips/adds those; the full schema is
  still enforced client-side by `parse_output`.
- https://platform.claude.com/docs/en/api/versioning: `anthropic-version: 2026-10-21`
  (temperature/top_p/top_k removed, so none are sent).
- https://platform.claude.com/docs/en/models/haiku-5-5/overview: `claude-haiku-5-5`,
  adaptive thinking on by default, default effort `medium`.
Raw httpx2 rather than the `anthropic` SDK: no new dependency, and the contract test
asserts the exact wire body. No `tools` / `tool_choice` is ever sent.
"""

from typing import Any

import httpx2
from pydantic import SecretStr

from common.llm.base import (
    TIMEOUT,
    InvalidOutput,
    LLMResult,
    parse_output,
    post_json,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-5-5"
API_VERSION = "2026-10-21"
# Thinking tokens count against max_tokens; low effort keeps a JSON-sized budget enough
# for extraction/verification and keeps cost down.
EFFORT = "low"

_UNSUPPORTED = {"minimum", "maximum", "multipleOf", "minLength", "maxLength", "maxItems"}


def api_schema(schema: Any) -> Any:
    """Schema as Anthropic structured outputs accepts it (see module docstring)."""
    if isinstance(schema, list):
        return [api_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: api_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if out.get("minItems", 0) > 1:
        del out["minItems"]
    types = schema.get("type")
    if types == "object" or (isinstance(types, list) and "object" in types):
        out["additionalProperties"] = False
    if "properties" in schema:  # keys here are property names, not keywords
        out["properties"] = {k: api_schema(v) for k, v in schema["properties"].items()}
    return out


class AnthropicProvider:
    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        client: httpx2.AsyncClient | None = None,
        retry_delay: float = 1.0,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/v1/messages"
        self._http = client or httpx2.AsyncClient(timeout=TIMEOUT)
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return f"AnthropicProvider(model={self.model!r})"

    async def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResult:
        body = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": api_schema(schema)},
            },
        }
        headers = {
            "x-api-key": self._api_key.get_secret_value(),
            "anthropic-version": API_VERSION,
        }
        data, latency_ms = await post_json(
            self._http,
            self._url,
            headers=headers,
            body=body,
            provider=self.provider,
            retry_delay=self._retry_delay,
        )
        stop = data.get("stop_reason")
        if stop != "end_turn":
            raise InvalidOutput(f"anthropic stop_reason={stop}")
        text = "".join(
            b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return LLMResult(
            data=parse_output(text, schema, self.provider),
            # cache reads/writes are input too; counting them keeps cap maths conservative
            input_tokens=int(usage.get("input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens", 0)),
            model=str(data.get("model") or self.model),
            provider=self.provider,
            latency_ms=latency_ms,
        )
