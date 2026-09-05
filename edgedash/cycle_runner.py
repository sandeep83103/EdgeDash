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
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENTRY_POINT = _REPO_ROOT / "run_cycle.py"

# Hard ceiling for a UI-triggered run. Longer than a normal cycle, short
# enough that a hung run cannot block the tab forever.
_RUN_TIMEOUT_SECONDS = 600


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
