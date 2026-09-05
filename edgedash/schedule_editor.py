"""Read and write the daily schedule for the SEPARATE scheduler process.

Steering rule 49: the scheduler and the dashboard are separate processes. The
real scheduler is the GitHub Actions workflow (.github/workflows/cycle.yml),
which runs `run_cycle.py` on a cron. The dashboard never runs that scheduled
cycle itself — it only lets a user choose *when* it should fire.

This module translates a user's chosen daily local time (IST) into the UTC
cron GitHub Actions requires, and rewrites only the `cron:` line in the
workflow. The change takes effect once the file is committed and pushed —
the dashboard cannot (and must not) reconfigure the runner at runtime.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "cycle.yml"

# The dashboard is used from India; the user picks a local (IST) time.
_IST_OFFSET = timedelta(hours=5, minutes=30)

_CRON_LINE = re.compile(r'^(\s*- cron:\s*)"[^"]*"(.*)$')


def ist_to_utc_cron(hour: int, minute: int) -> str:
    """Convert a daily IST time to a daily UTC 5-field cron expression.

    Example: 06:00 IST → "30 0 * * *" (00:30 UTC).
    """
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour must be 0-23 and minute 0-59.")

    total = (hour * 60 + minute) - int(_IST_OFFSET.total_seconds() // 60)
    total %= 24 * 60  # wrap across midnight
    utc_hour, utc_minute = divmod(total, 60)
    return f"{utc_minute} {utc_hour} * * *"


def read_current_cron() -> str | None:
    """Return the current cron expression from the workflow, or None."""
    if not _WORKFLOW_PATH.exists():
        return None
    for line in _WORKFLOW_PATH.read_text(encoding="utf-8").splitlines():
        m = _CRON_LINE.match(line)
        if m:
            inner = re.search(r'"([^"]*)"', line)
            return inner.group(1) if inner else None
    return None


def write_daily_schedule(hour: int, minute: int) -> str:
    """Rewrite the workflow's cron line to fire daily at the given IST time.

    Returns the new UTC cron string. Raises FileNotFoundError if the workflow
    is missing and ValueError if no cron line is present to replace.
    """
    if not _WORKFLOW_PATH.exists():
        raise FileNotFoundError(f"Workflow not found: {_WORKFLOW_PATH}")

    new_cron = ist_to_utc_cron(hour, minute)
    lines = _WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()

    replaced = False
    out: list[str] = []
    for line in lines:
        m = _CRON_LINE.match(line)
        if m and not replaced:
            out.append(
                f'{m.group(1)}"{new_cron}"'
                f"      # {hour:02d}:{minute:02d} IST (edited from dashboard)"
            )
            replaced = True
        else:
            out.append(line)

    if not replaced:
        raise ValueError("No 'cron:' line found in the workflow to update.")

    _WORKFLOW_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return new_cron
