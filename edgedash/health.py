"""Health reporting — read-only, deterministic, no new services.

`python -m edgedash.health` prints one line per check and exits non-zero if
the system is unhealthy, so a scheduler/CI step can turn "unhealthy" into a
failed job and a notification.

Checks:
  1. stale_listings   — newest listing older than max_data_age_days (3).
  2. no_recent_cycle  — no SUCCESSFUL (verified-pass) cycle in 48 hours.
  3. last_three_failed — the last 3 cycles all failed verification.
  4. db_unreachable   — the database could not be read at all.

Design mirrors state.py / verification.py: a PURE assess() takes the raw
values plus `now` and returns structured results (testable, no clock, no DB);
thin wrappers read through the storage module (rule 2) and call it. `now` is
always a parameter, never read inside the pure core.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Thresholds. max_data_age_days already exists in config for verification;
# we read it there so the two agree. The rest are health-specific constants.
_NO_CYCLE_HOURS = 48
_LAST_N_FAILED = 3
_DASHBOARD_FRESH_HOURS = 24   # dashboard "green" window (tighter than CI's 48)


@dataclass
class CheckResult:
    name: str
    healthy: bool
    observed: str          # human-readable observed value, for the one-liner
    message: str


@dataclass
class HealthReport:
    healthy: bool
    checks: list[CheckResult]

    def summary(self) -> str:
        good = sum(1 for c in self.checks if c.healthy)
        return f"{good}/{len(self.checks)} checks healthy"


# ---------------------------------------------------------------------------
# Pure assessment — no DB, no clock. Everything is passed in.
# ---------------------------------------------------------------------------

def assess(
    *,
    now: datetime,
    db_reachable: bool,
    newest_listing_at: datetime | None,
    last_pass_at: datetime | None,
    recent_verdicts: list[bool | None],   # newest first; True=pass False=fail None=n/a
    max_data_age_days: int,
) -> HealthReport:
    """Return a HealthReport from raw values. Deterministic and testable."""

    # If the DB is unreachable we cannot evaluate the data-driven checks
    # meaningfully; report that single failure clearly rather than guessing.
    if not db_reachable:
        return HealthReport(
            healthy=False,
            checks=[CheckResult(
                name="db_unreachable",
                healthy=False,
                observed="unreachable",
                message="db_unreachable: FAIL — the database could not be read",
            )],
        )

    checks: list[CheckResult] = [
        _check_stale_listings(now, newest_listing_at, max_data_age_days),
        _check_no_recent_cycle(now, last_pass_at),
        _check_last_three_failed(recent_verdicts),
        # db is reachable — record the positive so the line is always present.
        CheckResult("db_reachable", True, "reachable",
                    "db_reachable: OK — database read succeeded"),
    ]
    return HealthReport(healthy=all(c.healthy for c in checks), checks=checks)


def _age_hours(then: datetime | None, now: datetime) -> float | None:
    if then is None:
        return None
    return (now - then).total_seconds() / 3600.0


def _check_stale_listings(
    now: datetime, newest: datetime | None, max_days: int
) -> CheckResult:
    name = "stale_listings"
    if newest is None:
        return CheckResult(name, False, "no listings",
                           f"{name}: FAIL — no listings in the database")
    age_days = (now - newest).total_seconds() / 86400.0
    healthy = age_days <= max_days
    verdict = "OK" if healthy else "FAIL"
    return CheckResult(
        name, healthy, f"{age_days:.1f}d old",
        f"{name}: {verdict} — newest listing {age_days:.1f} day(s) old "
        f"(limit {max_days})",
    )


def _check_no_recent_cycle(now: datetime, last_pass_at: datetime | None) -> CheckResult:
    name = "no_recent_cycle"
    if last_pass_at is None:
        return CheckResult(name, False, "never",
                           f"{name}: FAIL — no successful cycle has ever passed")
    hrs = _age_hours(last_pass_at, now) or 0.0
    healthy = hrs <= _NO_CYCLE_HOURS
    verdict = "OK" if healthy else "FAIL"
    return CheckResult(
        name, healthy, f"{hrs:.1f}h ago",
        f"{name}: {verdict} — last successful cycle {hrs:.1f}h ago "
        f"(limit {_NO_CYCLE_HOURS}h)",
    )


def _check_last_three_failed(recent_verdicts: list[bool | None]) -> CheckResult:
    name = "last_three_failed"
    considered = recent_verdicts[:_LAST_N_FAILED]
    # Only "unhealthy" if we actually have N cycles and every one failed.
    all_failed = (
        len(considered) >= _LAST_N_FAILED
        and all(v is False for v in considered)
    )
    healthy = not all_failed
    verdict = "OK" if healthy else "FAIL"
    shown = ",".join("pass" if v is True else "fail" if v is False else "n/a"
                     for v in considered) or "none"
    return CheckResult(
        name, healthy, shown,
        f"{name}: {verdict} — last {len(considered)} verdict(s): {shown}",
    )


# ---------------------------------------------------------------------------
# Dashboard status: one-line green / amber / red (task 3 shares this logic)
# ---------------------------------------------------------------------------

def dashboard_status(
    *,
    now: datetime,
    last_pass_at: datetime | None,
    recent_verdicts: list[bool | None],
) -> tuple[str, str]:
    """Return (level, message) for the dashboard banner.

    red   — the last 3 cycles all failed verification.
    green — last successful cycle within 24h.
    amber — otherwise (stale, or never succeeded).
    Pure; `now` is a parameter.
    """
    considered = recent_verdicts[:_LAST_N_FAILED]
    if len(considered) >= _LAST_N_FAILED and all(v is False for v in considered):
        return "red", "Last 3 cycles failed verification — data may be unreliable."

    hrs = _age_hours(last_pass_at, now)
    if hrs is not None and hrs <= _DASHBOARD_FRESH_HOURS:
        return "green", f"Live — last verified cycle {hrs:.0f}h ago."

    if hrs is None:
        return "amber", "No verified cycle yet — data not available."
    return "amber", f"Stale — last verified cycle {hrs:.0f}h ago."


# ---------------------------------------------------------------------------
# I/O wrappers (read-only through storage, rule 2)
# ---------------------------------------------------------------------------

def _gather(db) -> dict[str, Any]:
    """Read the raw values the checks need. Raises if the DB is unreachable."""
    from edgedash import storage
    newest = storage.last_fetch_time(db)
    passing = storage.last_passing_cycle(db)
    last_pass_at = None
    if passing and passing.get("finished_at"):
        last_pass_at = _parse(passing["finished_at"])
    recent = storage.recent_cycles(db, _LAST_N_FAILED)
    verdicts = [c.get("verdict_passed") for c in recent]
    return {
        "newest_listing_at": newest,
        "last_pass_at": last_pass_at,
        "recent_verdicts": verdicts,
    }


def _parse(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def report(db, now: datetime | None = None) -> HealthReport:
    """Full health report reading through storage. Never leaks secrets."""
    from edgedash.config import load_config
    now = now or datetime.now(timezone.utc)
    max_days = load_config().max_data_age_days

    try:
        raw = _gather(db)
    except Exception:
        # DB unreachable / unreadable → single clear failure, no detail leak.
        return assess(
            now=now, db_reachable=False, newest_listing_at=None,
            last_pass_at=None, recent_verdicts=[], max_data_age_days=max_days,
        )

    return assess(
        now=now,
        db_reachable=True,
        newest_listing_at=raw["newest_listing_at"],
        last_pass_at=raw["last_pass_at"],
        recent_verdicts=raw["recent_verdicts"],
        max_data_age_days=max_days,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import logging
    logging.basicConfig(level=logging.WARNING)
    try:
        from edgedash.config import load_config
        db = load_config().abs_db_path
    except Exception:
        print("health: FAIL — could not load configuration")
        return 1

    rep = report(db)
    for c in rep.checks:
        print(c.message)
    print(f"health: {'HEALTHY' if rep.healthy else 'UNHEALTHY'} — {rep.summary()}")
    return 0 if rep.healthy else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
