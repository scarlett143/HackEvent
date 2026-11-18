"""SMTP sender (runbook B7.5): TLS always, verified certs, retries, permanent auth errors."""

import smtplib
import ssl
from typing import Any, ClassVar, Self

import pytest

from common.config import get_settings
from common.email import (
    CredentialsRejected,
    EmailError,
    SmtpSender,
    TokenRevoked,
    render,
    sender_from_settings,
)

EMAIL = render("test", app_domain="hack.example.org")


class FakeSMTP:
    """Records the conversation; `fail` = exceptions raised by send_message, in order."""

    instances: ClassVar[list["FakeSMTP"]] = []
    fail: ClassVar[list[BaseException]] = []

    def __init__(self, host: str, port: int, timeout: float, context: Any = None) -> None:
        self.host, self.port, self.timeout, self.ssl_context = host, port, timeout, context
        self.calls: list[tuple[str, Any]] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append(("quit", None))

    def starttls(self, context: ssl.SSLContext) -> None:
        self.calls.append(("starttls", context))

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user, password)))
        if password == "wrong":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg: Any) -> None:
        if FakeSMTP.fail:
            raise FakeSMTP.fail.pop(0)
        self.calls.append(("send", msg))


@pytest.fixture(autouse=True)
def reset() -> None:
    FakeSMTP.instances, FakeSMTP.fail = [], []


def sender(port: int = 587, password: str = "app-password-1234") -> SmtpSender:
    return SmtpSender(
        host="smtp.example.org",
        port=port,
        username="sender@example.org",
        password=password,
        sender="sender@example.org",
        smtp_factory=FakeSMTP,
        retry_delay=0,
    )


async def test_starttls_with_verified_context_then_login_and_send() -> None:
    mid = await sender().send("prof@example.org", EMAIL)
    (s,) = FakeSMTP.instances
    assert (s.host, s.port, s.ssl_context) == ("smtp.example.org", 587, None)
    names = [c[0] for c in s.calls]
    assert names == ["starttls", "login", "send", "quit"]  # TLS before credentials
    ctx = s.calls[0][1]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    msg = s.calls[2][1]
    assert msg["To"] == "prof@example.org" and msg["Subject"] == EMAIL.subject
    assert msg["From"] == "HackEvents <sender@example.org>"
    assert mid == msg["Message-ID"] and mid.endswith("@example.org>")
    assert msg.get_body(("plain",)) is not None and msg.get_body(("html",)) is not None


async def test_port_465_uses_implicit_tls() -> None:
    await sender(port=465).send("prof@example.org", EMAIL)
    (s,) = FakeSMTP.instances
    assert s.ssl_context is not None and s.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert [c[0] for c in s.calls] == ["login", "send", "quit"]  # no STARTTLS on 465


async def test_rejected_login_is_permanent_and_hides_the_password() -> None:
    with pytest.raises(CredentialsRejected) as e:
        await sender(password="wrong").send("prof@example.org", EMAIL)
    assert isinstance(e.value, TokenRevoked)  # notifier / dispatcher: no retries
    assert "wrong" not in str(e.value)
    assert len(FakeSMTP.instances) == 1


async def test_transient_failures_are_retried_then_succeed() -> None:
    FakeSMTP.fail = [
        smtplib.SMTPServerDisconnected("dropped"),
        smtplib.SMTPDataError(451, b"try later"),
    ]
    await sender().send("prof@example.org", EMAIL)
    assert len(FakeSMTP.instances) == 3


async def test_permanent_reply_is_not_retried() -> None:
    FakeSMTP.fail = [smtplib.SMTPDataError(550, b"mailbox unavailable")]
    with pytest.raises(EmailError, match="SMTPDataError") as e:
        await sender().send("prof@example.org", EMAIL)
    assert not isinstance(e.value, TokenRevoked)
    assert len(FakeSMTP.instances) == 1


async def test_header_injection_is_refused_before_connecting() -> None:
    with pytest.raises(ValueError):
        await sender().send("prof@example.org\r\nBcc: x@evil.example", EMAIL)
    assert FakeSMTP.instances == []


def test_incomplete_config_is_a_clear_error() -> None:
    with pytest.raises(EmailError, match="SMTP not configured"):
        SmtpSender(host="smtp.example.org", port=587, username="", password="x", sender="")


def test_sender_from_settings_picks_smtp_only_when_host_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k, v in {
        "SMTP_HOST": "smtp.example.org",
        "SMTP_USERNAME": "sender@example.org",
        "SMTP_PASSWORD": "app-password-1234",
        "GMAIL_SENDER": "",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        s = sender_from_settings()
        assert isinstance(s, SmtpSender) and s.sender == "sender@example.org"
        monkeypatch.setenv("SMTP_HOST", "")
        get_settings.cache_clear()
        with pytest.raises(EmailError, match="Gmail not configured"):
            sender_from_settings()  # falls back to the Gmail API path
    finally:
        get_settings.cache_clear()
