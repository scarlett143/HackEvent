"""Structured source adapters: (body, final_url) -> EventCandidate list (TRD §4.1).

`sources.adapter` (doc 05) is `rss | api | selector | llm`. RSS sources dispatch to "rss";
`api` sources name their parser in `sources.selector_config["parser"]` (e.g. "devpost").
"""

from collections.abc import Callable

from pydantic import ValidationError

from workers.scrape.adapters.base import EventCandidate, ParseError, UnknownAdapter
from workers.scrape.adapters.devpost import parse_devpost
from workers.scrape.adapters.rss import parse_rss

Adapter = Callable[[bytes, str], list[EventCandidate]]

ADAPTERS: dict[str, Adapter] = {
    "rss": parse_rss,
    "devpost": parse_devpost,
}


def parse(adapter: str, body: bytes, url: str) -> list[EventCandidate]:
    """Run a named adapter. Raises UnknownAdapter or ParseError, nothing else."""
    try:
        fn = ADAPTERS[adapter]
    except KeyError:
        raise UnknownAdapter(adapter) from None
    try:
        return fn(body, url)
    except ParseError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as exc:
        raise ParseError(f"{adapter}: {type(exc).__name__}: {exc}") from exc


__all__ = ["ADAPTERS", "Adapter", "EventCandidate", "ParseError", "UnknownAdapter", "parse"]
