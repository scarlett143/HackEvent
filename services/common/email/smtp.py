"""SMTP sender (runbook B7 step 5): e.g. Gmail `smtp.gmail.com:587` with an app password.

Same interface as GmailSender: `await send(to, RenderedEmail) -> message id`, raising
EmailError (retry later) or CredentialsRejected (a TokenRevoked: needs a human, no retry).

- TLS is always on and certificates are verified: STARTTLS on 587 (default), implicit TLS
  on 465. Plain-text SMTP is not supported (credentials would cross the network in clear).
- stdlib smtplib runs in a worker thread so the event loop is never blocked.
- Temporary failures (connection drop, timeout, 4xx reply) are retried ATTEMPTS times.
- The password is never logged or put in an exception message.
"""

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Self

from common.config import Settings, get_settings
from common.email.gmail import EmailError, TokenRevoked, build_message
from common.email.templates import RenderedEmail

TIMEOUT_S = 20.0
ATTEMPTS = 3
SSL_PORT = 465

log = logging.getLogger(__name__)


class CredentialsRejected(TokenRevoked):
    """The SMTP server refused the login (wrong / revoked app password). Needs a human."""


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionError))


class SmtpSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        smtp_factory: Callable[..., Any] | None = None,
        retry_delay: float = 1.0,
    ) -> None:
        if not (host and username and password and sender):
            raise EmailError(
                "SMTP not configured: set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD "
                "(and SMTP_FROM if it differs from the username)"
            )
        self.sender = sender
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._ssl = port == SSL_PORT
        self._factory: Callable[..., Any] = smtp_factory or (
            smtplib.SMTP_SSL if self._ssl else smtplib.SMTP
        )
        self._retry_delay = retry_delay

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        s = settings or get_settings()
        return cls(
            host=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_username,
            password=s.smtp_password.get_secret_value(),
            sender=s.smtp_from or s.smtp_username,
        )

    async def aclose(self) -> None:  # parity with GmailSender; one connection per send
        return None

    async def send(self, to: str, email: RenderedEmail) -> str:
        """Send and return the Message-ID. Raises EmailError / CredentialsRejected."""
        msg = build_message(sender=self.sender, to=to, email=email)  # ValueError on CR/LF
        domain = self.sender.rpartition("@")[2] or None
        msg["Message-ID"] = make_msgid(domain=domain)
        for attempt in range(1, ATTEMPTS + 1):
            try:
                await asyncio.to_thread(self._deliver, msg)
            except smtplib.SMTPAuthenticationError:
                raise CredentialsRejected(
                    f"SMTP login refused by {self._host} (check SMTP_USERNAME / app password)"
                ) from None
            except (smtplib.SMTPException, OSError) as exc:
                if attempt < ATTEMPTS and _transient(exc):
                    await asyncio.sleep(self._retry_delay * attempt)
                    continue
                raise EmailError(f"SMTP send failed: {type(exc).__name__}") from None
            break
        message_id = str(msg["Message-ID"])
        log.info("email.sent smtp id=%s template_subject=%r", message_id, email.subject)
        return message_id

    def _deliver(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()  # verifies the server certificate + hostname
        if self._ssl:
            server = self._factory(self._host, self._port, timeout=TIMEOUT_S, context=context)
        else:
            server = self._factory(self._host, self._port, timeout=TIMEOUT_S)
        with server:
            if not self._ssl:
                server.starttls(context=context)
            server.login(self._username, self._password)
            server.send_message(msg)
