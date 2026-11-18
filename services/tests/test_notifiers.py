"""HE-073: notifier plugin contract (doc 06 §B3) and the in-app inbox API."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.stack import Pair, needs_stack, sql

from common import notifiers
from common.email import EmailError, RenderedEmail, TokenRevoked
from common.notifiers import Endpoint, Message, SendResult

MSG = Message(
    headline="2 days left to register",
    title="Acme CodeSprint",
    when_label="Registration closes",
    when="5 Dec 2026, 11:59 PM (Asia/Kolkata)",
    path="/events/e1",
)


class FakeGmail:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, RenderedEmail]] = []

    async def send(self, to: str, email: RenderedEmail) -> str:
        if self.error:
            raise self.error
        self.sent.append((to, email))
        return "id"


class FakeConn:
    """Stands in for AsyncConnection: records statements."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, stmt: Any, params: dict[str, Any]) -> None:
        self.calls.append(params)


def _plugin(channel: str, gmail: FakeGmail | None = None) -> Any:
    ctx: dict[str, Any] = {"gmail": gmail or FakeGmail()}
    return notifiers.build(channel, ctx, FakeConn())  # type: ignore[arg-type]


# --- contract: every registered plugin ----------------------------------------------------


@pytest.mark.parametrize("channel", notifiers.CHANNELS)
async def test_plugin_contract(channel: str) -> None:
    plugin = _plugin(channel)
    assert plugin is not None and plugin.channel == channel
    user = Endpoint(uuid.uuid4(), "prof@example.org")
    for result in (await plugin.send(user, MSG), await plugin.test(user)):
        assert isinstance(result, SendResult) and result.ok and result.error is None


@pytest.mark.parametrize(
    ("error", "permanent"),
    [(EmailError("Gmail send failed: HTTP 503"), False), (TokenRevoked("invalid_grant"), True)],
)
async def test_email_failures_are_results_not_exceptions(error: Exception, permanent: bool) -> None:
    plugin = _plugin("email", FakeGmail(error))
    result = await plugin.send(Endpoint(uuid.uuid4(), "prof@example.org"), MSG)
    assert result.ok is False and result.permanent_failure is permanent
    assert result.error and "prof@example.org" not in result.error  # no address in errors


async def test_email_without_address_is_permanent() -> None:
    result = await _plugin("email").send(Endpoint(uuid.uuid4(), None), MSG)
    assert result == SendResult(ok=False, permanent_failure=True, error="no email address")


async def test_email_renders_the_reminder_template() -> None:
    gmail = FakeGmail()
    await _plugin("email", gmail).send(Endpoint(uuid.uuid4(), "prof@example.org"), MSG)
    ((to, email),) = gmail.sent
    assert to == "prof@example.org"
    assert email.subject == "2 days left to register: Acme CodeSprint"
    assert "/events/e1" in email.text


async def test_in_app_row_shape() -> None:
    conn = FakeConn()
    uid, eid = uuid.uuid4(), uuid.uuid4()
    plugin = notifiers.InAppNotifier(conn)  # type: ignore[arg-type]
    await plugin.send(Endpoint(uid), Message(**{**MSG.__dict__, "event_id": eid}))
    assert conn.calls == [
        {
            "u": uid,
            "ty": "reminder",
            "t": "2 days left to register: Acme CodeSprint",
            "b": "Registration closes: 5 Dec 2026, 11:59 PM (Asia/Kolkata)",
            "e": eid,
        }
    ]


@pytest.mark.parametrize("channel", ["fcm", "telegram", "ntfy", "gcal"])
def test_channels_without_a_plugin_build_none(channel: str) -> None:
    assert _plugin(channel) is None


# --- inbox API ------------------------------------------------------------------------------


def _note(user: str, title: str, read: bool = False) -> str:
    return str(
        sql(
            "INSERT INTO notifications (user_id, type, title, body, read_at)"
            " VALUES (CAST(:u AS uuid), 'reminder', :t, 'b', CASE WHEN :r THEN now() END)"
            " RETURNING id::text",
            u=user,
            t=title,
            r=read,
        )[0][0]
    )


@pytest.fixture
def clean(stack_client: TestClient) -> TestClient:
    sql("TRUNCATE notifications")
    return stack_client


URL = "/api/v1/notifications"


@needs_stack
def test_list_is_own_rows_newest_first_with_unread_count(
    clean: TestClient, two_users: Pair
) -> None:
    a, b = two_users
    _note(a.id, "first")
    _note(a.id, "second", read=True)
    _note(b.id, "B's")
    r = clean.get(URL, headers=a.headers)
    assert r.status_code == 200
    body = r.json()
    assert [n["title"] for n in body["items"]] == ["second", "first"]
    assert body["unread"] == 1
    assert "B's" not in r.text


@needs_stack
def test_mark_read_ids_and_all_own_rows_only(clean: TestClient, two_users: Pair) -> None:
    a, b = two_users
    mine = _note(a.id, "mine")
    other = _note(a.id, "other")
    theirs = _note(b.id, "theirs")
    r = clean.post(f"{URL}/read", json={"ids": [mine, theirs]}, headers=a.headers)
    assert r.status_code == 200 and r.json() == {"updated": 1}  # B's id silently ignored
    assert sql(
        "SELECT read_at IS NULL FROM notifications WHERE id = CAST(:i AS uuid)", i=theirs
    ) == [(True,)]
    unread = clean.get("/api/v1/me/unread-count", headers=a.headers).json()["notifications"]
    assert unread == 1
    assert clean.post(f"{URL}/read", json={"all": True}, headers=a.headers).json() == {"updated": 1}
    assert clean.get("/api/v1/me/unread-count", headers=a.headers).json()["notifications"] == 0
    assert sql(
        "SELECT read_at IS NULL FROM notifications WHERE id = CAST(:i AS uuid)", i=other
    ) == [(False,)]
    assert clean.get("/api/v1/me/unread-count", headers=b.headers).json()["notifications"] == 1


@needs_stack
@pytest.mark.parametrize(
    "body", [{}, {"ids": [], "all": False}, {"ids": [str(uuid.uuid4())], "all": True}]
)
def test_mark_read_needs_exactly_one_of_ids_or_all(
    clean: TestClient, two_users: Pair, body: dict[str, Any]
) -> None:
    a, _ = two_users
    assert clean.post(f"{URL}/read", json=body, headers=a.headers).status_code == 422


@needs_stack
def test_inbox_requires_sign_in(clean: TestClient) -> None:
    assert clean.get(URL).status_code == 401
