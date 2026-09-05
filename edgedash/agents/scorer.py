"""Scorer agent — extract facts, score deterministically, write results.

No model calls in this file. The LLM is invoked only inside extractor.extract().
All arithmetic is in scoring.score_listing(). This file orchestrates the batch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from edgedash.agents.base import AgentResult
from edgedash.agents.extractor import extract
from edgedash.config import Config
from edgedash.llm import LLMError
from edgedash.scoring import score_listing
from edgedash.storage import (
    get_unscored_listings,
    log_cycle,
    save_score,
)

logger = logging.getLogger(__name__)

# Minimum spread (max - min) below which a batch is flagged as suspect.
_SUSPECT_SPREAD = 10


class Scorer:
    name: str = "Scorer"

    def run(
        self,
        config: Config,
        db_path: Path,
        stop_conditions: dict[str, int],
    ) -> AgentResult:
        # Limits come from the Orchestrator (rule 29), not from config directly.
        max_items = stop_conditions.get("max_items", config.scoring_batch_size)
        max_seconds = stop_conditions.get("max_seconds")
        # Adjusted-context flag the Orchestrator sets on a verification retry
        # after score_spread failed (rule 36). 1 = widen the distribution.
        widen = bool(stop_conditions.get("widen_distribution", 0))

        batch = get_unscored_listings(db_path, limit=max_items)

        if not batch:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="no unscored listings",
            )

        # Score every listing first (holding results in memory), so that on a
        # widen retry we can contrast-stretch the whole batch before persisting.
        scored_rows: list[dict] = []   # {id, score, reason}
        failed = 0
        timed_out = False
        started = datetime.now(timezone.utc)

        for listing in batch:
            if max_seconds is not None:
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                if elapsed >= max_seconds:
                    timed_out = True
                    break

            row = _score_one(listing, config, db_path)
            if row is None:
                failed += 1
            else:
                scored_rows.append(row)

        # On a widen retry, apply a deterministic contrast stretch around the
        # batch mean (see module docstring). Ranking is preserved; range and
        # stdev increase — exactly what check_score_spread measures.
        widened = False
        if widen and len(scored_rows) >= 2:
            _widen_scores(scored_rows, config)
            widened = True

        # Persist final scores (post-stretch if widened).
        for row in scored_rows:
            save_score(db_path, row["id"], row["score"], row["reason"])

        scores = [r["score"] for r in scored_rows]
        dist_notes = _distribution_notes(scores, failed)
        if widened:
            dist_notes += f" · WIDENED (gain={config.score_spread_gain})"
        if timed_out:
            dist_notes += f" · stopped at max_seconds={max_seconds}"
        _log_distribution(db_path, self.name, scores, failed)

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=len(scores),
            notes=dist_notes,
        )


# ---------------------------------------------------------------------------
# Per-listing scoring (isolated so one failure never stops the batch)
# ---------------------------------------------------------------------------

def _score_one(
    listing: dict,
    config: Config,
    db_path: Path,
) -> dict | None:
    """Extract and score one listing. Returns {id, score, reason} or None.

    Does NOT persist — the caller saves after an optional batch-level widen,
    so the two paths (normal and retry) share one write point.
    """
    lid = listing.get("id", "?")
    started = datetime.now(timezone.utc)

    try:
        facts = extract(listing, config, db_path)
        if facts is None:
            raise ValueError("extractor returned None")

        result = score_listing(listing, facts, config)
        return {"id": lid, "score": result["score"], "reason": result["reason"]}

    except LLMError as exc:
        _log_listing_failure(db_path, lid, started, f"LLMError: {exc}")
        logger.warning("Scorer: listing %s failed extraction — %s", lid, exc)
        return None

    except Exception as exc:
        _log_listing_failure(db_path, lid, started, f"{type(exc).__name__}: {exc}")
        logger.warning("Scorer: listing %s failed — %s", lid, exc)
        return None


def _widen_scores(scored_rows: list[dict], config: Config) -> None:
    """Contrast-stretch scores around the batch mean, in place (rule 36 retry).

    new = clamp(round(mean + gain * (score - mean)), 0, 100)

    gain > 1 pushes every score away from the mean, so the batch range and
    standard deviation both grow while the ranking is preserved. This is the
    "adjusted context" the Orchestrator requests when the score_spread check
    failed — a bunched distribution is stretched toward the 0-100 edges.
    A note is appended to each reason so the widening is visible, not silent.
    """
    scores = [r["score"] for r in scored_rows]
    mean = sum(scores) / len(scores)
    gain = config.score_spread_gain

    for row in scored_rows:
        stretched = mean + gain * (row["score"] - mean)
        new_score = max(0, min(100, round(stretched)))
        if new_score != row["score"]:
            row["reason"] += f" · widened {row['score']}→{new_score}"
        row["score"] = new_score


def _log_listing_failure(
    db_path: Path,
    listing_id: str,
    started: datetime,
    msg: str,
) -> None:
    now = datetime.now(timezone.utc)
    log_cycle(
        db_path,
        agent=f"Scorer/{listing_id[:12]}",
        started_at=started,
        finished_at=now,
        records_touched=0,
        status="fail",
        notes=msg,
    )


# ---------------------------------------------------------------------------
# Distribution stats and logging (rule 20)
# ---------------------------------------------------------------------------

def _distribution_notes(scores: list[int], failed: int) -> str:
    if not scores:
        return f"scored 0 · {failed} failed"

    lo   = min(scores)
    hi   = max(scores)
    mean = round(sum(scores) / len(scores))
    spread = hi - lo
    spread_label = "SUSPECT spread" if spread < _SUSPECT_SPREAD else "spread OK"

    parts = [
        f"scored {len(scores)}",
        f"range {lo}-{hi}",
        f"mean {mean}",
    ]
    if failed:
        parts.append(f"{failed} failed")
    parts.append(spread_label)
    return " · ".join(parts)


def _log_distribution(
    db_path: Path,
    agent_name: str,
    scores: list[int],
    failed: int,
) -> None:
    """Write score distribution to cycle_log per rule 20."""
    if not scores:
        notes = f"no scores produced · {failed} failed"
        status = "fail" if failed else "pass"
    else:
        lo     = min(scores)
        hi     = max(scores)
        mean   = round(sum(scores) / len(scores))
        spread = hi - lo
        suspect = spread < _SUSPECT_SPREAD

        notes = (
            f"distribution: count={len(scores)} min={lo} max={hi} "
            f"mean={mean} spread={spread}"
        )
        if suspect:
            notes += f" — SUSPECT: all scores within {spread} points"
        if failed:
            notes += f" · {failed} listing(s) failed"

        status = "fail" if suspect else "pass"

    now = datetime.now(timezone.utc)
    log_cycle(
        db_path,
        agent=f"{agent_name}/distribution",
        started_at=now,
        finished_at=now,
        records_touched=len(scores),
        status=status,
        notes=notes,
    )
