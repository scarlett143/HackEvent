"""OpenAI-compatible provider (HE-042): Chat Completions with a JSON-schema response format.

Targets any server speaking the Chat Completions API (OpenAI, OpenRouter, Groq, vLLM,
Ollama, ...): `POST {base_url}/chat/completions`, `Authorization: Bearer <key>`,
`response_format = {"type": "json_schema", "json_schema": {...}}`, JSON in
`choices[0].message.content`.
- `strict` is off: strict mode needs every property required + additionalProperties false,
  and not every compatible server supports it. Some hosts ignore `response_format`
  entirely, which is fine: `parse_output` enforces the full schema client-side (A8).
- A refusal arrives in `message.refusal` (possibly with finish_reason "stop"), and
  finish_reason "length" / "content_filter" means truncated or filtered output: all are
  InvalidOutput (retry once, then fallback).
- `max_tokens` rather than `max_completion_tokens`: it is the field compatible servers
  accept most widely. ponytail: switch per provider row if a reasoning-only model needs it.
Raw httpx2, no SDK: no new dependency, and the contract test asserts the wire body.
No `tools` / `tool_choice` is ever sent.
"""

from typing import Any

import httpx2
from pydantic import SecretStr

from common.llm.base import TIMEOUT, InvalidOutput, LLMResult, parse_output, post_json

DEFAULT_BASE_URL = "https://api.openai.com/v1"
SCHEMA_NAME = "hackevents_output"


class OpenAICompatProvider:
    provider = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        base_url: str | None = None,
        client: httpx2.AsyncClient | None = None,
        retry_delay: float = 1.0,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/chat/completions"
        self._http = client or httpx2.AsyncClient(timeout=TIMEOUT)
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return f"OpenAICompatProvider(model={self.model!r}, url={self._url!r})"

    async def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResult:
        body = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "schema": schema, "strict": False},
            },
        }
        headers = {"authorization": f"Bearer {self._api_key.get_secret_value()}"}
        data, latency_ms = await post_json(
            self._http,
            self._url,
            headers=headers,
            body=body,
            provider=self.provider,
            retry_delay=self._retry_delay,
        )
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        finish = choices[0].get("finish_reason")
        if message.get("refusal"):
            raise InvalidOutput("openai_compatible model refused")
        if finish != "stop":
            raise InvalidOutput(f"openai_compatible finish_reason={finish}")
        usage = data.get("usage") or {}
        return LLMResult(
            data=parse_output(message.get("content") or "", schema, self.provider),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or self.model),
            provider=self.provider,
            latency_ms=latency_ms,
        )
