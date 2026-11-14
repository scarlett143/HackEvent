"""Doc 08 A3 output schema (v1) as data, plus the post-validated candidate shape.

`SCHEMA` is sent to the provider and enforced in our code by `common.llm.base.validate`
inside `parse_output` (A8: never trust the provider's own enforcement). `EventCandidate`
is the shared candidate shape from the structured adapters (HE-023/024), so HE-047
dedupe has one input; `ExtractedCandidate` adds what post-validation decides
(status, needs_review + reason codes, the timezone applied to naive datetimes).

AGENTS.md: every prompt or schema change bumps SCHEMA_VERSION and re-runs the golden set
for each provider (golden set: HE-048).
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from workers.scrape.adapters.base import EventCandidate, Mode

SCHEMA_VERSION = 1

TITLE_MAX = 200
ORGANISER_MAX = 200
LOCATION_MAX = 200
PRIZE_MAX = 200
EVIDENCE_MAX = 600
ELIGIBILITY_ITEM_MAX = 40
ELIGIBILITY_MAX_ITEMS = 10
EVENTS_MAX = 10
MODES: tuple[Mode, ...] = ("online", "offline", "hybrid", "unknown")

_NULLABLE_STR = {"type": ["string", "null"]}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["is_hackathon", "events"],
    "properties": {
        "is_hackathon": {"type": "boolean"},
        "events": {
            "type": "array",
            "maxItems": EVENTS_MAX,
            "items": {
                "type": "object",
                "required": ["title", "organiser", "evidence", "confidence"],
                "properties": {
                    "title": {"type": "string", "maxLength": TITLE_MAX},
                    "organiser": {"type": "string", "maxLength": ORGANISER_MAX},
                    "registration_url": _NULLABLE_STR,
                    "registration_deadline": {
                        "type": ["string", "null"],
                        "description": "ISO 8601; include offset only if stated",
                    },
                    "event_start": _NULLABLE_STR,
                    "event_end": _NULLABLE_STR,
                    "timezone_stated": _NULLABLE_STR,
                    "mode": {"enum": list(MODES)},
                    "location": {"type": ["string", "null"], "maxLength": LOCATION_MAX},
                    "eligibility": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": ELIGIBILITY_ITEM_MAX},
                        "maxItems": ELIGIBILITY_MAX_ITEMS,
                    },
                    "prize": {"type": ["string", "null"], "maxLength": PRIZE_MAX},
                    "evidence": {
                        "type": "string",
                        "maxLength": EVIDENCE_MAX,
                        "description": "Verbatim quote from the page containing the dates",
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    },
}


class ExtractedCandidate(EventCandidate):
    """A post-validated LLM candidate. `extractor` is `<provider>:<model>` (doc 05)."""

    timezone_applied: str | None = None  # IANA name / offset used for naive datetimes
    status: Literal["open", "closed"] = "open"
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """What `extract()` returns; HE-047 dedupe consumes `events`."""

    schema_version: int = SCHEMA_VERSION
    is_hackathon: bool
    events: list[ExtractedCandidate]
