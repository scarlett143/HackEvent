"""HE-040/041: LLM adapter contract tests. MockTransport only: no network, synthetic keys.

The same contract runs for every provider (guardrails A8: providers are independent).
"""

import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from common import crypto
from common.llm import InvalidOutput, ProviderError, RateLimited, build_provider
from common.llm.anthropic import API_VERSION, AnthropicProvider, api_schema
from common.llm.base import LLMProvider, validate
from common.llm.gemini import GeminiProvider
from common.models import LlmProvider, LlmProviderType

KEY = "AIza" + "FAKEfake0123456789FAKEfake012345678"  # synthetic, AIza shape for the redactor
MASTER = os.urandom(32)

# doc 08 A3, schema v1
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["is_hackathon", "events"],
    "properties": {
        "is_hackathon": {"type": "boolean"},
        "events": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "required": ["title", "organiser", "evidence", "confidence"],
                "properties": {
                    "title": {"type": "string", "maxLength": 200},
                    "organiser": {"type": "string", "maxLength": 200},
                    "registration_deadline": {"type": ["string", "null"]},
                    "mode": {"enum": ["online", "offline", "hybrid", "unknown"]},
                    "evidence": {"type": "string", "maxLength": 600},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    },
}
GOOD: dict[str, Any] = {
    "is_hackathon": True,
    "events": [
        {
            "title": "HackX",
            "organiser": "IIT X",
            "registration_deadline": "2026-12-01",
            "mode": "online",
            "evidence": "Register by 1 Dec 2026",
            "confidence": 0.9,
        }
    ],
}
FORBIDDEN_KEYS = {"tools", "toolConfig", "tool_config", "tool_choice", "functionDeclarations"}


@dataclass
class Case:
    name: str
    type: LlmProviderType
    model: str
    url: str
    key_header: str
    make: Callable[[httpx2.AsyncClient], LLMProvider]
    ok: Callable[[str, int, int], dict[str, Any]]  # (model text, in tokens, out tokens) -> body
    sent_schema: Callable[[dict[str, Any]], Any]


def _gemini_ok(text: str, tin: int, tout: int) -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
        ],
        "usageMetadata": {
            "promptTokenCount": tin,
            "candidatesTokenCount": tout,
            "totalTokenCount": tin + tout,
        },
        "modelVersion": "gemini-3.5-flash-lite",
    }


def _anthropic_ok(text: str, tin: int, tout: int, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": tin, "output_tokens": tout},
    }


CASES = [
    Case(
        name="gemini",
        type=LlmProviderType.gemini,
        model="gemini-3.5-flash-lite",
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
        key_header="x-goog-api-key",
        make=lambda c: GeminiProvider(
            api_key=SecretStr(KEY), model="gemini-3.5-flash-lite", client=c, retry_delay=0
        ),
        ok=_gemini_ok,
        sent_schema=lambda b: b["generationConfig"]["responseJsonSchema"],
    ),
    Case(
        name="anthropic",
        type=LlmProviderType.anthropic,
        model="claude-haiku-5-5",
        url="https://api.anthropic.com/v1/messages",
        key_header="x-api-key",
        make=lambda c: AnthropicProvider(
            api_key=SecretStr(KEY), model="claude-haiku-5-5", client=c, retry_delay=0
        ),
        ok=_anthropic_ok,
        sent_schema=lambda b: b["output_config"]["format"]["schema"],
    ),
]


class Fake:
    """Returns queued (status, body, headers) in order, then the last one forever."""

    def __init__(self, *responses: tuple[int, Any, dict[str, str]]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body, headers = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        return httpx2.Response(status, json=body, headers=headers)


def _provider(case: Case, fake: Fake) -> LLMProvider:
    return case.make(httpx2.AsyncClient(transport=httpx2.MockTransport(fake)))


async def _call(p: LLMProvider) -> Any:
    return await p.complete_json(
        system="sys prompt", user="page text", schema=SCHEMA, max_output_tokens=512
    )


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


@pytest.fixture(params=CASES, ids=lambda c: c.name)
def case(request: pytest.FixtureRequest) -> Case:
    return request.param  # type: ignore[no-any-return]


async def test_request_shape(case: Case) -> None:
    fake = Fake((200, case.ok(json.dumps(GOOD), 1, 1), {}))
    await _call(_provider(case, fake))
    (req,) = fake.requests
    assert req.method == "POST"
    assert str(req.url) == case.url
    assert KEY not in str(req.url)  # key never in the URL, so never in access logs
    assert req.headers[case.key_header] == KEY
    body = json.loads(req.content)
    assert case.sent_schema(body)["type"] == "object"
    assert not (_keys(body) & FORBIDDEN_KEYS), "extraction LLM must never get tools"
    assert "sys prompt" in json.dumps(body) and "page text" in json.dumps(body)


async def test_parses_data_and_usage(case: Case) -> None:
    fake = Fake((200, case.ok(json.dumps(GOOD), 1234, 56), {}))
    result = await _call(_provider(case, fake))
    assert result.data == GOOD
    assert (result.input_tokens, result.output_tokens) == (1234, 56)
    assert result.provider == case.name
    assert result.model
    assert result.latency_ms >= 0


async def test_429_raises_rate_limited_without_retry(case: Case) -> None:
    fake = Fake((429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, {"retry-after": "7"}))
    with pytest.raises(RateLimited) as exc:
        await _call(_provider(case, fake))
    assert exc.value.retry_after == 7.0
    assert len(fake.requests) == 1


async def test_5xx_retried_then_provider_error(case: Case) -> None:
    fake = Fake((503, {"error": {"status": "UNAVAILABLE"}}, {}))
    with pytest.raises(ProviderError) as exc:
        await _call(_provider(case, fake))
    assert not isinstance(exc.value, (RateLimited, InvalidOutput))
    assert len(fake.requests) == 3


async def test_5xx_then_success(case: Case) -> None:
    fake = Fake((500, {}, {}), (200, case.ok(json.dumps(GOOD), 1, 1), {}))
    assert (await _call(_provider(case, fake))).data == GOOD
    assert len(fake.requests) == 2


async def test_4xx_not_retried(case: Case) -> None:
    fake = Fake((400, {"error": {"status": "INVALID_ARGUMENT"}}, {}))
    with pytest.raises(ProviderError):
        await _call(_provider(case, fake))
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "[1, 2]",
        json.dumps({"is_hackathon": True}),  # missing events
        json.dumps({**GOOD, "events": [{**GOOD["events"][0], "confidence": 3}]}),
        json.dumps({**GOOD, "events": [{**GOOD["events"][0], "mode": "moon"}]}),
        json.dumps({**GOOD, "events": [{**GOOD["events"][0], "title": "x" * 201}]}),
    ],
    ids=["not-json", "not-object", "missing", "out-of-range", "bad-enum", "too-long"],
)
async def test_invalid_output(case: Case, text: str) -> None:
    fake = Fake((200, case.ok(text, 1, 1), {}))
    with pytest.raises(InvalidOutput):
        await _call(_provider(case, fake))


async def test_key_never_logged_or_in_errors(case: Case, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    for fake in (
        Fake((429, {}, {})),
        Fake((503, {}, {})),
        Fake((401, {"error": {"status": "UNAUTHENTICATED"}}, {})),
        Fake((200, case.ok("garbage", 1, 1), {})),
    ):
        p = _provider(case, fake)
        seen.append(repr(p))
        with pytest.raises(ProviderError) as exc:
            await _call(p)
        seen += [str(exc.value), repr(exc.value)]
    await _call(_provider(case, Fake((200, case.ok(json.dumps(GOOD), 1, 1), {}))))
    assert "llm.retry" in caplog.text  # the retry path did log
    for text in [caplog.text, *seen]:
        assert KEY not in text


async def test_factory_decrypts_with_row_aad(case: Case) -> None:
    row_id = uuid.uuid4()
    row = LlmProvider(
        id=row_id,
        type=case.type,
        model=case.model,
        base_url=None,
        api_key_enc=crypto.encrypt(
            KEY.encode(), crypto.aad("llm_providers", "api_key_enc", row_id), MASTER
        ),
    )
    fake = Fake((200, case.ok(json.dumps(GOOD), 1, 1), {}))
    p = build_provider(
        row, client=httpx2.AsyncClient(transport=httpx2.MockTransport(fake)), master_key=MASTER
    )
    assert p.provider == case.name
    assert KEY not in repr(p)
    await _call(p)
    assert fake.requests[0].headers[case.key_header] == KEY

    # ciphertext copied from another row does not decrypt
    row.id = uuid.uuid4()
    with pytest.raises(crypto.DecryptionError):
        build_provider(row, master_key=MASTER)


def test_gemini_retry_info_delay() -> None:
    from common.llm.base import _retry_after

    resp = httpx2.Response(
        429,
        json={
            "error": {
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "31s"}
                ]
            }
        },
    )
    assert _retry_after(resp) == 31.0
    assert _retry_after(httpx2.Response(429)) is None


def test_validator() -> None:
    assert validate(GOOD, SCHEMA) == []
    assert validate({"is_hackathon": 1, "events": []}, SCHEMA)  # bool is not int and vice versa
    with pytest.raises(ValueError, match="unsupported schema keyword"):
        validate({}, {"type": "object", "patternProperties": {}})


async def test_anthropic_request_details() -> None:
    fake = Fake((200, _anthropic_ok(json.dumps(GOOD), 1, 1), {}))
    await _call(_provider(CASES[1], fake))
    req = fake.requests[0]
    assert req.headers["anthropic-version"] == API_VERSION
    body = json.loads(req.content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 512
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k"} & set(body)  # removed in 2026-10-21


def test_anthropic_schema_drops_unsupported_constraints() -> None:
    sent = api_schema(SCHEMA)
    assert sent["additionalProperties"] is False
    event = sent["properties"]["events"]["items"]
    assert event["additionalProperties"] is False
    assert "maxItems" not in sent["properties"]["events"]
    assert "maxLength" not in event["properties"]["title"]
    assert "minimum" not in event["properties"]["confidence"]
    assert event["required"] == SCHEMA["properties"]["events"]["items"]["required"]
    # a property *named* like a keyword survives
    assert "maximum" in api_schema({"type": "object", "properties": {"maximum": {}}})["properties"]
    assert SCHEMA["properties"]["events"]["maxItems"] == 10  # input not mutated


@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
async def test_anthropic_incomplete_stop_is_invalid_output(stop: str) -> None:
    fake = Fake((200, _anthropic_ok(json.dumps(GOOD), 1, 1, stop), {}))
    with pytest.raises(InvalidOutput, match=stop):
        await _call(_provider(CASES[1], fake))


async def test_gemini_non_stop_finish_is_invalid_output() -> None:
    body = _gemini_ok(json.dumps(GOOD), 1, 1)
    body["candidates"][0]["finishReason"] = "MAX_TOKENS"
    with pytest.raises(InvalidOutput, match="MAX_TOKENS"):
        await _call(_provider(CASES[0], Fake((200, body, {}))))
