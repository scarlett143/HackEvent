"""One-off: obtain the Gmail sender's refresh token (runbook B7 step 3).

    docker compose exec api python -m scripts.gmail_authorize

Loopback flow with PKCE, no local server: Google redirects the browser to
http://localhost/?code=..., which fails to load; the operator pastes that address
back here. Works from a headless server. Needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET;
if that OAuth client is a "Web application", add `http://localhost` to its
authorised redirect URIs first. Writes no files; the token is printed once for the
operator to paste into infra/.env.
"""

import argparse
import base64
import hashlib
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

from common.config import get_settings
from common.email.gmail import TIMEOUT, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/gmail.send"


def auth_url(*, client_id: str, redirect_uri: str, state: str, verifier: str, hint: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # ... even if consent was given before
        "state": state,
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
    }
    if hint:
        params["login_hint"] = hint
    return f"{AUTH_URL}?{urlencode(params)}"


def code_from_redirect(pasted: str, state: str) -> str:
    query = parse_qs(urlsplit(pasted.strip()).query)
    if query.get("state") != [state]:
        raise SystemExit("State mismatch: paste the address from this run's sign-in.")
    if "error" in query:
        raise SystemExit(f"Google refused: {query['error'][0]}")
    if "code" not in query:
        raise SystemExit("No ?code= in the pasted address.")
    return query["code"][0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Get the Gmail sender refresh token.")
    parser.add_argument("--redirect-uri", default="http://localhost")
    args = parser.parse_args()

    s = get_settings()
    secret = s.google_client_secret.get_secret_value()
    if not (s.google_client_id and secret):
        raise SystemExit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in infra/.env first.")

    state, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(64)
    print("1. Open this URL and sign in as the sender account", s.gmail_sender or "(GMAIL_SENDER)")
    url = auth_url(
        client_id=s.google_client_id,
        redirect_uri=args.redirect_uri,
        state=state,
        verifier=verifier,
        hint=s.gmail_sender,
    )
    print(url)
    print("2. Allow 'Send email on your behalf'. The browser then fails to open a localhost page;")
    print("   that is expected.")
    code = code_from_redirect(input("3. Paste the full address from the browser bar: "), state)

    resp = httpx2.post(
        TOKEN_URL,
        timeout=TIMEOUT,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": s.google_client_id,
            "client_secret": secret,
            "redirect_uri": args.redirect_uri,
            "code_verifier": verifier,
        },
    )
    body = resp.json()
    if resp.status_code != 200 or "refresh_token" not in body:
        raise SystemExit(f"Token exchange failed: HTTP {resp.status_code} {body.get('error', '')}")

    print("\n4. Put this line in infra/.env (it is a secret: do not paste it anywhere else),")
    print("   then restart: docker compose up -d api worker-notify\n")
    sys.stdout.write(f"GMAIL_OAUTH_REFRESH_TOKEN={body['refresh_token']}\n")


if __name__ == "__main__":
    main()
