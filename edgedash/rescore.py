"""Manual re-scoring escape hatch — run on purpose, never automatically.

Rule 18 prohibits automatic re-scoring. This command is the deliberate
override. The extraction cache is deliberately NOT cleared, so re-scoring
costs zero LLM API calls — facts were already extracted; only the
arithmetic reruns.

Usage:
    python -m edgedash.rescore --id <listing_id>
    python -m edgedash.rescore --all
"""

from __future__ import annotations

import argparse
import sys

from edgedash.config import load_config
from edgedash.storage import clear_all_scores, clear_score, init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m edgedash.rescore",
        description="Clear scores so the next cycle re-scores them. "
                    "The extraction cache is preserved — no API calls needed.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all",
        action="store_true",
        help="Clear every score in the database.",
    )
    group.add_argument(
        "--id",
        metavar="LISTING_ID",
        help="Clear the score for one specific listing.",
    )
    args = parser.parse_args()

    config = load_config()
    db = config.abs_db_path

    if not db.exists():
        print(f"No database found at {db}.")
        print("Run  python run_cycle.py  first.")
        sys.exit(1)

    init_db(db)   # safe no-op if already initialised; ensures migrations ran

    if args.all:
        _handle_all(db)
    else:
        _handle_one(db, args.id)


def _handle_all(db) -> None:
    print()
    print("This will clear ALL scores from every listing in the database.")
    print("The extraction cache will be preserved — re-scoring costs no API calls.")
    print()
    answer = input("Type  yes  to confirm: ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        sys.exit(0)

    cleared = clear_all_scores(db)
    print()
    if cleared == 0:
        print("No scored listings found — nothing to clear.")
    else:
        print(f"Cleared scores for {cleared} listing(s).")
    _remind()


def _handle_one(db, listing_id: str) -> None:
    found = clear_score(db, listing_id)
    print()
    if found:
        print(f"Cleared score for listing  {listing_id}.")
    else:
        print(f"No listing found with id  {listing_id}.")
        sys.exit(1)
    _remind()


def _remind() -> None:
    print()
    print("Run  python run_cycle.py  to re-score.")
    print()


if __name__ == "__main__":
    main()
