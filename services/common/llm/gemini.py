"""Google Gemini provider: REST `models/{model}:generateContent` in JSON mode.

Verified 2026-11-14 against https://aistudio.google.com/api-reference/generate-content
and https://aistudio.google.com/docs/models:
- `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
- key in the `x-goog-api-key` header, never the `?key=` query string (URLs land in logs)
- `generationConfig.responseMimeType = "application/json"` + `responseJsonSchema`
  (standard JSON Schema; the older `responseSchema` is an OpenAPI subset that cannot
  express `"type": ["string", "null"]`, which doc 08 A3 uses)
- usage from `usageMetadata.promptTokenCount` / `candidatesTokenCount` / `thoughtsTokenCount`
No `tools` / `toolConfig` is ever sent.
"""

from typing import Any

import httpx2
from pydantic import SecretStr

from common.llm.base import (
    TIMEOUT,
    InvalidOutput,
    LLMResult,
    ProviderError,
    parse_output,
    post_json,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash-lite"  # stable; docs: most cost-effective for high throughput


class GeminiProvider:
    provider = "gemini"

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
        self._url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/models/{model}:generateContent"
        self._http = client or httpx2.AsyncClient(timeout=TIMEOUT)
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return f"GeminiProvider(model={self.model!r})"

    async def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResult:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "maxOutputTokens": max_output_tokens,
                "candidateCount": 1,
            },
        }
        headers = {"x-goog-api-key": self._api_key.get_secret_value()}
        data, latency_ms = await post_json(
            self._http,
            self._url,
            headers=headers,
            body=body,
            provider=self.provider,
            retry_delay=self._retry_delay,
        )
        usage = data.get("usageMetadata") or {}
        result = parse_output(_text(data), schema, self.provider)
        return LLMResult(
            data=result,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            # thinking tokens are billed as output
            output_tokens=int(usage.get("candidatesTokenCount", 0))
            + int(usage.get("thoughtsTokenCount", 0)),
            model=str(data.get("modelVersion") or self.model),
            provider=self.provider,
            latency_ms=latency_ms,
        )


def _text(data: dict[str, Any]) -> str:
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise InvalidOutput(f"gemini blocked the prompt: {block}")
    try:
        cand = data["candidates"][0]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("gemini response has no candidates") from None
    reason = cand.get("finishReason", "STOP")
    if reason != "STOP":
        raise InvalidOutput(f"gemini finishReason={reason}")
    parts = (cand.get("content") or {}).get("parts") or []
    # skip thought parts; JSON mode returns the answer as plain text parts
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))
