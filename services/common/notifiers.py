"""Notifier plugins (HE-073, doc 06 §B3): one interface, one class per channel.

    class Notifier(Protocol):
        channel: str
        async def send(self, endpoint, msg) -> SendResult
        async def test(self, endpoint) -> SendResult

Contract (tests/test_notifiers.py runs it against every registered plugin):
- `send` / `test` never raise for delivery problems: they return SendResult(ok=False,
  error=...). `permanent_failure=True` means retrying cannot help (revoked token, FCM
  UNREGISTERED, Telegram "bot blocked"): the caller stops retrying and, for endpoint
  channels, marks the endpoint invalid;
- errors never contain message bodies, addresses or credentials.

`Endpoint` is who to reach: the user (in-app), an address (email), later a decrypted
`channel_endpoints` row (FCM token, Telegram chat id). Plugins for fcm / telegram / ntfy /
gcal arrive with HE-074 / 075 / 076 / 064; `build()` returns None until then.
"""

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from common.email import EmailError, GmailSender, TokenRevoked, render


@dataclass(frozen=True)
class Endpoint:
    user_id: uuid.UUID
    address: str | None = None  # email address / chat id / token, by channel


@dataclass(frozen=True)
class Message:
    """Doc 06 B3 template: headline, title, when line, deep link."""

    headline: str
    title: str
    when_label: str
    when: str
    path: str  # app path, e.g. /events/<id>; links are built from APP_DOMAIN
    event_id: uuid.UUID | None = None
    kind: str = "reminder"  # notifications.type


@dataclass(frozen=True)
class SendResult:
    ok: bool
    permanent_failure: bool = False
    error: str | None = None


class Notifier(Protocol):
    channel: str

    async def send(self, endpoint: Endpoint, msg: Message) -> SendResult: ...

    async def test(self, endpoint: Endpoint) -> SendResult: ...


TEST_MESSAGE = Message(
    headline="Test notification",
    title="In-app notifications work.",
    when_label="",
    when="",
    path="/notifications",
    kind="test",
)


class InAppNotifier:
    """Writes a `notifications` row on the caller's connection (same transaction as the
    reminder claim, so the row and the 'sent' status commit or roll back together)."""

    channel = "in_app"

    def __init__(self, conn: AsyncConnection) -> None:
        self.conn = conn

    async def send(self, endpoint: Endpoint, msg: Message) -> SendResult:
        title = f"{msg.headline}: {msg.title}" if msg.when else msg.headline
        body = f"{msg.when_label}: {msg.when}" if msg.when else msg.title
        await self.conn.execute(
            text(
                "INSERT INTO notifications (user_id, type, title, body, event_id)"
                " VALUES (:u, :ty, :t, :b, :e)"
            ),
            {"u": endpoint.user_id, "ty": msg.kind, "t": title, "b": body, "e": msg.event_id},
        )
        return SendResult(ok=True)

    async def test(self, endpoint: Endpoint) -> SendResult:
        return await self.send(endpoint, TEST_MESSAGE)


class EmailNotifier:
    """Gmail API (HE-030). `endpoint.address` is the user's email."""

    channel = "email"

    def __init__(self, gmail: GmailSender) -> None:
        self.gmail = gmail

    async def _send(self, endpoint: Endpoint, template: str, ctx: dict[str, Any]) -> SendResult:
        if not endpoint.address:
            return SendResult(ok=False, permanent_failure=True, error="no email address")
        try:
            await self.gmail.send(endpoint.address, render(template, **ctx))
        except TokenRevoked as exc:
            return SendResult(ok=False, permanent_failure=True, error=f"TokenRevoked: {exc}")
        except (EmailError, ValueError) as exc:  # ValueError: header injection guard
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        return SendResult(ok=True)

    async def send(self, endpoint: Endpoint, msg: Message) -> SendResult:
        return await self._send(
            endpoint,
            "reminder",
            {
                "headline": msg.headline,
                "title": msg.title,
                "when_label": msg.when_label,
                "when": msg.when,
                "path": msg.path,
            },
        )

    async def test(self, endpoint: Endpoint) -> SendResult:
        return await self._send(endpoint, "test", {})


CHANNELS = ("in_app", "email")  # channels with a plugin today


def build(channel: str, ctx: dict[str, Any], conn: AsyncConnection) -> Notifier | None:
    """The plugin for `channel`, or None if that channel has no sender yet.
    `ctx` is the worker context (keeps one GmailSender, so its token cache survives)."""
    if channel == "in_app":
        return InAppNotifier(conn)
    if channel == "email":
        if "gmail" not in ctx:
            ctx["gmail"] = GmailSender.from_settings()
        return EmailNotifier(ctx["gmail"])
    return None
