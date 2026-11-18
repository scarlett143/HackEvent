"""RSS 2.0 / RSS 1.0 / Atom adapter (HE-023).

XXE / billion-laughs safe without defusedxml: any DOCTYPE, entity declaration or external
entity reference is rejected by a strict expat pre-pass, so the ElementTree parse that
follows can only ever see the five predefined XML entities.
RSS gives announcements, not schedules: pubDate is publication time, so no event dates or
deadline are set here (the LLM path enriches those from the linked page).
Feeds are mostly not hackathons (blogs, Substack newsletters: HE-029), so an item whose
title + description has no doc 08 A7 keyword is dropped, as the LLM pre-filter would.
"""

import logging
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from xml.parsers import expat

from pydantic import ValidationError

from workers.keywords import has_keyword
from workers.scrape.adapters.base import EventCandidate, ParseError, to_text

log = logging.getLogger(__name__)

CONFIDENCE = 0.5  # a feed item is not known to be a hackathon until the page is read


def _forbid(*_: object) -> None:
    raise ParseError("DTDs and entity declarations are not allowed in feeds")


def safe_xml(body: bytes) -> ET.Element:
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _forbid
    guard.EntityDeclHandler = _forbid
    guard.UnparsedEntityDeclHandler = _forbid
    guard.ExternalEntityRefHandler = _forbid  # type: ignore[assignment]
    try:
        guard.Parse(body, True)
        return ET.fromstring(body)
    except (expat.ExpatError, ET.ParseError) as exc:
        raise ParseError(f"malformed XML: {exc}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, *names: str) -> ET.Element | None:
    for name in names:
        for child in node:
            if _local(child.tag) == name:
                return child
    return None


def _text(node: ET.Element, *names: str) -> str | None:
    for name in names:
        child = _child(node, name)
        if child is not None and (value := "".join(child.itertext()).strip()):
            return value
    return None


def _link(item: ET.Element) -> str | None:
    for child in item:
        if _local(child.tag) != "link":
            continue
        href = child.get("href")  # Atom
        if href and child.get("rel", "alternate") == "alternate":
            return href
        if not href and child.text and child.text.strip():  # RSS
            return child.text.strip()
    return None


def parse_rss(body: bytes, url: str) -> list[EventCandidate]:
    root = safe_xml(body)
    if _local(root.tag) not in ("rss", "RDF", "feed"):
        raise ParseError(f"not a feed: root element <{_local(root.tag)}>")
    channel = _child(root, "channel") if _local(root.tag) != "feed" else root
    organiser = (_text(channel, "title") if channel is not None else None) or "Unknown"
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out: list[EventCandidate] = []
    for item in items:
        title = _text(item, "title")
        description = _text(item, "description", "summary", "encoded", "content")
        if not has_keyword(to_text(f"{title or ''} {description or ''}")):
            continue
        link = _link(item)
        try:
            out.append(
                EventCandidate(
                    title=title or "",
                    organiser=organiser,
                    registration_url=urljoin(url, link) if link else None,
                    description=description,
                    evidence=title or "",
                    confidence=CONFIDENCE,
                    extractor="rss",
                )
            )
        except ValidationError as exc:
            log.warning("rss item skipped: %s", exc.errors(include_url=False)[0]["msg"])
    return out
