"""doc 08 A7 hackathon keyword list, shared by the LLM pre-filter and the feed adapters.

Its own module so the structured adapters can use it without importing the extraction
package (whose schema imports the adapters).
"""

# ponytail: constant; move to a DB setting when admins need to edit it (A7 "list editable").
KEYWORDS = (
    "hackathon",
    "hack",
    "ideathon",
    "codeathon",
    "challenge",
    "coding contest",
    "buildathon",
)


def has_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in KEYWORDS)
