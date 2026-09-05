"""Launch a cycle as a SEPARATE process from the dashboard.

Steering rule 49 says the scheduler and the dashboard are separate processes
that share only the database. The dashboard must never run a cycle in its own
process. To honour that while still offering a "Run now" button, this helper
shells out to `run_cycle.py` as a child process — the dashboard never imports
the orchestrator and never scores or fetches in-process.

The child is bounded: it inherits run_cycle.py's own stop conditions and
verification, and we impose a hard wall-clock timeout here too (rule 51 —
the scheduled job is bounded).
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENTRY_POINT = _REPO_ROOT / "run_cycle.py"

# Hard ceiling for a UI-triggered run. Longer than a normal cycle, short
# enough that a hung run cannot block the tab forever.
_RUN_TIMEOUT_SECONDS = 600

# Stage markers the orchestrator prints, mapped to a progress percentage.
# We match on substrings of run_cycle.py's console output so the dashboard
# can show real forward motion. Order matters: later matches win.
_STAGE_MARKERS: tuple[tuple[str, int, str], ...] = (
    ("starting cycle", 5, "Reading state…"),
    ("Current state", 15, "Planning agents…"),
    ("Running agents", 30, "Fetching & scoring…"),
    ("Fetcher", 40, "Fetching job listings…"),
    ("Scorer", 60, "Scoring listings…"),
    ("GapAnalyzer", 75, "Analysing skill gaps…"),
    ("Verification", 88, "Verifying output…"),
    ("Cycle summary", 96, "Finalising…"),
    ("Nothing to do", 96, "Nothing to do this cycle…"),
)


class RunResult:
    """Outcome of a UI-triggered cycle run."""

    def __init__(self, ok: bool, returncode: int | None, output: str) -> None:
        self.ok = ok
        self.returncode = returncode
        self.output = output


def run_cycle_subprocess(timeout: int = _RUN_TIMEOUT_SECONDS) -> RunResult:
    """Run one cycle in a child process and capture its console output.

    Returns a RunResult. Never raises — a failure to launch or a timeout is
    reported through the result so the dashboard can show a clean message
    (rule 50) rather than a traceback.
    """
    if not _ENTRY_POINT.exists():
        return RunResult(False, None, f"Entry point not found: {_ENTRY_POINT}")

    try:
        proc = subprocess.run(
            [sys.executable, str(_ENTRY_POINT)],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            False,
            None,
            f"The cycle exceeded the {timeout}s time limit and was stopped.",
        )
    except OSError as exc:
        return RunResult(False, None, f"Could not start the cycle: {exc}")

    output = (proc.stdout or "") + (proc.stderr or "")
    # run_cycle.py exits 0 for complete / nothing_to_do / dry_run, 1 for partial.
    return RunResult(proc.returncode == 0, proc.returncode, output)


def run_cycle_streaming(
    timeout: int = _RUN_TIMEOUT_SECONDS,
) -> Iterator[tuple[int, str, RunResult | None]]:
    """Run one cycle in a child process, yielding (percent, label, result).

    Yields progress updates as the child prints its stage banners, then a
    final tuple whose third element is the completed RunResult. Never raises:
    a launch failure or timeout is reported through the final RunResult so the
    dashboard can show a clean message (rule 50), not a traceback.
    """
    if not _ENTRY_POINT.exists():
        yield 100, "Failed to start", RunResult(
            False, None, f"Entry point not found: {_ENTRY_POINT}"
        )
        return

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(_ENTRY_POINT)],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        yield 100, "Failed to start", RunResult(
            False, None, f"Could not start the cycle: {exc}"
        )
        return

    collected: list[str] = []
    pct = 5
    deadline = time.time() + timeout

    yield pct, "Starting the cycle…", None

    assert proc.stdout is not None
    for line in proc.stdout:
        collected.append(line)
        if time.time() > deadline:
            proc.kill()
            yield 100, "Timed out", RunResult(
                False, None,
                "".join(collected)
                + f"\nThe cycle exceeded the {timeout}s time limit and was stopped.",
            )
            return
        new_pct, label = _match_stage(line, pct)
        if new_pct > pct:
            pct = new_pct
            yield pct, label, None

    returncode = proc.wait()
    output = "".join(collected)
    yield 100, "Done", RunResult(returncode == 0, returncode, output)


def _match_stage(line: str, current_pct: int) -> tuple[int, str]:
    """Map one output line to a (percent, label), never regressing."""
    best_pct, best_label = current_pct, ""
    for marker, pct, label in _STAGE_MARKERS:
        if marker in line and pct >= best_pct:
            best_pct, best_label = pct, label
    return best_pct, best_label
