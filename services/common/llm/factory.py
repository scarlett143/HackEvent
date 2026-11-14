"""Build a provider from an `llm_providers` row (TRD: LLM keys live encrypted in the DB).

The key is decrypted with the row-bound AAD, wrapped in SecretStr straight away and only
unwrapped when the request header is built, so it never shows up in repr/str/logs.
"""

import httpx2
from pydantic import SecretStr

from common import crypto
from common.llm.anthropic import AnthropicProvider
from common.llm.base import LLMProvider, ProviderError
from common.llm.gemini import GeminiProvider
from common.models import LlmProvider, LlmProviderType


def decrypt_api_key(row: LlmProvider, master_key: bytes | None = None) -> SecretStr:
    blob = crypto.decrypt(
        row.api_key_enc, crypto.aad("llm_providers", "api_key_enc", row.id), master_key
    )
    return SecretStr(blob.decode())


def build_provider(
    row: LlmProvider,
    *,
    client: httpx2.AsyncClient | None = None,
    master_key: bytes | None = None,
) -> LLMProvider:
    key = decrypt_api_key(row, master_key)
    if row.type == LlmProviderType.gemini:
        return GeminiProvider(api_key=key, model=row.model, base_url=row.base_url, client=client)
    if row.type == LlmProviderType.anthropic:
        return AnthropicProvider(api_key=key, model=row.model, base_url=row.base_url, client=client)
    raise ProviderError(f"provider type {row.type} is not implemented")
