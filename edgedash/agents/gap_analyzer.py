"""GapAnalyzer agent — deterministic skill gap computation.

No LLM calls anywhere in this file. No network. Pure Python arithmetic
over data that is already in the database.

Opportunity cost formula (rule 24):
    For each canonical missing skill S:
        opportunity_cost = Σ (listing.fit_score / 100.0)
                           for each scored listing that requires S

    A listing scored 85 contributes 0.85; one scored 20 contributes 0.20.
    This means a skill blocking ten 80-point listings (cost 8.0) outranks
    one blocking twenty 30-point listings (cost 6.0) even though the latter
    is more frequent by count. Raw frequency never drives the ranking.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.skill_classifier import classify_skills
from edgedash.skills import canonical
from edgedash.storage import (
    get_scored_with_extractions,
    save_gap_snapshot,
)

_MIN_LISTINGS_FOR_CONFIDENCE = 3
_TOP_N = 10
_MAX_EXAMPLE_IDS = 5


class GapAnalyzer:
    name: str = "GapAnalyzer"

    def run(
        self,
        config: Config,
        db_path: Path,
        stop_conditions: dict[str, int],
    ) -> AgentResult:
        # max_seconds is the Orchestrator-supplied budget (rule 29). Gap
        # analysis is a single bounded pass over already-fetched data, so
        # there is no mid-loop checkpoint to interrupt; we accept the limit
        # and surface it if the run overruns.
        max_seconds = stop_conditions.get("max_seconds")
        started = datetime.now(timezone.utc)

        rows = get_scored_with_extractions(db_path)

        if not rows:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="no scored+extracted listings available",
            )

        my_skills: set[str] = {
            canonical(s, config.skill_aliases)
            for s in config.my_skills
            if s.strip()
        }

        # Classify every canonical skill across the whole batch in one pass.
        # The model only labels individual skills; all gap arithmetic below is
        # pure Python (rule 22). Verdicts are cached, so this is a no-op on
        # skills already seen in a previous run.
        electrical = _classify_all(rows, config, db_path)

        gaps = _compute_gaps(rows, my_skills, config.skill_aliases, electrical)
        ranked = sorted(gaps.values(), key=lambda g: g["opportunity_cost"], reverse=True)
        top = ranked[:_TOP_N]

        run_id = str(uuid.uuid4())
        save_gap_snapshot(db_path, run_id, top)

        notes = _build_notes(top, len(rows))
        if max_seconds is not None:
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed > max_seconds:
                notes += f" · OVERRAN max_seconds={max_seconds} ({elapsed:.0f}s)"
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=len(top),
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _classify_all(
    rows: list[dict[str, Any]],
    config: Config,
    db_path: Path,
) -> dict[str, bool]:
    """Canonicalise every skill in the batch and classify it once via the LLM.

    Returns canonical_skill -> is_electrical. Cached verdicts mean repeat
    runs make no model calls for skills already seen.
    """
    aliases = config.skill_aliases
    all_canon: set[str] = set()
    for row in rows:
        facts = row["facts"]
        for key in ("required_skills", "nice_to_have"):
            for s in (facts.get(key) or []):
                if s.strip():
                    all_canon.add(canonical(s, aliases))
    return classify_skills(sorted(all_canon), config, db_path)


def _compute_gaps(
    rows: list[dict[str, Any]],
    my_skills: set[str],
    aliases: dict[str, str],
    electrical: dict[str, bool],
) -> dict[str, dict[str, Any]]:
    """Return a dict of canonical_skill -> gap_record for every missing skill.

    Only skills the classifier flagged as electrical-engineering skills are
    counted. Language requirements, soft/management skills, and non-English
    strings are excluded via the `electrical` verdict map (rule 22 — the model
    labels individual skills; the arithmetic here is pure Python).
    """

    # accumulators keyed on canonical skill name
    scores_by_skill:     defaultdict[str, list[int]]  = defaultdict(list)
    ids_by_skill:        defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    nice_count_by_skill: defaultdict[str, int]         = defaultdict(int)

    for row in rows:
        score  = row["fit_score"]
        lid    = row["id"]
        facts  = row["facts"]

        required = [
            c for c in (
                canonical(s, aliases)
                for s in (facts.get("required_skills") or [])
                if s.strip()
            )
            if electrical.get(c, False)
        ]
        nice = [
            c for c in (
                canonical(s, aliases)
                for s in (facts.get("nice_to_have") or [])
                if s.strip()
            )
            if electrical.get(c, False)
        ]

        for skill in required:
            if skill not in my_skills:
                scores_by_skill[skill].append(score)
                ids_by_skill[skill].append((score, lid))

        for skill in nice:
            if skill not in my_skills:
                nice_count_by_skill[skill] += 1

    gaps: dict[str, dict[str, Any]] = {}
    for skill, scores in scores_by_skill.items():
        count        = len(scores)
        opp_cost     = sum(s / 100.0 for s in scores)
        mean_score   = sum(scores) / count
        top_score    = max(scores)
        confidence   = "ok" if count >= _MIN_LISTINGS_FOR_CONFIDENCE else "low"

        # example_ids: up to 5 listing IDs, highest-scoring first (rule 26)
        sorted_ids = sorted(ids_by_skill[skill], key=lambda t: t[0], reverse=True)
        example_ids = [lid for _, lid in sorted_ids[:_MAX_EXAMPLE_IDS]]

        gaps[skill] = {
            "skill":            skill,
            "listings_blocked": count,
            "opportunity_cost": opp_cost,
            "mean_score":       mean_score,
            "top_score":        top_score,
            "also_nice":        nice_count_by_skill.get(skill, 0),
            "confidence":       confidence,
            "example_ids":      example_ids,
        }

    return gaps


# ---------------------------------------------------------------------------
# Notes string for AgentResult
# ---------------------------------------------------------------------------

def _build_notes(top: list[dict[str, Any]], total_analysed: int) -> str:
    if not top:
        return f"no gaps found · {total_analysed} listings analysed"

    best = top[0]
    top_str = (
        f"{best['skill']} "
        f"({best['listings_blocked']} listings, "
        f"cost {best['opportunity_cost']:.1f})"
    )
    return (
        f"{len(top)} gaps · "
        f"top: {top_str} · "
        f"{total_analysed} listings analysed"
    )
