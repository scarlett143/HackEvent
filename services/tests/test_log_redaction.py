"""HE-007: secrets never reach log output (Security doc §6, AGENTS.md "Never log ...").

All secrets below are synthetic and assembled at runtime so no key-shaped literal is
committed to this public repo.
"""

import base64
import io
import logging
import logging.config
from collections.abc import Iterator

import pytest
from arq.logs import default_log_config
from arq.worker import get_kwargs
from uvicorn.config import LOGGING_CONFIG

from common.logging import REDACTED, RedactingFilter, configure_logging, redact
from workers import settings as worker_settings

FAKE = "FAKEfake0123456789FAKEfake0123456789"  # 36 chars, obviously synthetic


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


JWT = f"{_b64('{"alg":"HS256"}')}.{_b64('{"sub":"fake-user"}')}.{_b64('fake-signature')}"
GOOGLE_KEY = "AIza" + FAKE[:35]
ANTHROPIC_KEY = "sk-ant-api03-" + FAKE
OPENAI_KEY = "sk-proj-" + FAKE
GOOGLE_ACCESS = "ya29." + FAKE
GOOGLE_REFRESH = "1//0g" + FAKE
TELEGRAM = "123456789:" + FAKE[:35]
FCM = "fakeDeviceId0123:APA91b" + FAKE * 3

# (id, input text, secret that must disappear, exact expected output)
CASES = [
    (
        "authorization-header",
        f"headers Authorization: Bearer {FAKE}",
        FAKE,
        f"headers Authorization: {REDACTED}",
    ),
    ("bearer-inline", f"sent Bearer {FAKE} upstream", FAKE, f"sent Bearer {REDACTED} upstream"),
    (
        "cookie-header",
        f"Cookie: he_session={FAKE}; csrftoken=abc",
        FAKE,
        f"Cookie: {REDACTED}",
    ),
    (
        "set-cookie-header",
        f"resp Set-Cookie: he_session={FAKE}; HttpOnly; Secure",
        FAKE,
        f"resp Set-Cookie: {REDACTED}",
    ),
    (
        "google-api-key",
        f"maps call with {GOOGLE_KEY} ok",
        GOOGLE_KEY,
        f"maps call with {REDACTED} ok",
    ),
    ("anthropic-key", f"provider key {ANTHROPIC_KEY}.", ANTHROPIC_KEY, f"provider key {REDACTED}."),
    ("openai-key", f"provider key {OPENAI_KEY}", OPENAI_KEY, f"provider key {REDACTED}"),
    (
        "query-string-token",
        f"GET /cb?state=ok&access_token={FAKE}&page=2",
        FAKE,
        f"GET /cb?state=ok&access_token={REDACTED}&page=2",
    ),
    (
        "query-string-key",
        f"GET https://maps.example.test/api?key={FAKE}&q=pune",
        FAKE,
        f"GET https://maps.example.test/api?key={REDACTED}&q=pune",
    ),
    (
        "json-password",
        '{"password": "fake-pass-123", "user": "alice"}',
        "fake-pass-123",
        f'{{"password": "{REDACTED}", "user": "alice"}}',
    ),
    (
        "json-authorization",
        f'{{"Authorization": "Bearer {FAKE}", "Accept": "*/*"}}',
        FAKE,
        f'{{"Authorization": "{REDACTED}", "Accept": "*/*"}}',
    ),
    (
        "dict-repr-api-key",
        f"{{'api_key': '{FAKE}', 'model': 'm'}}",
        FAKE,
        f"{{'api_key': '{REDACTED}', 'model': 'm'}}",
    ),
    (
        "kwargs-password",
        "connect(user='alice', password='fake-pass-123')",
        "fake-pass-123",
        f"connect(user='alice', password='{REDACTED}')",
    ),
    (
        "env-style-secret",
        "GOOGLE_CLIENT_SECRET=fake-client-secret loaded",
        "fake-client-secret",
        f"GOOGLE_CLIENT_SECRET={REDACTED} loaded",
    ),
    ("jwt", f"session jwt {JWT} issued", JWT, f"session jwt {REDACTED} issued"),
    ("google-access-token", f"oauth {GOOGLE_ACCESS} ok", GOOGLE_ACCESS, f"oauth {REDACTED} ok"),
    ("google-refresh-token", f"refresh {GOOGLE_REFRESH}", GOOGLE_REFRESH, f"refresh {REDACTED}"),
    ("telegram-token", f"bot {TELEGRAM} started", TELEGRAM, f"bot {REDACTED} started"),
    (
        "telegram-token-in-url",
        f"POST https://api.telegram.org/bot{TELEGRAM}/sendMessage",
        TELEGRAM,
        f"POST https://api.telegram.org/bot{REDACTED}/sendMessage",
    ),
    ("fcm-token", f"push to {FCM} failed", FCM, f"push to {REDACTED} failed"),
    (
        "database-url",
        "connecting postgresql+asyncpg://hackevents:fake-db-pw@postgres:5432/hackevents",
        "fake-db-pw",
        f"connecting postgresql+asyncpg://hackevents:{REDACTED}@postgres:5432/hackevents",
    ),
    (
        "redis-url-no-user",
        "redis at redis://:fake-redis-pw@redis:6379/0",
        "fake-redis-pw",
        f"redis at redis://:{REDACTED}@redis:6379/0",
    ),
]
IDS = [c[0] for c in CASES]

NEGATIVES = [
    "token bucket refilled: 5 tokens left",
    "max_tokens=1024 input_tokens=200 output_tokens=50",
    "Event starts 2026-11-13T10:30:00+05:30, deadline 2026-11-20",
    "Smart India Hackathon 2026: Grand Finale (Pune) at 12:30",
    "GET /api/v1/events?page=2&sort=deadline 200",
    "monkey=banana turkey=ok",
    "Bearer of good news",
    "http://localhost:8000/api/v1/health",
    "skip sk-short and he:scrape:health-check",
    "the session was a basic workshop on cookies and tokens",
]


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(message)s"))
        self.addFilter(RedactingFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))


@pytest.fixture
def cap() -> Iterator[tuple[logging.Logger, _Capture]]:
    logger = logging.getLogger("he.test.redaction")
    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, handler
    logger.removeHandler(handler)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    names = ["", "uvicorn", "uvicorn.error", "uvicorn.access", "arq"]
    saved = {
        n: (lg.handlers[:], lg.level, lg.propagate, lg.disabled)
        for n in names
        for lg in [logging.getLogger(n)]
    }
    yield
    for n, (handlers, level, propagate, disabled) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate, lg.disabled = propagate, disabled


@pytest.mark.parametrize(("_id", "text", "secret", "expected"), CASES, ids=IDS)
def test_redact_pattern(_id: str, text: str, secret: str, expected: str) -> None:
    out = redact(text)
    assert secret not in out
    assert out == expected


@pytest.mark.parametrize(("_id", "text", "secret", "expected"), CASES, ids=IDS)
def test_msg_is_redacted(
    cap: tuple[logging.Logger, _Capture], _id: str, text: str, secret: str, expected: str
) -> None:
    logger, h = cap
    logger.info(text)
    assert h.lines == [expected]


@pytest.mark.parametrize(("_id", "text", "secret", "expected"), CASES, ids=IDS)
def test_percent_args_are_redacted(
    cap: tuple[logging.Logger, _Capture], _id: str, text: str, secret: str, expected: str
) -> None:
    logger, h = cap
    logger.info("upstream said %s (attempt %d)", text, 2)
    assert h.lines == [f"upstream said {expected} (attempt 2)"]


@pytest.mark.parametrize(("_id", "text", "secret", "expected"), CASES, ids=IDS)
def test_extra_fields_are_redacted(
    cap: tuple[logging.Logger, _Capture], _id: str, text: str, secret: str, expected: str
) -> None:
    logger, h = cap
    logger.info("ctx", extra={"detail": text, "payload": {"items": [text], "n": 1}})
    rec = h.records[0]
    assert rec.__dict__["detail"] == expected
    assert rec.__dict__["payload"] == {"items": [expected], "n": 1}


@pytest.mark.parametrize(("_id", "text", "secret", "expected"), CASES, ids=IDS)
def test_exception_text_is_redacted(
    cap: tuple[logging.Logger, _Capture], _id: str, text: str, secret: str, expected: str
) -> None:
    logger, h = cap
    try:
        raise RuntimeError(text)
    except RuntimeError:
        logger.exception("request failed")
    out = h.lines[0]
    assert secret not in out
    assert f"RuntimeError: {expected}" in out
    assert "Traceback (most recent call last)" in out


def test_secret_split_across_template_and_arg(cap: tuple[logging.Logger, _Capture]) -> None:
    logger, h = cap
    logger.info("token=%s user=%s", FAKE, "alice")
    assert h.lines == [f"token={REDACTED} user=alice"]


def test_sensitive_keys_in_extra_are_redacted_whole(cap: tuple[logging.Logger, _Capture]) -> None:
    logger, h = cap
    headers = {"Authorization": "opaque", "Cookie": "a=b", "X-Api-Key": "k", "Accept": "*/*"}
    logger.info("req", extra={"headers": headers, "fcm_token": "opaque-device-token"})
    rec = h.records[0]
    assert rec.__dict__["headers"] == {
        "Authorization": REDACTED,
        "Cookie": REDACTED,
        "X-Api-Key": REDACTED,
        "Accept": "*/*",
    }
    assert rec.__dict__["fcm_token"] == REDACTED


@pytest.mark.parametrize("text", NEGATIVES)
def test_normal_text_unchanged(cap: tuple[logging.Logger, _Capture], text: str) -> None:
    logger, h = cap
    assert redact(text) == text
    logger.info("%s", text, extra={"title": text})
    assert h.lines == [text]
    assert h.records[0].__dict__["title"] == text
    assert h.records[0].args == (text,)  # untouched records keep their args


def test_filtering_twice_is_idempotent() -> None:
    rec = logging.LogRecord("x", logging.INFO, "", 0, "token=%s", (FAKE,), None)
    f = RedactingFilter()
    assert f.filter(rec) and f.filter(rec)
    assert RedactingFilter().filter(rec)
    assert rec.getMessage() == f"token={REDACTED}"
    assert redact(redact(f"token={FAKE}")) == f"token={REDACTED}"


def test_uvicorn_child_logger_is_redacted(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging.config.dictConfig(LOGGING_CONFIG)  # what uvicorn does before importing the app
    configure_logging()
    logging.getLogger("uvicorn.error.he_child").error("auth header Authorization: Bearer %s", FAKE)
    err = capsys.readouterr().err
    assert FAKE not in err
    assert f"auth header Authorization: {REDACTED}" in err


def test_uvicorn_access_log_keeps_working_and_is_redacted(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging.config.dictConfig(LOGGING_CONFIG)
    configure_logging()
    path = f"/api/v1/auth/callback?code=c&access_token={FAKE}"
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", "GET", path, "1.1", 200
    )
    out = capsys.readouterr().out
    assert FAKE not in out
    assert f"access_token={REDACTED}" in out and "200" in out


def test_api_startup_installs_filter_on_root(restore_logging: None) -> None:
    import api.main  # noqa: F401  module init calls configure_logging()

    configure_logging()
    root = logging.getLogger()
    assert root.handlers
    for handler in root.handlers:
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)


@pytest.mark.parametrize(
    "cls",
    [
        worker_settings.ScrapeWorker,
        worker_settings.LlmWorker,
        worker_settings.NotifyWorker,
        worker_settings.SchedulerWorker,
    ],
)
async def test_arq_workers_install_redaction_on_startup(
    restore_logging: None, capsys: pytest.CaptureFixture[str], cls: type
) -> None:
    on_startup: object = get_kwargs(cls)["on_startup"]  # exactly what arq passes to Worker
    assert on_startup is worker_settings.install_log_redaction
    logging.config.dictConfig(default_log_config(verbose=False))  # arq CLI, after import
    await worker_settings.install_log_redaction({})
    logging.getLogger("arq.worker").info("redis_url=redis://:%s@redis:6379/0", "fake-redis-pw")
    err = capsys.readouterr().err
    assert "fake-redis-pw" not in err
    assert f"redis://:{REDACTED}@redis:6379/0" in err


def test_configure_logging_is_idempotent(restore_logging: None) -> None:
    handler = logging.StreamHandler(io.StringIO())
    logging.getLogger().addHandler(handler)
    configure_logging()
    configure_logging()
    assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
