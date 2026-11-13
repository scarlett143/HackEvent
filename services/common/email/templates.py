"""Email templates (HE-030): each template has a .txt and an .html version.

HTML is autoescaped, so user-supplied values (names, titles) can't inject markup.
The .txt part is plain text and is deliberately not HTML-escaped.
Links are built only from APP_DOMAIN via `app_url()` (AGENTS.md).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from common.config import get_settings

SUBJECTS = {
    "invite": "Your HackEvents invite",
    "test": "HackEvents test email",
}
# Invite links expire after 72 h (Security doc §4).
DEFAULTS: dict[str, dict[str, Any]] = {"invite": {"expires_hours": 72}}

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


def render(name: str, *, app_domain: str | None = None, **context: Any) -> RenderedEmail:
    """Render template `name` (a key of SUBJECTS). Unknown variables raise."""
    domain = app_domain or get_settings().app_domain
    subject = SUBJECTS[name]

    def app_url(path: str) -> str:
        return f"https://{domain}{quote(path)}"

    ctx = {"app_url": app_url, "app_domain": domain, "subject": subject}
    ctx |= DEFAULTS.get(name, {}) | context
    return RenderedEmail(
        subject=subject,
        text=_env.get_template(f"{name}.txt").render(ctx),
        html=_env.get_template(f"{name}.html").render(ctx),
    )


def invite(*, token: str, inviter: str, app_domain: str | None = None) -> RenderedEmail:
    """Invite email for link `/invite/<token>`."""
    return render("invite", app_domain=app_domain, token=token, inviter=inviter)
