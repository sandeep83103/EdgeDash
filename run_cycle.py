"""Entry point — run one full EdgeDash cycle.

Usage:
    python run_cycle.py
    python run_cycle.py --dry-run
    python run_cycle.py --force Scorer --force GapAnalyzer
    python run_cycle.py --explain

Flags (none change planning logic; build_plan stays a pure function):
    --dry-run        Read state, build+print the plan, then exit without
                     executing anything. No writes, no API calls, exit 0.
    --force <agent>  Add the named agent to the plan even if state says skip.
                     Repeatable. Reason recorded as "forced by operator".
    --explain        Print every SystemState value next to the decision it
                     drove. The "why did it skip that?" debugging tool.

Exit codes:
    0  — complete, nothing_to_do, or dry_run (all success; rule 28)
    1  — partial (at least one agent failed; the cycle still finished)
"""

import argparse
import sys

from edgedash.config import load_config
from edgedash.orchestrator import PARTIAL, run_cycle


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python run_cycle.py",
        description="Run one state-driven EdgeDash cycle.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan and exit without executing anything.",
    )
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="AGENT",
        dest="force",
        help="Force an agent to run even if state would skip it. Repeatable.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print each state value next to the decision it drove.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    config = load_config()

    outcome = run_cycle(
        config,
        dry_run=args.dry_run,
        force_agents=args.force,
        explain=args.explain,
    )
    sys.exit(1 if outcome == PARTIAL else 0)
