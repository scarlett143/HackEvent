"""Log redaction (Security doc §6: "Never log secrets").

`RedactingFilter` rewrites a LogRecord in place so secrets never reach a handler:
the message (template and %-args), non-standard `extra` attributes, exception text
and stack info. Matches are replaced with `[REDACTED]`; surrounding text is kept.

`configure_logging()` installs the filter on every handler that exists when it is
called (root, uvicorn, arq ...). Handler filters see records propagated from child
loggers, logger filters do not, which is why the filter goes on handlers. Call it
*after* the server/worker framework has run its own `dictConfig`.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

REDACTED = "[REDACTED]"

_NOT_REDACTED = r"(?!\[REDACTED\])"

# Key names whose value is always secret. The key must END with one of these words,
# so `token_bucket=`, `max_tokens=` or `access_token_expires_at=` are left alone.
# A bare `key` only counts as its own word or suffix (`key=`, `MASTER_KEY=`, not `monkey=`).
_SECRET_KEY = (
    r"(?:[A-Za-z0-9_.-]*?(?:"
    r"apikey|secret|password|passwd|pwd|token|authorization|cookie|credentials?|"
    r"session[_-]?id|dsn"
    r")|(?:[A-Za-z0-9_.-]*[_.-])?key)"
)
_SECRET_KEY_RE = re.compile(rf"{_SECRET_KEY}", re.IGNORECASE)

# scheme://user:PASS@host (DATABASE_URL, REDIS_URL, any DSN); user may be empty.
_URL_PASSWORD = re.compile(
    r"(?P<pre>\b[a-z][a-z0-9+.-]*://[^:/\s@]*:)[^\s@/]+(?P<post>@)", re.IGNORECASE
)
_HEADER_LINE = re.compile(
    r"(?P<pre>\b(?:proxy-authorization|authorization|set-cookie|cookie|x-api-key|x-goog-api-key)"
    r"\s*:\s*+)" + _NOT_REDACTED + r"[^\r\n]+",
    re.IGNORECASE,
)
# Case-sensitive and >= 8 chars so prose ("bearer of news") is left alone.
_AUTH_SCHEME = re.compile(r"(?P<pre>\bBearer\s++)" + _NOT_REDACTED + r"[A-Za-z0-9._~+/=-]{8,}")
# "password": "x"  /  'api_key': 'x'  (JSON, dict repr)
_QUOTED_KV = re.compile(
    rf"(?P<pre>(?P<q>[\"']){_SECRET_KEY}(?P=q)\s*:\s*+)"
    rf"(?:(?P<vq>[\"'])(?!\[REDACTED\](?P=vq))[^\"']*(?P=vq)|{_NOT_REDACTED}[^\s,}}\]\"']+)",
    re.IGNORECASE,
)
# token=abc (query strings, kwargs, env style), password='x'
_PLAIN_KV = re.compile(
    rf"(?P<pre>(?<![A-Za-z0-9_.-]){_SECRET_KEY}\s*=\s*+)"
    rf"(?:(?P<vq>[\"'])(?!\[REDACTED\](?P=vq))[^\"']*(?P=vq)|{_NOT_REDACTED}[^\s&,;)\]}}\"']+)",
    re.IGNORECASE,
)
_SHAPES = re.compile(
    r"AIza[0-9A-Za-z_-]{35}"  # Google API key
    r"|\bsk-[A-Za-z0-9_-]{16,}"  # Anthropic sk-ant-..., OpenAI-style sk-...
    r"|\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"  # JWT
    r"|\bya29\.[0-9A-Za-z_-]+"  # Google OAuth access token
    r"|\b1//[0-9A-Za-z_-]{20,}"  # Google OAuth refresh token
    r"|(?<!\d)\d{5,}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"  # Telegram bot token
    r"|(?:[A-Za-z0-9_-]+:)?APA91[A-Za-z0-9_-]{20,}"  # FCM registration token
)


def _keep_prefix(m: re.Match[str]) -> str:
    """Replace the value but keep the key / quotes around it."""
    value = m.group(0)[len(m.group("pre")) :]
    q = m.groupdict().get("vq")
    body = f"{q}{REDACTED}{q}" if q and value.startswith(q) else REDACTED
    return m.group("pre") + body + (m.groupdict().get("post") or "")


_RULES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (_URL_PASSWORD, lambda m: m.group("pre") + REDACTED + m.group("post")),
    (_HEADER_LINE, lambda m: m.group("pre") + REDACTED),
    (_AUTH_SCHEME, lambda m: m.group("pre") + REDACTED),
    (_QUOTED_KV, _keep_prefix),
    (_PLAIN_KV, _keep_prefix),
    (_SHAPES, lambda m: REDACTED),
)


def redact(text: str) -> str:
    """Return `text` with every known secret pattern replaced by `[REDACTED]`."""
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_KEY_RE.fullmatch(k) else _redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
}
_DONE = "_he_redacted"


class RedactingFilter(logging.Filter):
    """Redacts secrets in place; never drops a record. Idempotent per record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _DONE, False):
            return True
        setattr(record, _DONE, True)

        # Args are redacted individually first so formatters that read record.args
        # (e.g. uvicorn's AccessFormatter) keep working.
        # The template itself is only checked via the formatted message: redacting it
        # alone could swallow a placeholder (e.g. "redis://:%s@host").
        if record.args:
            record.args = _redact_value(record.args)
        try:
            message = record.getMessage()
        except (TypeError, ValueError, KeyError):  # malformed %-format: logging reports it
            message = None
        if message is not None:
            clean = redact(message)
            if clean != message:  # secret spans template + arg: collapse to plain text
                record.msg, record.args = clean, None

        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_ATTRS or key == _DONE:
                continue
            record.__dict__[key] = (
                REDACTED if _SECRET_KEY_RE.fullmatch(key) else _redact_value(value)
            )

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def _install(handler: logging.Handler) -> None:
    if not any(isinstance(f, RedactingFilter) for f in handler.filters):
        handler.addFilter(RedactingFilter())


def configure_logging(level: int | str = logging.INFO) -> None:
    """Set the root level and install `RedactingFilter` on every existing handler.

    Adds a stderr handler to root if it has none (otherwise records would go to
    logging.lastResort, which cannot be filtered). Safe to call more than once.
    """
    root = logging.getLogger()
    if not root.handlers:
        stderr = logging.StreamHandler()
        stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(stderr)
    root.setLevel(level)
    loggers = [root] + [
        lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)
    ]
    for lg in loggers:
        for handler in lg.handlers:
            _install(handler)
