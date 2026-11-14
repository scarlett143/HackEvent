"""Shared candidate shape for structured source adapters (TRD §4.1, doc 08 §A3).

Adapters emit the same fields the LLM extractor does, so dedupe/upsert is shared.
Every text field is reduced to plain text and length-capped *by the model*, so no
adapter can hand scraped HTML downstream. URLs that are not http(s) are dropped.
"""

import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, field_validator


class ParseError(Exception):
    """The body is not what the adapter expects (malformed, unsafe, wrong shape)."""


class UnknownAdapter(KeyError):
    """No adapter registered under that name."""


_BLOCK_TAGS = frozenset(
    {"br", "p", "div", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
)
_SKIP_TAGS = frozenset({"script", "style", "template", "noscript"})


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def to_text(value: str) -> str:
    """HTML (or plain text) -> plain text: tags dropped, entities decoded, whitespace tidy."""
    parser = _TextOnly()
    parser.feed(value)
    parser.close()
    lines = (re.sub(r"[^\S\n]+", " ", line).strip() for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def _text(limit: int) -> BeforeValidator:
    def clean(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = to_text(value)[:limit].strip()
        return text or None

    return BeforeValidator(clean)


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 2048:
        return None
    return url


def _require_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware")
    return value.astimezone(UTC)


Mode = Literal["online", "offline", "hybrid", "unknown"]
UTCDatetime = Annotated[datetime | None, AfterValidator(_require_utc)]


class EventCandidate(BaseModel):
    """One event as found on a source; mirrors doc 08 §A3 plus description/extractor."""

    title: Annotated[str, _text(200), Field(min_length=1, max_length=200)]
    organiser: Annotated[str, _text(200), Field(min_length=1, max_length=200)]
    registration_url: Annotated[str | None, BeforeValidator(safe_url)] = None
    registration_deadline: UTCDatetime = None
    event_start: UTCDatetime = None
    event_end: UTCDatetime = None
    timezone_stated: Annotated[str | None, _text(64)] = None
    mode: Mode = "unknown"
    location: Annotated[str | None, _text(200)] = None
    eligibility: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=10
    )
    prize: Annotated[str | None, _text(200)] = None
    description: Annotated[str | None, _text(4000)] = None
    evidence: Annotated[str, _text(600), Field(max_length=600)] = ""
    confidence: float = Field(ge=0, le=1)
    extractor: str  # event_sources.extractor: "rss" / "devpost" / "unstop"

    @field_validator("eligibility", mode="before")
    @classmethod
    def _dedupe_chips(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        seen: dict[str, None] = {}
        for item in value:
            if isinstance(item, str) and (chip := to_text(item)[:40].strip()):
                seen.setdefault(chip, None)
        return list(seen)[:10]


def parse_offset_datetime(value: Any) -> datetime | None:
    """ISO 8601 with an explicit offset -> UTC. No offset = ambiguous = None (never guess)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)
