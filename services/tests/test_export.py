"""HE-123 GET /me/export: the caller's own data only, as a JSON download, no secrets."""

import json
import uuid

from fastapi.testclient import TestClient
from tests.stack import Pair, needs_stack, sql

URL = "/api/v1/me/export"


@needs_stack
def test_export_is_own_data_only_without_secrets(stack_client: TestClient, two_users: Pair) -> None:
    a, b = two_users
    (ev,) = sql(
        "INSERT INTO events (title, organiser_name, description, registration_url, mode, status,"
        " review_status, confidence, dedupe_key) VALUES ('Hack', 'Org', '', 'https://example.com/r',"
        " 'online', 'open', 'auto', 0.9, :k) RETURNING id::text",
        k=uuid.uuid4().hex,
    )[0]
    for who in (a, b):
        stack_client.post(f"/api/v1/events/{ev}/registration", headers=who.headers)
        sql(
            "INSERT INTO notifications (user_id, type, title, body)"
            " VALUES (CAST(:u AS uuid), 't', :t, 'b')",
            u=who.id, t=f"note for {who.email}",
        )  # fmt: skip
        sql(
            "INSERT INTO channel_endpoints (user_id, channel, address_enc, label, is_valid)"
            " VALUES (CAST(:u AS uuid), 'email', '\\x00ff'::bytea, 'Mail', true)",
            u=who.id,
        )
    sql("UPDATE users SET totp_secret_enc = '\\x01'::bytea WHERE id = CAST(:u AS uuid)", u=a.id)

    r = stack_client.get(URL, headers=a.headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].startswith('attachment; filename="hackevents-export-')
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["user"]["email"] == a.email
    assert [n["title"] for n in data["notifications"]] == [f"note for {a.email}"]
    assert [x["event_id"] for x in data["registrations"]] == [ev]
    assert data["events"] == {ev: "Hack"}
    assert len(data["sessions"]) >= 1 and len(data["channel_endpoints"]) == 1
    text = json.dumps(data)
    assert b.email not in text and b.id not in text  # nothing of B's
    for secret in ("password_hash", "token_hash", "totp_secret_enc", "address_enc", "meta"):
        assert f'"{secret}"' not in text, secret

    assert stack_client.get(URL).status_code == 401
