"""Deterministic scoring — no model calls, no network, no llm imports.

score_listing() is a pure function: given a listing dict, an extraction
facts dict, and a Config, it returns a score dict. Same inputs always
produce the same output. All weights come from config — they never appear
here as magic numbers.

build_reason() assembles a human-readable string from the score components.
The model never writes this string (rule 19).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Seniority band scale
# ---------------------------------------------------------------------------

# Ordered lowest → highest. Distance is the index difference.
_SENIORITY_BANDS: list[str] = ["junior", "mid", "senior", "lead"]
_BAND_SCORE: dict[int, float] = {0: 1.0, 1: 0.6, 2: 0.25}  # 3+ → 0.0


def _seniority_score(facts_seniority: str, target: str) -> float:
    fs = facts_seniority.lower().strip()
    ts = target.lower().strip()
    if fs == "unknown" or fs not in _SENIORITY_BANDS:
        return 0.5   # neutral — we simply don't know
    if ts not in _SENIORITY_BANDS:
        return 0.5
    distance = abs(_SENIORITY_BANDS.index(fs) - _SENIORITY_BANDS.index(ts))
    return _BAND_SCORE.get(distance, 0.0)


# ---------------------------------------------------------------------------
# Skill match
# ---------------------------------------------------------------------------

def _skill_score(
    required: list[str],
    nice: list[str],
    my_skills: list[str],
) -> tuple[float, list[str], list[str]]:
    """Return (raw_score, matched, missing) for required skills.

    nice_to_have counts at 1/3 weight added on top, capped at 1.0.
    Returns matched and missing lists for use in build_reason().
    """
    my_lower: set[str] = {s.lower().strip() for s in my_skills}

    matched: list[str] = []
    missing: list[str] = []

    if required:
        for skill in required:
            if skill.lower().strip() in my_lower:
                matched.append(skill)
            else:
                missing.append(skill)
        base = len(matched) / len(required)
    else:
        # No required skills stated — neutral, not penalised.
        base = 0.7
        missing = []

    # Nice-to-have bonus: each match adds (1/3) / len(nice), cap at 1.0.
    if nice:
        nice_matched = sum(
            1 for s in nice if s.lower().strip() in my_lower
        )
        bonus = (nice_matched / len(nice)) * (1.0 / 3.0)
        base = min(1.0, base + bonus)

    return base, matched, missing


# ---------------------------------------------------------------------------
# Location fit
# ---------------------------------------------------------------------------

def _location_score(
    remote_ok: bool | None,
    listing_location: str | None,
    target_city: str,
) -> float:
    if remote_ok is True:
        return 1.0
    loc = (listing_location or "").lower().strip()
    if not loc:
        return 0.5   # unknown
    if target_city.lower().strip() in loc:
        return 1.0
    if "remote" in loc:
        return 1.0
    return 0.1       # clearly somewhere else and not remote


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------

def _recency_score(posted_at: str | None) -> tuple[float, int | None]:
    """Return (score, age_days). age_days is None when posted_at is absent."""
    if not posted_at:
        return 0.5, None
    try:
        dt = datetime.fromisoformat(posted_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).days
        # Linear decay: 1.0 at day 0, 0.0 at day 30.
        score = max(0.0, 1.0 - age_days / 30.0)
        return score, age_days
    except (ValueError, TypeError, OverflowError):
        return 0.5, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_listing(
    listing: dict[str, Any],
    facts: dict[str, Any],
    config: Config,
) -> dict[str, Any]:
    """Score one listing against config. Pure function — no side effects.

    Returns:
        {
            "score":      int 0-100,
            "reason":     str,
            "components": {
                "skill_match":   float,
                "seniority_fit": float,
                "location_fit":  float,
                "recency":       float,
            }
        }
    """
    my_skills: list[str] = config.my_skills

    skill_raw, matched, missing = _skill_score(
        facts.get("required_skills") or [],
        facts.get("nice_to_have") or [],
        my_skills,
    )
    seniority_raw  = _seniority_score(
        facts.get("seniority") or "unknown",
        config.target_seniority,
    )
    location_raw   = _location_score(
        facts.get("remote_ok"),
        listing.get("location"),
        config.target_city,
    )
    recency_raw, age_days = _recency_score(listing.get("posted_at"))

    weighted = (
        skill_raw    * config.weight_skill_match
        + seniority_raw * config.weight_seniority_fit
        + location_raw  * config.weight_location_fit
        + recency_raw   * config.weight_recency
    )
    score = max(0, min(100, round(weighted * 100)))

    components = {
        "skill_match":   round(skill_raw,    4),
        "seniority_fit": round(seniority_raw, 4),
        "location_fit":  round(location_raw,  4),
        "recency":       round(recency_raw,   4),
    }

    reason = build_reason(
        components=components,
        facts=facts,
        config=config,
        matched=matched,
        missing=missing,
        age_days=age_days,
    )

    return {"score": score, "reason": reason, "components": components}


def build_reason(
    components: dict[str, float],
    facts: dict[str, Any],
    config: Config,
    matched: list[str],
    missing: list[str],
    age_days: int | None,
) -> str:
    """Build a compact human-readable reason from score components (rule 19).

    The model never writes this string. Every clause is derived from
    numbers and lists produced by score_listing().

    Example:
        "4/6 required skills · seniority fits · remote · posted 2d ago ·
         gap: substation design, etap"
    """
    parts: list[str] = []

    # Skill match clause
    required = facts.get("required_skills") or []
    if required:
        parts.append(f"{len(matched)}/{len(required)} required skills")
    else:
        parts.append("no required skills listed")

    # Seniority clause
    s = components["seniority_fit"]
    seniority_label = facts.get("seniority") or "unknown"
    if s >= 1.0:
        parts.append("seniority fits")
    elif s >= 0.6:
        parts.append(f"seniority near ({seniority_label})")
    elif s >= 0.5:
        parts.append(f"seniority unknown")
    else:
        parts.append(f"seniority mismatch ({seniority_label})")

    # Location clause
    lf = components["location_fit"]
    ro = facts.get("remote_ok")
    if ro is True:
        parts.append("remote")
    elif lf >= 1.0:
        parts.append(f"in {config.target_city}")
    elif lf >= 0.5:
        parts.append("location unknown")
    else:
        parts.append("not remote / wrong city")

    # Recency clause
    if age_days is None:
        parts.append("posting date unknown")
    elif age_days == 0:
        parts.append("posted today")
    else:
        parts.append(f"posted {age_days}d ago")

    # Gap clause — the most actionable part
    if missing:
        gap_str = ", ".join(missing[:5])
        if len(missing) > 5:
            gap_str += f" (+{len(missing) - 5} more)"
        parts.append(f"gap: {gap_str}")

    return " · ".join(parts)
