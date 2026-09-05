"""Verifier agent — judges output plausibility, never repairs (rule 34).

Reads the current cycle's scores, extracted facts, latest gap snapshot, and
latest fetch time from storage; runs the deterministic checks in
verification.run_all_checks; and returns an AgentResult carrying the Verdict.

Writes NO data other than the verdict it surfaces in its AgentResult and the
cycle_log line the Orchestrator records (rule 34). No LLM (rule 35).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.storage import (
    get_latest_gap_snapshot,
    get_scored_with_extractions,
    last_fetch_time,
)
from edgedash.verification import Verdict, run_all_checks


class Verifier:
    name: str = "Verifier"

    def run(
        self,
        config: Config,
        db_path: Path,
        stop_conditions: dict[str, int],
    ) -> AgentResult:
        verdict = self.verify(config, db_path)

        # The AgentResult status reflects the VERDICT (rule 37: name the check
        # and observed value that failed, never just "failed").
        status = "ok" if verdict.passed else "failed"
        notes = _verdict_notes(verdict)

        return AgentResult(
            agent=self.name,
            status=status,
            records_touched=0,        # the Verifier never writes data (rule 34)
            notes=notes,
        )

    def verify(self, config: Config, db_path: Path) -> Verdict:
        """Read current-cycle outputs and return the Verdict. Pure read."""
        rows = get_scored_with_extractions(db_path)
        scores = [int(r["fit_score"]) for r in rows]
        facts_list = [r["facts"] for r in rows]

        gaps = get_latest_gap_snapshot(db_path)
        latest_fetch = last_fetch_time(db_path)

        now = datetime.now(timezone.utc)
        return run_all_checks(
            scores=scores,
            facts_list=facts_list,
            gaps=gaps,
            latest_fetch_at=latest_fetch,
            config=config,
            now=now,
        )


def _verdict_notes(verdict: Verdict) -> str:
    """One-line verdict for the AgentResult (rule 37).

    Example on failure:
        "VERDICT: fail — score_spread observed {'range': 6, ...} (min 10)"
    """
    if verdict.passed:
        return f"VERDICT: pass — {verdict.summary}"

    # Lead with the first failing check's specifics, then list all failures.
    first = verdict.failed_checks[0]
    detail = (
        f"{first.name} observed {first.observed} "
        f"(threshold {first.threshold})"
    )
    all_names = ", ".join(c.name for c in verdict.failed_checks)
    return f"VERDICT: fail — {detail} · failed: {all_names}"
