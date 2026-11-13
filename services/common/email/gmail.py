"""Gmail API sender (TRD §2 Email row, API doc B3, runbook B7).

Refresh token -> access token (cached until shortly before expiry) -> users.messages.send
with a base64url RFC 5322 message (multipart/alternative: text + HTML).
Scope `gmail.send` only. Tokens are never logged or put in exception messages.
"""

import asyncio
import base64
import logging
import time
from email.message import EmailMessage
from typing import Any, Self

import httpx2

from common.config import Settings, get_settings
from common.email.templates import RenderedEmail

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
TIMEOUT = httpx2.Timeout(15.0, connect=5.0)
ATTEMPTS = 3
EXPIRY_MARGIN_S = 60

log = logging.getLogger(__name__)


class EmailError(Exception):
    """Email could not be sent."""


class TokenRevoked(EmailError):
    """Google answered `invalid_grant`: the refresh token is revoked or expired.

    Needs a human: re-run `python -m scripts.gmail_authorize` (runbook B7).
    """


def alert_admin_token_revoked() -> None:
    """Admin alert hook for a dead sender token (HE-030 acceptance).

    Only a log event until the Admin notification channel exists; swap the body then.
    """
    log.error("email.token_revoked: re-run `python -m scripts.gmail_authorize` (runbook B7)")


def build_message(*, sender: str, to: str, email: RenderedEmail) -> EmailMessage:
    """RFC 5322 message; raises ValueError on CR/LF in any header (header injection)."""
    msg = EmailMessage()
    msg["From"] = f"HackEvents <{sender}>"
    msg["To"] = to
    msg["Subject"] = email.subject
    msg.set_content(email.text)
    msg.add_alternative(email.html, subtype="html")
    return msg


def encode_raw(msg: EmailMessage) -> str:
    """Gmail `raw` field: base64url of the full message bytes."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


class GmailSender:
    def __init__(
        self,
        *,
        sender: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        client: httpx2.AsyncClient | None = None,
        retry_delay: float = 1.0,
    ) -> None:
        if not (sender and client_id and client_secret and refresh_token):
            raise EmailError(
                "Gmail not configured: set GMAIL_SENDER, GMAIL_OAUTH_REFRESH_TOKEN, "
                "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (runbook B7)"
            )
        self.sender = sender
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._http = client or httpx2.AsyncClient(timeout=TIMEOUT)
        self._retry_delay = retry_delay
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, client: httpx2.AsyncClient | None = None
    ) -> Self:
        s = settings or get_settings()
        return cls(
            sender=s.gmail_sender,
            client_id=s.google_client_id,
            client_secret=s.google_client_secret.get_secret_value(),
            refresh_token=s.gmail_oauth_refresh_token.get_secret_value(),
            client=client,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, to: str, email: RenderedEmail) -> str:
        """Send and return the Gmail message id."""
        raw = encode_raw(build_message(sender=self.sender, to=to, email=email))
        token = await self._access_token()
        resp = await self._post(
            SEND_URL, json={"raw": raw}, headers={"Authorization": f"Bearer {token}"}
        )
        if resp.status_code == 401:
            self._token = None  # stale/revoked access token; next call refreshes
        if resp.status_code != 200:
            raise EmailError(f"Gmail send failed: HTTP {resp.status_code} {_google_error(resp)}")
        message_id = str(resp.json()["id"])
        log.info("email.sent id=%s template_subject=%r", message_id, email.subject)
        return message_id

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            resp = await self._post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
            )
            if resp.status_code != 200:
                error = _google_error(resp)
                if error == "invalid_grant":
                    alert_admin_token_revoked()
                    raise TokenRevoked("Gmail refresh token revoked or expired (invalid_grant)")
                raise EmailError(f"Gmail token refresh failed: HTTP {resp.status_code} {error}")
            body = resp.json()
            self._token = str(body["access_token"])
            self._expires_at = time.monotonic() + int(body["expires_in"]) - EXPIRY_MARGIN_S
            return self._token

    async def _post(self, url: str, **kwargs: Any) -> httpx2.Response:
        """POST with up to ATTEMPTS tries on network errors, 429 and 5xx."""
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1
            try:
                resp = await self._http.post(url, **kwargs)
            except httpx2.TransportError as exc:
                if last:
                    raise EmailError(f"Gmail unreachable: {type(exc).__name__}") from None
            else:
                if last or (resp.status_code != 429 and resp.status_code < 500):
                    return resp
            log.warning("email.retry attempt=%d url=%s", attempt + 1, url)
            await asyncio.sleep(self._retry_delay * 2**attempt)
        raise AssertionError("unreachable")


def _google_error(resp: httpx2.Response) -> str:
    """Error code only (`invalid_grant`, `rateLimitExceeded` ...), never the body."""
    try:
        err = resp.json().get("error")
    except ValueError:
        return ""
    if isinstance(err, str):
        return err  # OAuth endpoint: {"error": "invalid_grant", ...}
    if isinstance(err, dict):
        return str(err.get("status", ""))  # Gmail API: {"error": {"status": "..."}}
    return ""
