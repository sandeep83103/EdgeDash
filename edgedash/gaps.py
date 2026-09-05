"""Gap report CLI — prints the latest gap snapshot as a readable table.

Usage:
    python -m edgedash.gaps            # latest snapshot table
    python -m edgedash.gaps --trend    # change over time vs earliest snapshot

Read-only — no writes, no interpolation, no extrapolation.
"""

from __future__ import annotations

import argparse
import sys

from edgedash.config import load_config
from edgedash.storage import (
    count_gap_snapshots,
    get_earliest_gap_snapshot,
    get_latest_gap_snapshot,
    init_db,
)

_W = 72
_BAR_WIDTH = 18
_LOW_CONF_MARKER = " ⚠ low confidence"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m edgedash.gaps",
        description="Skill gap report — read-only view of the latest snapshot.",
    )
    parser.add_argument(
        "--trend",
        action="store_true",
        help="Show opportunity_cost change over time vs the earliest snapshot.",
    )
    args = parser.parse_args()

    config = load_config()
    db = config.abs_db_path

    if not db.exists():
        print(f"No database at {db}.")
        print("Run  python run_cycle.py  first.")
        sys.exit(0)

    init_db(db)

    if args.trend:
        _trend(db)
    else:
        _latest(db)


def _latest(db) -> None:
    rows = get_latest_gap_snapshot(db)

    if not rows:
        print()
        print("No gap snapshots found.")
        print("Run  python run_cycle.py  to fetch, score, and analyse listings.")
        return

    computed_at = rows[0]["computed_at"][:19].replace("T", " ") + " UTC"
    _banner(f"Skill gap report  —  {computed_at}")

    _print_table(rows)
    _print_legend(rows)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _print_table(rows: list[dict]) -> None:
    max_opp = max(r["opportunity_cost"] for r in rows) or 1.0

    # Column widths
    c_rank  =  4
    c_skill = 30
    c_blk   =  6
    c_cost  =  6
    c_mean  =  6
    c_bar   = _BAR_WIDTH

    header = (
        f"  {'#':<{c_rank}}"
        f"{'Skill':<{c_skill}}"
        f"{'Blkd':>{c_blk}}"
        f"{'Cost':>{c_cost}}"
        f"{'Mean':>{c_mean}}  "
        f"{'Opportunity':<{c_bar}}"
    )
    print()
    print(header)
    print("  " + "─" * (_W - 2))

    for i, row in enumerate(rows, 1):
        skill    = row["skill"]
        blocked  = row["listings_blocked"]
        cost     = row["opportunity_cost"]
        mean     = row["mean_score"]
        conf     = row["confidence"]
        also     = row.get("also_nice", 0)

        bar      = _bar(cost, max_opp, _BAR_WIDTH)
        low_flag = " ⚠" if conf == "low" else ""
        nice_str = f" (+{also}✦)" if also else ""

        skill_display = skill[:c_skill - 1] + "…" if len(skill) >= c_skill else skill
        skill_col = f"{skill_display}{low_flag}{nice_str}"
        # Recompute column width accounting for flag/nice suffix
        pad = max(0, c_skill - len(skill_col))

        print(
            f"  {i:<{c_rank}}"
            f"{skill_col}{' ' * pad}"
            f"{blocked:>{c_blk}}"
            f"{cost:>{c_cost}.1f}"
            f"{mean:>{c_mean}.0f}  "
            f"{bar}"
        )

    print("  " + "─" * (_W - 2))
    total_blocked = sum(r["listings_blocked"] for r in rows)
    total_cost    = sum(r["opportunity_cost"] for r in rows)
    print(
        f"  {'TOTAL':<{c_rank + c_skill}}"
        f"{total_blocked:>{c_blk}}"
        f"{total_cost:>{c_cost}.1f}"
    )


def _print_legend(rows: list[dict]) -> None:
    low_conf = [r for r in rows if r["confidence"] == "low"]
    print()
    print("  Blkd = listings requiring this skill you don't have")
    print("  Cost = Σ(fit_score/100) — weighted by how good each listing is")
    print("  ✦    = also appears as nice-to-have in additional listings")
    if low_conf:
        skills = ", ".join(r["skill"] for r in low_conf)
        print(f"  ⚠    = low confidence (< 3 listings): {skills}")
    print()


# ---------------------------------------------------------------------------
# Trend view  (python -m edgedash.gaps --trend)
# ---------------------------------------------------------------------------

def _trend(db) -> None:
    n_snapshots = count_gap_snapshots(db)

    if n_snapshots == 0:
        print()
        print("No gap snapshots found.")
        print("Run  python run_cycle.py  to fetch, score, and analyse listings.")
        return

    if n_snapshots == 1:
        # Exactly one data point. Refuse to invent a trend from it.
        latest = get_latest_gap_snapshot(db)
        date = latest[0]["computed_at"][:10] if latest else "unknown"
        _banner("Skill gap trend")
        print()
        print(f"  Only one snapshot exists so far (from {date}).")
        print("  A trend needs at least two snapshots taken on different runs.")
        print()
        print("  Run  python run_cycle.py  once per day.")
        print("  After the next run you will have 2 snapshots — enough for a")
        print("  first trend. Roughly 1 more day of runs is needed.")
        print()
        return

    earliest = get_earliest_gap_snapshot(db)
    latest = get_latest_gap_snapshot(db)

    early_date = earliest[0]["computed_at"][:19].replace("T", " ")
    late_date = latest[0]["computed_at"][:19].replace("T", " ")

    early_cost = {r["skill"]: r["opportunity_cost"] for r in earliest}
    early_skills = set(early_cost)
    latest_top = latest  # already ranked by opportunity_cost desc
    latest_top_skills = {r["skill"] for r in latest_top}

    _banner("Skill gap trend")
    print()
    print(f"  Comparing {n_snapshots} snapshots")
    print(f"  Earliest : {early_date} UTC")
    print(f"  Latest   : {late_date} UTC")

    _print_trend_table(latest_top, early_cost, early_skills)

    # Skills that were in the earliest top-10 but have dropped out of the
    # latest top-10.
    dropped = [
        r for r in earliest
        if r["skill"] not in latest_top_skills
    ]
    print()
    if dropped:
        print("  Dropped out of the latest top 10 (were present at the start):")
        for r in dropped:
            print(f"    − {r['skill']}  (was cost {r['opportunity_cost']:.1f})")
    else:
        print("  No skills dropped out of the top 10 since the earliest snapshot.")

    print()
    print("  NEW = not present in the earliest snapshot")
    print("  Δ    = latest opportunity_cost minus earliest")
    print()


def _print_trend_table(
    latest_rows: list[dict],
    early_cost: dict[str, float],
    early_skills: set[str],
) -> None:
    c_rank  =  4
    c_skill = 26
    c_early =  8
    c_late  =  8
    c_delta =  9
    c_pct   =  9

    print()
    header = (
        f"  {'#':<{c_rank}}"
        f"{'Skill':<{c_skill}}"
        f"{'Earliest':>{c_early}}"
        f"{'Latest':>{c_late}}"
        f"{'Δ':>{c_delta}}"
        f"{'Δ%':>{c_pct}}"
    )
    print(header)
    print("  " + "─" * (_W - 2))

    for i, row in enumerate(latest_rows, 1):
        skill = row["skill"]
        late = row["opportunity_cost"]
        is_new = skill not in early_skills
        early = early_cost.get(skill, 0.0)

        skill_display = skill[:c_skill - 1] + "…" if len(skill) >= c_skill else skill

        if is_new:
            skill_col = f"{skill_display} NEW"
            pad = max(0, c_skill - len(skill_col))
            print(
                f"  {i:<{c_rank}}"
                f"{skill_col}{' ' * pad}"
                f"{'—':>{c_early}}"
                f"{late:>{c_late}.1f}"
                f"{'—':>{c_delta}}"
                f"{'—':>{c_pct}}"
            )
            continue

        delta = late - early
        pct = (delta / early * 100.0) if early else 0.0
        sign = "+" if delta >= 0 else ""

        print(
            f"  {i:<{c_rank}}"
            f"{skill_display:<{c_skill}}"
            f"{early:>{c_early}.1f}"
            f"{late:>{c_late}.1f}"
            f"{sign + format(delta, '.1f'):>{c_delta}}"
            f"{sign + format(pct, '.0f') + '%':>{c_pct}}"
        )

    print("  " + "─" * (_W - 2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(value: float, maximum: float, width: int) -> str:
    filled = round(width * value / maximum) if maximum else 0
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _banner(text: str) -> None:
    print()
    print("═" * _W)
    print(f"  {text}")
    print("═" * _W)


if __name__ == "__main__":
    main()
