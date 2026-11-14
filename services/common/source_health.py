"""Source health (HE-031): green / amber / red with a human reason, derived from counters.

Pure and DB-free so the scheduler (alerts) and the Admin source-health dashboard (HE-092)
compute the same status from the same `sources` columns.

Definitions:
- error run: a scrape whose outcome is `error` (fetch failed, SSRF-blocked, robots
  disallowed, HTTP >= 400, retry limit). Tracked by `sources.error_streak`; any
  changed/unchanged run resets it.
- empty run: a run whose content produced zero event candidates, reported by the adapter
  or extraction path via `workers.scheduler.record_candidates(source_id, 0)`. Tracked by
  `sources.empty_streak`; a run with >= 1 candidate resets it. An `unchanged` run (same
  hash / 304) is neither empty nor non-empty: identical content is no evidence of breakage.
"""

import enum
from typing import NamedTuple

ERROR_RED = 3  # runbook Part F: "5 empty or 3 error runs"
EMPTY_RED = 5


class HealthStatus(enum.StrEnum):
    GREEN = "green"
    AMBER = "amber"  # some consecutive errors / empty runs, below the red threshold
    RED = "red"  # >= ERROR_RED consecutive errors or >= EMPTY_RED consecutive empty runs


class SourceHealth(NamedTuple):
    status: HealthStatus
    reason: str


def source_health(error_streak: int, empty_streak: int, last_error: str | None) -> SourceHealth:
    reasons = []
    if error_streak:
        last = f"; last error: {last_error}" if last_error else ""
        reasons.append(f"{error_streak} failed run(s) in a row (red at {ERROR_RED}){last}")
    if empty_streak:
        reasons.append(f"{empty_streak} run(s) in a row with no events (red at {EMPTY_RED})")
    if error_streak >= ERROR_RED or empty_streak >= EMPTY_RED:
        return SourceHealth(HealthStatus.RED, "; ".join(reasons))
    if reasons:
        return SourceHealth(HealthStatus.AMBER, "; ".join(reasons))
    return SourceHealth(HealthStatus.GREEN, "OK")
