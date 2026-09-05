"""System state inspection — deterministic, testable, no LLM.

read_state() takes `now` as a parameter (never calls datetime.now itself),
so the whole thing is reproducible in tests. All reads go through the
storage module (rule 2) and are cheap: counts and MAX(timestamp) only,
never full table loads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from edgedash.config import Config
from edgedash.storage import (
    count_unscored,
    gaps_computed_at,
    last_cycle,
    last_fetch_time,
    last_score_time,
)


@dataclass
class SystemState:
    now: datetime

    last_fetch_at: datetime | None
    hours_since_fetch: float | None   # None if never fetched

    unscored_count: int

    gaps_computed_at: datetime | None
    gaps_stale: bool                  # True if a score is newer than the snapshot,
                                      # or if gaps were never computed

    last_cycle_verdict: str | None    # "pass" | "fail" | None
    last_cycle_at: datetime | None


def read_state(config: Config, now: datetime) -> SystemState:
    """Read a cheap snapshot of system state as of `now`."""
    db = config.abs_db_path

    fetch_at = last_fetch_time(db)
    hours_since = _hours_between(fetch_at, now)

    unscored = count_unscored(db)

    gaps_at = gaps_computed_at(db)
    last_score = last_score_time(db)
    stale = _gaps_are_stale(gaps_at, last_score)

    cycle = last_cycle(db)
    verdict = cycle[0] if cycle else None
    cycle_at = cycle[1] if cycle else None

    return SystemState(
        now=now,
        last_fetch_at=fetch_at,
        hours_since_fetch=hours_since,
        unscored_count=unscored,
        gaps_computed_at=gaps_at,
        gaps_stale=stale,
        last_cycle_verdict=verdict,
        last_cycle_at=cycle_at,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _hours_between(then: datetime | None, now: datetime) -> float | None:
    if then is None:
        return None
    return (now - then).total_seconds() / 3600.0


def _gaps_are_stale(
    gaps_at: datetime | None,
    last_score: datetime | None,
) -> bool:
    """Gaps are stale when they don't reflect the latest scoring.

    - Never computed → stale (there is analysis to do once scores exist).
    - No scores yet → not stale (nothing to analyse).
    - Latest score newer than the snapshot → stale.
    """
    if gaps_at is None:
        return True
    if last_score is None:
        return False
    return last_score > gaps_at
