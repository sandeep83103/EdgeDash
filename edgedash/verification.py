"""Verification checks — deterministic Python, no LLM (rule 34, 35).

A model cannot be the judge of a model's output, so nothing here calls the
LLM. Each check is a PURE function: no clock, no network, no database reads.
Everything a check needs is passed in, including `now` for freshness.

Every check answers "does this output look plausible?" — never "is this
value correct?" (rule 35). There is no ground truth for a fit score; checks
assert properties of the distribution and shape only.

Checks return a CheckResult; run_all_checks aggregates them into a Verdict.
The Verifier judges — it never repairs (rule 34). Acting on a failed verdict
is the Orchestrator's job.

Thresholds come from config (rule 39); none are hardcoded here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from edgedash.config import Config

# Minimum scores needed before the spread check is meaningful.
_MIN_SCORES_FOR_SPREAD = 5


@dataclass
class CheckResult:
    name: str
    passed: bool
    observed: Any          # the value actually seen (for the log — rule 37)
    threshold: Any         # the config threshold it was compared against
    message: str           # human-readable, names check + observed value


@dataclass
class Verdict:
    passed: bool
    failed_checks: list[CheckResult]
    summary: str


# ---------------------------------------------------------------------------
# 1. Score spread — catches the inflation failure mode
# ---------------------------------------------------------------------------

def check_score_spread(scores: list[int], config: "Config") -> CheckResult:
    """FAIL if scores are too bunched: range < min_score_spread OR
    stdev < min_score_stdev. A model that inflates everything to a narrow
    band produces plausible-looking-but-useless output; this catches it.

    Passes trivially (and says so) when there are fewer than 5 scores —
    spread is not meaningful on a tiny sample.
    """
    name = "score_spread"
    n = len(scores)

    if n < _MIN_SCORES_FOR_SPREAD:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"n={n}",
            threshold=f"needs >= {_MIN_SCORES_FOR_SPREAD} scores",
            message=(
                f"{name}: PASS (trivially) — only {n} score(s), "
                f"fewer than {_MIN_SCORES_FOR_SPREAD}; spread not meaningful."
            ),
        )

    score_range = max(scores) - min(scores)
    stdev = statistics.pstdev(scores)

    range_ok = score_range >= config.min_score_spread
    stdev_ok = stdev >= config.min_score_stdev
    passed = range_ok and stdev_ok

    observed = {"range": score_range, "stdev": round(stdev, 2)}
    threshold = {
        "min_score_spread": config.min_score_spread,
        "min_score_stdev": config.min_score_stdev,
    }

    if passed:
        message = (
            f"{name}: PASS — range={score_range} "
            f"(>= {config.min_score_spread}), stdev={stdev:.2f} "
            f"(>= {config.min_score_stdev})."
        )
    else:
        problems = []
        if not range_ok:
            problems.append(
                f"range={score_range} below min_score_spread={config.min_score_spread}"
            )
        if not stdev_ok:
            problems.append(
                f"stdev={stdev:.2f} below min_score_stdev={config.min_score_stdev}"
            )
        message = f"{name}: FAIL — " + "; ".join(problems) + " (score inflation)"

    return CheckResult(name, passed, observed, threshold, message)


# ---------------------------------------------------------------------------
# 2. Extraction sanity — catches a broken extractor / sentence-as-skills
# ---------------------------------------------------------------------------

def check_extraction_sanity(
    facts_list: list[dict],
    config: "Config",
) -> CheckResult:
    """FAIL if too many extractions are empty, or any listing has absurdly
    many skills.

    - empty rate > max_empty_extraction_pct → the extractor is likely broken.
    - any listing with > max_skills_per_listing → the model probably returned
      a whole sentence (or paragraph) split into "skills".
    """
    name = "extraction_sanity"
    n = len(facts_list)

    if n == 0:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0",
            threshold="n/a",
            message=f"{name}: PASS (trivially) — no extractions to check.",
        )

    empty_count = sum(
        1 for f in facts_list if not (f.get("required_skills") or [])
    )
    empty_pct = (empty_count / n) * 100.0

    # Largest single required_skills list, and which listing it was.
    max_skills = 0
    for f in facts_list:
        count = len(f.get("required_skills") or [])
        if count > max_skills:
            max_skills = count

    empty_ok = empty_pct <= config.max_empty_extraction_pct
    skills_ok = max_skills <= config.max_skills_per_listing
    passed = empty_ok and skills_ok

    observed = {"empty_pct": round(empty_pct, 1), "max_skills": max_skills}
    threshold = {
        "max_empty_extraction_pct": config.max_empty_extraction_pct,
        "max_skills_per_listing": config.max_skills_per_listing,
    }

    if passed:
        message = (
            f"{name}: PASS — empty={empty_pct:.1f}% "
            f"(<= {config.max_empty_extraction_pct}%), "
            f"max_skills={max_skills} (<= {config.max_skills_per_listing})."
        )
    else:
        problems = []
        if not empty_ok:
            problems.append(
                f"empty_pct={empty_pct:.1f} above "
                f"max_empty_extraction_pct={config.max_empty_extraction_pct} "
                f"(broken extractor)"
            )
        if not skills_ok:
            problems.append(
                f"max_skills={max_skills} above "
                f"max_skills_per_listing={config.max_skills_per_listing} "
                f"(sentence returned as skills)"
            )
        message = f"{name}: FAIL — " + "; ".join(problems)

    return CheckResult(name, passed, observed, threshold, message)


# ---------------------------------------------------------------------------
# 3. Gap sample size — catches ranking a rumour
# ---------------------------------------------------------------------------

def check_gap_sample_size(gaps: list[dict], config: "Config") -> CheckResult:
    """FAIL if the top-ranked gap was computed from fewer than
    min_gap_sample listings. Ranking a skill seen in one listing as the
    #1 gap is ranking a rumour.

    `gaps` is assumed ranked (top gap first), matching GapAnalyzer output.
    """
    name = "gap_sample_size"

    if not gaps:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0",
            threshold=config.min_gap_sample,
            message=f"{name}: PASS (trivially) — no gaps to check.",
        )

    top = gaps[0]
    sample = int(top.get("listings_blocked", 0))
    passed = sample >= config.min_gap_sample

    top_skill = top.get("skill", "?")
    if passed:
        message = (
            f"{name}: PASS — top gap '{top_skill}' from {sample} listing(s) "
            f"(>= {config.min_gap_sample})."
        )
    else:
        message = (
            f"{name}: FAIL — top gap '{top_skill}' computed from only "
            f"{sample} listing(s), below min_gap_sample={config.min_gap_sample} "
            f"(ranking a rumour)"
        )

    return CheckResult(name, passed, sample, config.min_gap_sample, message)


# ---------------------------------------------------------------------------
# 4. Freshness — catches stale data. `now` is a PARAMETER (testable).
# ---------------------------------------------------------------------------

def check_freshness(
    latest_fetch_at: datetime | None,
    config: "Config",
    now: datetime,
) -> CheckResult:
    """FAIL if the newest listing is older than max_data_age_days.

    `now` is passed in, never read from the clock, so this is deterministic
    and testable.
    """
    name = "freshness"

    if latest_fetch_at is None:
        return CheckResult(
            name=name,
            passed=False,
            observed="never",
            threshold=config.max_data_age_days,
            message=(
                f"{name}: FAIL — never fetched; no data exists "
                f"(max_data_age_days={config.max_data_age_days})"
            ),
        )

    age_days = (now - latest_fetch_at).total_seconds() / 86400.0
    passed = age_days <= config.max_data_age_days

    if passed:
        message = (
            f"{name}: PASS — newest listing {age_days:.1f} day(s) old "
            f"(<= {config.max_data_age_days})."
        )
    else:
        message = (
            f"{name}: FAIL — newest listing {age_days:.1f} day(s) old, "
            f"above max_data_age_days={config.max_data_age_days} (stale data)"
        )

    return CheckResult(name, passed, round(age_days, 2), config.max_data_age_days, message)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def run_all_checks(
    scores: list[int],
    facts_list: list[dict],
    gaps: list[dict],
    latest_fetch_at: datetime | None,
    config: "Config",
    now: datetime,
) -> Verdict:
    """Run every check. The verdict passes only if all checks pass.

    Pure aggregation — no side effects. The caller (Orchestrator) decides
    what to do with a failed verdict (rule 34).
    """
    results = [
        check_score_spread(scores, config),
        check_extraction_sanity(facts_list, config),
        check_gap_sample_size(gaps, config),
        check_freshness(latest_fetch_at, config, now),
    ]

    failed = [r for r in results if not r.passed]
    passed = not failed

    if passed:
        summary = f"verified: all {len(results)} checks passed"
    else:
        names = ", ".join(r.name for r in failed)
        summary = f"FAILED {len(failed)}/{len(results)} checks: {names}"

    return Verdict(passed=passed, failed_checks=failed, summary=summary)
