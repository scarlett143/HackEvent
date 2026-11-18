"""Outbound email (HE-030). Becomes a notifier plugin once HE-073 defines the interface."""

from common.config import get_settings
from common.email.gmail import EmailError, GmailSender, TokenRevoked
from common.email.smtp import CredentialsRejected, SmtpSender
from common.email.templates import RenderedEmail, invite, render

EmailSender = GmailSender | SmtpSender


def sender_from_settings() -> EmailSender:
    """SMTP when SMTP_HOST is set, otherwise the Gmail API. Raises EmailError if the chosen
    one is not fully configured."""
    if get_settings().smtp_host:
        return SmtpSender.from_settings()
    return GmailSender.from_settings()


__all__ = [
    "CredentialsRejected",
    "EmailError",
    "EmailSender",
    "GmailSender",
    "RenderedEmail",
    "SmtpSender",
    "TokenRevoked",
    "invite",
    "render",
    "sender_from_settings",
]
