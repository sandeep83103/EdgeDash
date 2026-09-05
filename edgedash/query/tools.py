"""Query tool registry — deterministic, parameterised, read-only.

NO LLM anywhere in this file. These are the hand-written queries a router
model may SELECT from; it never composes a query (rule 40). Every parameter
is treated as untrusted model input: coerced to its declared type and clamped
to a safe range before any read (rule 41). Every read goes through the storage
module (rule 2). Every tool reads from the last passing cycle only (rule 46).

Each tool returns:
    {"rows": list[dict], "summary": str}

`rows` is the data (shown alongside any phrased answer, rule 44); `summary`
is a short description of what was looked at ("47 listings from the last 7
days"). When there is no passing cycle, tools return empty rows and say so.

The registry:
    TOOLS: dict[str, ToolSpec]   name -> spec (func + description + params)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from edgedash import storage
from edgedash.config import load_config
from edgedash.skills import canonical

# ---------------------------------------------------------------------------
# Registry + @tool decorator
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    name: str
    description: str            # what the router model sees — specific + unambiguous
    parameters: dict[str, Any]  # JSON-schema-style spec
    func: Callable[..., dict]


TOOLS: dict[str, ToolSpec] = {}


def tool(
    *,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
    """Register a query function in TOOLS under its own name.

    `description` is the router-facing text: state exactly when the tool
    applies so the model can route unambiguously. `parameters` is a
    JSON-schema-style dict of the accepted, typed parameters.
    """
    def _register(func: Callable[..., dict]) -> Callable[..., dict]:
        TOOLS[func.__name__] = ToolSpec(
            name=func.__name__,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            func=func,
        )
        return func
    return _register


# ---------------------------------------------------------------------------
# Parameter validation + clamping (rule 41). Every param is untrusted.
# ---------------------------------------------------------------------------

def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """Coerce value to int and clamp to [lo, hi]. Non-coercible → default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _no_passing_cycle(db: str | Path) -> bool:
    return storage.last_passing_cycle(db) is None


def _empty(summary: str) -> dict:
    return {"rows": [], "summary": summary}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _skills_present(db: str | Path) -> set[str]:
    """Canonical skills present in the latest gap snapshot (for validating
    a model-supplied `skill` parameter against reality)."""
    return {row["skill"] for row in storage.get_latest_gap_snapshot(db)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool(
    description=(
        "Companies that have posted job listings in the last N days, with a "
        "count of listings per company. Use for questions like 'who is hiring "
        "recently' or 'which companies posted this week'. Parameter: days "
        "(1-90, default 7)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
        },
    },
)
def companies_hiring(db: str | Path, days: int = 7) -> dict:
    days = _clamp_int(days, 1, 90, 7)
    if _no_passing_cycle(db):
        return _empty("no verified cycle yet — nothing to report")

    since_iso = (_now() - timedelta(days=days)).isoformat()
    rows = storage.companies_since(db, since_iso)
    total = sum(r["n"] for r in rows)
    return {
        "rows": rows,
        "summary": f"{total} listing(s) from {len(rows)} company(ies) in the last {days} day(s)",
    }


@tool(
    description=(
        "The highest-scoring job listings for the user's profile: score, "
        "title, company, and the reason string. Use for 'best matches', "
        "'top jobs for me', 'what should I look at'. Parameter: n (1-25, "
        "default 10)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
        },
    },
)
def best_matches(db: str | Path, n: int = 10) -> dict:
    n = _clamp_int(n, 1, 25, 10)
    if _no_passing_cycle(db):
        return _empty("no verified cycle yet — nothing to report")

    listings = storage.get_listings(db, limit=n, min_score=0)
    rows = [
        {
            "score": l.get("fit_score"),
            "title": l.get("title"),
            "company": l.get("company"),
            "reason": l.get("fit_reason") or "",
        }
        for l in listings
    ]
    return {"rows": rows, "summary": f"top {len(rows)} scored listing(s)"}


@tool(
    description=(
        "The user's top skill gaps ranked by opportunity cost (fit-weighted "
        "demand), each with how many listings it blocks. Use for 'what skills "
        "am I missing', 'biggest gaps', 'what should I learn'. Parameter: n "
        "(1-25, default 5)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
        },
    },
)
def top_gaps(db: str | Path, n: int = 5) -> dict:
    n = _clamp_int(n, 1, 25, 5)
    if _no_passing_cycle(db):
        return _empty("no verified cycle yet — nothing to report")

    snapshot = storage.get_latest_gap_snapshot(db)[:n]
    rows = [
        {
            "skill": g.get("skill"),
            "opportunity_cost": round(g.get("opportunity_cost", 0), 2),
            "listings_blocked": g.get("listings_blocked"),
            "confidence": g.get("confidence"),
        }
        for g in snapshot
    ]
    return {"rows": rows, "summary": f"top {len(rows)} skill gap(s) by opportunity cost"}


@tool(
    description=(
        "Drill down into ONE named skill gap: the specific listings that "
        "require that skill and the user lacks. Use when the user names a "
        "skill and asks 'which jobs need <skill>' or 'show me the listings "
        "behind that gap'. Parameter: skill (string, required)."
    ),
    parameters={
        "type": "object",
        "properties": {"skill": {"type": "string"}},
        "required": ["skill"],
    },
)
def gap_detail(db: str | Path, skill: str) -> dict:
    if _no_passing_cycle(db):
        return _empty("no verified cycle yet — nothing to report")

    aliases = load_config().skill_aliases
    canon = canonical(str(skill), aliases)

    # Match against skills actually present; unknown → empty, never raise.
    snapshot = storage.get_latest_gap_snapshot(db)
    match = next((g for g in snapshot if g.get("skill") == canon), None)
    if match is None:
        return _empty(f"'{skill}' is not among the current skill gaps")

    import json as _json
    try:
        ids = _json.loads(match.get("example_ids") or "[]")
    except (ValueError, TypeError):
        ids = []

    rows = storage.listings_by_ids(db, [str(i) for i in ids])
    return {
        "rows": rows,
        "summary": f"{len(rows)} listing(s) blocked by '{canon}'",
    }


@tool(
    description=(
        "How the opportunity cost of a named skill gap has changed over the "
        "last N weeks, using stored gap snapshots. Use for 'is <skill> gap "
        "growing', 'trend for <skill>', 'has this changed over time'. "
        "Parameters: skill (string, required), weeks (1-12, default 3)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "weeks": {"type": "integer", "minimum": 1, "maximum": 12, "default": 3},
        },
        "required": ["skill"],
    },
)
def trend(db: str | Path, skill: str, weeks: int = 3) -> dict:
    weeks = _clamp_int(weeks, 1, 12, 3)
    if _no_passing_cycle(db):
        return _empty("no verified cycle yet — nothing to report")

    aliases = load_config().skill_aliases
    canon = canonical(str(skill), aliases)

    since_iso = (_now() - timedelta(weeks=weeks)).isoformat()
    snap_rows = storage.gap_trend_rows(db, canon, since_iso)
    if not snap_rows:
        return _empty(f"no snapshots for '{canon}' in the last {weeks} week(s)")

    rows = [
        {
            "computed_at": r["computed_at"],
            "opportunity_cost": round(r["opportunity_cost"], 2),
            "listings_blocked": r["listings_blocked"],
        }
        for r in snap_rows
    ]
    first, last = rows[0]["opportunity_cost"], rows[-1]["opportunity_cost"]
    delta = round(last - first, 2)
    return {
        "rows": rows,
        "summary": (
            f"'{canon}' over {len(rows)} snapshot(s) in {weeks} week(s): "
            f"opportunity_cost {first}→{last} (Δ {delta:+})"
        ),
    }


@tool(
    description=(
        "Totals about the dataset: number of listings, how many are scored, "
        "how many unscored, and the newest listing date. Use for 'how many "
        "jobs', 'how much data do we have', 'is anything unscored'. No "
        "parameters."
    ),
    parameters={"type": "object", "properties": {}},
)
def listing_count(db: str | Path) -> dict:
    if _no_passing_cycle(db):
        return _empty("no verified cycle yet — nothing to report")

    total, scored = storage.count_listings(db)
    newest = storage.last_fetch_time(db)
    rows = [{
        "total_listings": total,
        "scored": scored,
        "unscored": total - scored,
        "newest_listing": newest.isoformat() if newest else None,
    }]
    return {"rows": rows, "summary": f"{total} listing(s), {scored} scored, {total - scored} unscored"}


@tool(
    description=(
        "For ONE named skill, how often it appears as a REQUIRED skill versus "
        "a nice-to-have across all extracted listings. Use for 'is <skill> "
        "usually required or optional', 'how in-demand is <skill>'. "
        "Parameter: skill (string, required)."
    ),
    parameters={
        "type": "object",
        "properties": {"skill": {"type": "string"}},
        "required": ["skill"],
    },
)
def skill_demand(db: str | Path, skill: str) -> dict:
    if _no_passing_cycle(db):
        return _empty("no verified cycle yet — nothing to report")

    aliases = load_config().skill_aliases
    canon = canonical(str(skill), aliases)

    counts = storage.skill_demand_counts(db, canon)
    if counts["required"] == 0 and counts["nice_to_have"] == 0:
        return _empty(f"'{canon}' does not appear in any extracted listing")

    rows = [{
        "skill": canon,
        "required": counts["required"],
        "nice_to_have": counts["nice_to_have"],
    }]
    return {
        "rows": rows,
        "summary": (
            f"'{canon}': required in {counts['required']} listing(s), "
            f"nice-to-have in {counts['nice_to_have']}"
        ),
    }
