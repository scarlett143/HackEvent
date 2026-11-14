"""LLM adapter contract (API doc B2, guardrails doc 08 A1/A5/A8).

One call shape for every provider: system + user text in, a JSON object matching
`schema` out. Structured JSON output only: no tools, no function calling, no browsing
(AGENTS.md "Never add tools/function-calling to the extraction LLM"). The schema is
enforced in our code (`validate`) whatever the provider claims, so prompts never rely
on one provider's feature (A8).

`common.llm.router.call_llm()` (HE-043) is the single choke point every caller must use:
it routes by role/priority, enforces the per-provider daily/monthly cost caps, falls back
and records `llm_usage`. Nothing else may call `LLMProvider.complete_json`.
"""

import asyncio
import email.utils
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2

TIMEOUT = httpx2.Timeout(30.0, connect=5.0)  # API doc B2: provider timeout 30 s
ATTEMPTS = 3

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """Provider failed (HTTP error, network, malformed envelope). Never carries keys or bodies."""


class RateLimited(ProviderError):
    """HTTP 429. `retry_after` is seconds from the provider, or None if it gave none."""

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class InvalidOutput(ProviderError):
    """The model answered, but not with JSON matching the schema (A5: retry once, then fallback)."""


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    latency_ms: int


class LLMProvider(Protocol):
    provider: str
    model: str

    async def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResult: ...


# --- shared HTTP + parsing helpers -------------------------------------------------------


async def post_json(
    http: httpx2.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    provider: str,
    retry_delay: float,
) -> tuple[dict[str, Any], int]:
    """POST; retry network errors and 5xx with backoff; 429 raises RateLimited at once
    so the router can move to the next provider instead of sleeping here.
    Returns (json body, latency ms of the successful attempt)."""
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        start = time.monotonic()
        try:
            resp = await http.post(url, headers=headers, json=body)
        except httpx2.TransportError as exc:
            if last:
                raise ProviderError(f"{provider} unreachable: {type(exc).__name__}") from None
        else:
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code == 429:
                raise RateLimited(f"{provider} rate limited (HTTP 429)", _retry_after(resp))
            if resp.status_code < 500:
                if resp.status_code != 200:
                    raise ProviderError(
                        f"{provider} HTTP {resp.status_code} {_error_code(resp)}".rstrip()
                    )
                try:
                    data = resp.json()
                except ValueError:
                    raise ProviderError(f"{provider} returned a non-JSON envelope") from None
                if not isinstance(data, dict):
                    raise ProviderError(f"{provider} returned a non-object envelope")
                return data, latency_ms
            if last:
                raise ProviderError(f"{provider} HTTP {resp.status_code} after {ATTEMPTS} tries")
        log.warning("llm.retry provider=%s attempt=%d", provider, attempt + 1)
        await asyncio.sleep(retry_delay * 2**attempt)
    raise AssertionError("unreachable")


def _retry_after(resp: httpx2.Response) -> float | None:
    """`Retry-After` header (seconds or HTTP date), else Google's RetryInfo `retryDelay: "30s"`."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(header)
            except (TypeError, ValueError):
                return None
            return max(0.0, when.timestamp() - time.time())
    try:
        details = resp.json()["error"]["details"]
        for d in details:
            delay = d.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _error_code(resp: httpx2.Response) -> str:
    """Provider error code only (`INVALID_ARGUMENT`, `invalid_request_error`), never the body."""
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return ""
    if isinstance(err, dict):
        return str(err.get("status") or err.get("type") or "")
    return ""


def parse_output(text: str, schema: dict[str, Any], provider: str) -> dict[str, Any]:
    """Model text -> dict validated against schema, else InvalidOutput (no model text in it)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise InvalidOutput(f"{provider} output is not valid JSON") from None
    if not isinstance(data, dict):
        raise InvalidOutput(f"{provider} output is not a JSON object")
    errors = validate(data, schema)
    if errors:
        raise InvalidOutput(f"{provider} output does not match schema: {'; '.join(errors[:5])}")
    return data


# --- JSON Schema subset validator ---------------------------------------------------------
# ponytail: covers the keywords our schemas use (doc 08 A3); jsonschema is not a dependency.
# Unknown validation keywords raise ValueError so a new schema can't silently skip checks;
# switch to the `jsonschema` package if schemas outgrow this list.

_ANNOTATIONS = {"description", "title", "default", "format", "examples", "$schema"}
_KEYWORDS = {
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "maxItems",
    "minItems",
    "maxLength",
    "minLength",
    "minimum",
    "maximum",
    "anyOf",
}
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _is_type(value: Any, name: str) -> bool:
    if name not in _TYPES:
        raise ValueError(f"unsupported schema type {name!r}")
    if isinstance(value, bool) and name in ("number", "integer"):
        return False
    return isinstance(value, _TYPES[name])


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Errors (as `path: problem`) for value against schema; empty list = valid."""
    unknown = set(schema) - _KEYWORDS - _ANNOTATIONS
    if unknown:
        raise ValueError(f"unsupported schema keyword(s) at {path}: {sorted(unknown)}")
    errors: list[str] = []
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in types):
            return [f"{path}: expected {'/'.join(types)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not one of the allowed values")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal the constant")
    if "anyOf" in schema and all(validate(value, s, path) for s in schema["anyOf"]):
        errors.append(f"{path}: matches no anyOf branch")
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", len(value)):
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']}")
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", len(value)):
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        errors += [f"{path}.{k}: required" for k in schema.get("required", []) if k not in value]
        for k, v in value.items():
            if k in props:
                errors += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{k}: unexpected property")
    return errors
