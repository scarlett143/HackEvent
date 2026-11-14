"""LLM extraction: page HTML -> post-validated hackathon candidates (doc 08 Part A)."""

from workers.extraction.pipeline import MAX_OUTPUT_TOKENS, extract
from workers.extraction.schema import SCHEMA, SCHEMA_VERSION, ExtractedCandidate, ExtractionResult

__all__ = [
    "MAX_OUTPUT_TOKENS",
    "SCHEMA",
    "SCHEMA_VERSION",
    "ExtractedCandidate",
    "ExtractionResult",
    "extract",
]
