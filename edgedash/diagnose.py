"""Read-only database diagnostic.

Usage:
    python -m edgedash.diagnose

Reads from the existing database only. No writes, no schema changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from edgedash.config import load_config
from edgedash.storage import (
    count_listings_by_source,
    cross_source_duplicates,
    listings_with_bad_fields,
    recent_listings,
)

_W = 62


def main() -> None:
    config = load_config()
    db = config.abs_db_path

    if not db.exists():
        print(f"No database found at {db}.")
        print("Run  python run_cycle.py  first to populate it.")
        sys.exit(0)

    _banner(f"EdgeDash diagnostics — {db.name}")

    _section_totals(db)
    _section_cross_source_dupes(db)
    _section_recent(db)
    _section_bad_fields(db)

    print()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _section_totals(db: Path) -> None:
    _header("Listings by source")
    by_source = count_listings_by_source(db)

    if not by_source:
        print("  (no listings in database)")
        return

    total = sum(by_source.values())
    for source, count in by_source.items():
        bar = _bar(count, total, width=20)
        print(f"  {source:<16}  {count:>5}  {bar}")
    print()
    print(f"  {'TOTAL':<16}  {total:>5}")


def _section_cross_source_dupes(db: Path) -> None:
    _header("Probable cross-source duplicates  (same title + company, different source)")
    dupes = cross_source_duplicates(db)

    if not dupes:
        print("  None found.")
        return

    print(f"  {len(dupes)} duplicate pair(s) detected.\n")
    col_t = 28
    col_c = 18
    print(f"  {'Title':<{col_t}}  {'Company':<{col_c}}  Sources")
    print("  " + "─" * (_W - 2))
    for row in dupes:
        title   = _trunc(row["title"]   or "", col_t)
        company = _trunc(row["company"] or "", col_c)
        print(f"  {title:<{col_t}}  {company:<{col_c}}  {row['sources']}")


def _section_recent(db: Path) -> None:
    _header("5 most recent listings")
    rows = recent_listings(db, limit=5)

    if not rows:
        print("  (none)")
        return

    col_src = 12
    col_co  = 16
    col_t   = _W - col_src - col_co - 8
    print(f"  {'Source':<{col_src}}  {'Company':<{col_co}}  Title")
    print("  " + "─" * (_W - 2))
    for row in rows:
        src     = _trunc(row["source"]  or "", col_src)
        company = _trunc(row["company"] or "", col_co)
        title   = _trunc(row["title"]   or "", col_t)
        fetched = (row["fetched_at"] or "")[:16].replace("T", " ")
        print(f"  {src:<{col_src}}  {company:<{col_co}}  {title}  [{fetched}]")


def _section_bad_fields(db: Path) -> None:
    _header("Data quality — NULL or empty url / title / company")
    bad = listings_with_bad_fields(db)

    if not bad:
        print("  No issues found.")
        return

    print(f"  {len(bad)} row(s) with missing required field(s):\n")
    for row in bad:
        problems = _missing_fields(row)
        print(f"  id={row['id']}  source={row['source'] or '?'}")
        print(f"    missing: {', '.join(problems)}")
        print(f"    title={row['title']!r}  company={row['company']!r}")
        print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _missing_fields(row: dict) -> list[str]:
    bad = []
    for field in ("url", "title", "company"):
        v = row.get(field)
        if v is None or str(v).strip() == "":
            bad.append(field)
    return bad


def _bar(value: int, total: int, width: int = 20) -> str:
    filled = round(width * value / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def _trunc(s: str, n: int) -> str:
    return s[:n - 1] + "…" if len(s) > n else s


def _banner(text: str) -> None:
    print()
    print("═" * _W)
    print(f"  {text}")
    print("═" * _W)


def _header(text: str) -> None:
    print()
    print(f"── {text} " + "─" * max(0, _W - len(text) - 4))


if __name__ == "__main__":
    main()
