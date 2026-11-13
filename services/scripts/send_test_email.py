"""Manual acceptance test for HE-030: send the test template to one address.

    docker compose exec api python -m scripts.send_test_email --to <address>

Exit 0 and a Gmail message id = accepted by Gmail; then check the recipient's Inbox.
"""

import argparse
import asyncio

from common.email import EmailError, GmailSender, render
from common.logging import configure_logging


async def _send(to: str) -> str:
    sender = GmailSender.from_settings()
    try:
        return await sender.send(to, render("test"))
    finally:
        await sender.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the HackEvents test email.")
    parser.add_argument("--to", required=True, help="recipient address")
    args = parser.parse_args()
    configure_logging()
    try:
        message_id = asyncio.run(_send(args.to))
    except EmailError as exc:
        raise SystemExit(f"FAILED: {exc}") from None
    print(f"Sent. Gmail message id: {message_id}. Now check the Inbox (not Spam) of {args.to}.")


if __name__ == "__main__":
    main()
