"""HE-030: Gmail API sender + templates. All credentials are synthetic, built at runtime."""

import base64
import email
import json
import logging
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from email.policy import default
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from arq.worker import get_kwargs

from common.config import get_settings
from common.email import EmailError, GmailSender, RenderedEmail, TokenRevoked, invite, render
from common.email.gmail import SEND_URL, TOKEN_URL, build_message, encode_raw
from scripts import gmail_authorize
from workers import settings as worker_settings

FAKE = "FAKEfake0123456789FAKEfake0123456789"
REFRESH = "1//0g" + FAKE
SECRET = "GOCSPX-" + FAKE
ACCESS = "ya29." + FAKE
SENDER = "sender@example.test"
TO = "someone@example.test"
MAIL = RenderedEmail(subject="Hi", text="plain body", html="<p>html body</p>")

Handler = Callable[[httpx2.Request], httpx2.Response]


class Google:
    """Fake token + Gmail endpoints; `send_statuses` are returned in order, then 200."""

    def __init__(self, *, token_status: int = 200, expires_in: int = 3599) -> None:
        self.token_status = token_status
        self.expires_in = expires_in
        self.send_statuses: list[int] = []
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            if self.token_status != 200:
                return httpx2.Response(
                    self.token_status,
                    json={"error": "invalid_grant", "error_description": "Token has been revoked."},
                )
            return httpx2.Response(
                200, json={"access_token": ACCESS, "expires_in": self.expires_in}
            )
        assert str(request.url) == SEND_URL
        status = self.send_statuses.pop(0) if self.send_statuses else 200
        if status != 200:
            return httpx2.Response(status, json={"error": {"status": "UNAVAILABLE"}})
        return httpx2.Response(200, json={"id": "msg-1", "threadId": "t-1"})

    def calls(self, url: str) -> list[httpx2.Request]:
        return [r for r in self.requests if str(r.url) == url]


def _sender(google: Handler) -> GmailSender:
    return GmailSender(
        sender=SENDER,
        client_id="client-id.apps.googleusercontent.com",
        client_secret=SECRET,
        refresh_token=REFRESH,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(google)),
        retry_delay=0,
    )


def _sent_message(request: httpx2.Request) -> EmailMessage:
    raw = json.loads(request.content)["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=default)
    assert isinstance(msg, EmailMessage)
    return msg


async def test_token_is_refreshed_once_and_cached() -> None:
    google = Google()
    sender = _sender(google)
    assert await sender.send(TO, MAIL) == "msg-1"
    assert await sender.send(TO, MAIL) == "msg-1"

    (token_req,) = google.calls(TOKEN_URL)
    form = parse_qs(token_req.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == [REFRESH]
    sends = google.calls(SEND_URL)
    assert len(sends) == 2
    assert all(r.headers["Authorization"] == f"Bearer {ACCESS}" for r in sends)


async def test_token_near_expiry_is_refreshed() -> None:
    google = Google(expires_in=30)  # inside the 60 s safety margin
    sender = _sender(google)
    await sender.send(TO, MAIL)
    await sender.send(TO, MAIL)
    assert len(google.calls(TOKEN_URL)) == 2


async def test_message_encoding() -> None:
    google = Google()
    await _sender(google).send(TO, MAIL)
    raw = json.loads(google.calls(SEND_URL)[0].content)["raw"]
    assert not set(raw) & {"+", "/"}  # base64url alphabet

    msg = _sent_message(google.calls(SEND_URL)[0])
    assert msg["From"] == f"HackEvents <{SENDER}>"
    assert msg["To"] == TO
    assert msg["Subject"] == "Hi"
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p.get_content().strip() for p in msg.iter_parts()}
    assert parts == {"text/plain": "plain body", "text/html": "<p>html body</p>"}


def test_header_injection_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_message(sender=SENDER, to=f"{TO}\r\nBcc: victim@example.test", email=MAIL)


def test_non_ascii_content_round_trips() -> None:
    mail = RenderedEmail(subject="Closes 20 Nov · IST", text="Café ₹500", html="<p>Café ₹500</p>")
    raw = encode_raw(build_message(sender=SENDER, to=TO, email=mail))
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=default)
    assert msg["Subject"] == "Closes 20 Nov · IST"
    assert isinstance(msg, EmailMessage)
    body = msg.get_body(("plain",))
    assert body is not None and isinstance(body, EmailMessage)
    assert body.get_content().strip() == "Café ₹500"


async def test_retries_on_503_then_succeeds() -> None:
    google = Google()
    google.send_statuses = [503]
    assert await _sender(google).send(TO, MAIL) == "msg-1"
    assert len(google.calls(SEND_URL)) == 2


async def test_retries_on_429_and_gives_up_after_three() -> None:
    google = Google()
    google.send_statuses = [429, 503, 503, 200]
    with pytest.raises(EmailError, match="HTTP 503 UNAVAILABLE"):
        await _sender(google).send(TO, MAIL)
    assert len(google.calls(SEND_URL)) == 3


async def test_network_error_is_retried_and_reported_without_details() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom", request=request)

    with pytest.raises(EmailError, match="Gmail unreachable: ConnectError"):
        await _sender(down).send(TO, MAIL)


async def test_client_error_is_not_retried() -> None:
    google = Google()
    google.send_statuses = [400]
    with pytest.raises(EmailError, match="HTTP 400"):
        await _sender(google).send(TO, MAIL)
    assert len(google.calls(SEND_URL)) == 1


async def test_invalid_grant_raises_token_revoked_alerts_and_logs_no_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    google = Google(token_status=400)
    with pytest.raises(TokenRevoked) as exc_info:
        await _sender(google).send(TO, MAIL)

    assert not google.calls(SEND_URL)
    assert "email.token_revoked" in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    for secret in (REFRESH, SECRET, ACCESS, FAKE):
        assert secret not in caplog.text
        assert secret not in str(exc_info.value)


def test_unconfigured_sender_fails_fast() -> None:
    with pytest.raises(EmailError, match="not configured"):
        GmailSender(sender=SENDER, client_id="", client_secret="", refresh_token="")


# --- templates -------------------------------------------------------------------------


def test_invite_html_is_autoescaped() -> None:
    xss = '<script>alert("x")</script>'
    mail = invite(token="tok123", inviter=xss, app_domain="he.example.test")
    assert "<script>" not in mail.html
    assert "&lt;script&gt;" in mail.html
    assert xss in mail.text  # plain text part is not HTML: shown verbatim, not escaped


def test_invite_links_use_app_domain() -> None:
    mail = invite(token="tok123", inviter="Asha", app_domain="he.example.test")
    link = "https://he.example.test/invite/tok123"
    assert link in mail.text
    assert f'href="{link}"' in mail.html
    assert "72 hours" in mail.text and "72 hours" in mail.html
    assert "Asha invited you" in mail.text
    assert "localhost" not in mail.text + mail.html
    assert "!" not in mail.text  # copy tone: no exclamation marks (UI brief §7)


def test_token_in_link_is_url_quoted() -> None:
    mail = invite(token="a b/../x", inviter="A", app_domain="he.example.test")
    assert "https://he.example.test/invite/a%20b/../x" in mail.text


@pytest.fixture
def app_domain_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_DOMAIN", "from-env.example.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_default_domain_comes_from_settings(app_domain_env: None) -> None:
    mail = render("test")
    assert "https://from-env.example.test/" in mail.text
    assert "https://from-env.example.test/" in mail.html
    assert mail.subject == "HackEvents test email"


# --- worker + scripts ------------------------------------------------------------------


def test_send_email_is_registered_on_notify_worker() -> None:
    cls: Any = worker_settings.NotifyWorker  # arq's get_kwargs is mistyped
    functions: Any = get_kwargs(cls)["functions"]
    assert worker_settings.send_email in functions


async def test_send_email_job_renders_and_sends() -> None:
    google = Google()
    ctx: dict[str, Any] = {"gmail": _sender(google)}
    message_id = await worker_settings.send_email(
        ctx, TO, "invite", {"token": "tok", "inviter": "Asha"}
    )
    assert message_id == "msg-1"
    msg = _sent_message(google.calls(SEND_URL)[0])
    assert msg["Subject"] == "Your HackEvents invite"
    body = msg.get_body(("plain",))
    assert isinstance(body, EmailMessage) and "expires in 72 hours" in body.get_content()


def test_authorize_url_requests_offline_gmail_send_with_pkce() -> None:
    url = gmail_authorize.auth_url(
        client_id="cid", redirect_uri="http://localhost", state="st", verifier="v" * 64, hint=""
    )
    query = parse_qs(url.split("?", 1)[1])
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.send"]
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert "v" * 64 not in url  # only the challenge leaves the machine


def test_authorize_rejects_wrong_state() -> None:
    assert gmail_authorize.code_from_redirect("http://localhost/?state=st&code=c1", "st") == "c1"
    with pytest.raises(SystemExit):
        gmail_authorize.code_from_redirect("http://localhost/?state=evil&code=c1", "st")
