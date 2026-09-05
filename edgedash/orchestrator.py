"""Orchestrator — state-driven cycle controller (rules 28-33).

Flow:
    1. read_state()          — cheap snapshot of the system (state.py)
    2. build_plan()          — decide which agents run (planning.py)
    3. print + log the plan  — before executing anything (rule 31)
    4. execute plan.to_run   — in order, each wrapped in try/except (rule 32)
    5. write ONE summary row — plan, ran, skipped, durations, outcome (rule 33)

The Orchestrator does no fetching, scoring, or analysis (rule 30). It resolves
agents by name from the registry and passes each Task's goal and
stop_conditions down (rule 29). It never runs a fixed sequence (rule 28).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from edgedash.agents.base import Agent, AgentResult
from edgedash.agents.fetcher import Fetcher
from edgedash.agents.gap_analyzer import GapAnalyzer
from edgedash.agents.mock_fetcher import MockFetcher
from edgedash.agents.scorer import Scorer
from edgedash.agents.verifier import Verifier
from edgedash.config import Config
from edgedash.planning import Plan, Task, build_plan
from edgedash.state import SystemState, read_state
from edgedash.storage import clear_all_scores, init_db, log_cycle
from edgedash.verification import Verdict

# ---------------------------------------------------------------------------
# Agent registry — unchanged. The Orchestrator resolves agents by name and
# knows nothing else about them.
# ---------------------------------------------------------------------------

_REGISTRY: list[Agent] = [
    Fetcher(),
    MockFetcher(),
    Scorer(),
    GapAnalyzer(),
    Verifier(),
]

# Cycle outcomes (rule 33 / rule 28).
COMPLETE = "complete"
PARTIAL = "partial"
NOTHING_TO_DO = "nothing_to_do"
DRY_RUN = "dry_run"
DEGRADED = "degraded"      # verification failed after the one allowed retry (rule 36)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_cycle(
    config: Config,
    *,
    dry_run: bool = False,
    force_agents: list[str] | None = None,
    explain: bool = False,
) -> str:
    """Run one state-driven cycle. Returns the outcome string.

    Outcomes: "complete" | "partial" | "nothing_to_do" | "dry_run".
    "nothing_to_do" is a SUCCESS (rule 28), not an error.

    Operational flags (none alter build_plan's rules — a pure function):
      dry_run       — read state, build+print plan, then exit without executing.
      force_agents  — agent names to add to the plan even if state would skip
                      them. Appended AFTER build_plan; planning logic untouched.
      explain       — print every SystemState value next to the decision it drove.
    """
    force_agents = force_agents or []
    db = config.abs_db_path
    now = datetime.now(timezone.utc)

    _banner("EdgeDash — starting cycle" + (" (DRY RUN)" if dry_run else ""))

    # A dry run must not write anything (rule: no writes, no API calls).
    # init_db does CREATE TABLE IF NOT EXISTS, which would create the .db file
    # on a fresh setup — so on a dry run we only init an already-existing DB.
    if dry_run and not db.exists():
        _print_dim(f"  DB does not exist yet: {db} (dry run — not creating it)")
        state = _empty_state(now)
    else:
        init_db(db)
        _print_dim(f"  DB ready: {db}")
        # ── 1. Read state ────────────────────────────────────────────────────
        state = read_state(config, now)
    _print_state(state)

    # ── 2. Build plan (pure) ─────────────────────────────────────────────────
    plan = build_plan(state, config)

    # ── 2a. Apply operator overrides AFTER planning (does not touch rules) ───
    forced_names = _apply_forced(plan, force_agents, config)

    # ── 2b. Explain: show state values against the decisions they drove ──────
    if explain:
        _print_explain(state, plan, config)

    # ── 3. Print + log the plan BEFORE executing anything (rule 31) ──────────
    print()
    print(plan.render())

    if forced_names:
        _print_dim(
            "\n  ⚠  PLAN MANUALLY OVERRIDDEN by operator — forced: "
            + ", ".join(forced_names)
        )

    # ── Dry run: stop here. No writes, no API calls, exit 0. ─────────────────
    if dry_run:
        _banner("Dry run — plan shown above, nothing executed. (success)")
        return DRY_RUN

    # ── 4. Execute plan.to_run in order (rule 32) ────────────────────────────
    to_run = plan.to_run
    if not to_run:
        outcome = NOTHING_TO_DO
        _write_summary(db, plan, results=[], outcome=outcome, now=now,
                       forced=forced_names)
        _banner("Nothing to do this cycle — all agents skipped. (success)")
        return outcome

    _header("Running agents")
    results: list[_TaskResult] = []
    any_failed = False
    ran_scorer = False

    for task in to_run:
        tr = _execute_task(task, config, db)
        results.append(tr)
        if tr.status == "fail":
            any_failed = True
        if task.agent_name == "Scorer":
            ran_scorer = True

    # ── 5. Verification + at most ONE retry (rules 34-36) ────────────────────
    verdict, retry_count, verify_results = _verify_with_retry(
        config, db, plan, ran_scorer
    )
    results.extend(verify_results)

    # ── 6. Outcome + single summary row (rule 33) ────────────────────────────
    if verdict is not None and not verdict.passed:
        # Verification failed after the allowed retry → degraded, stop, no raise.
        outcome = DEGRADED
    elif any_failed:
        outcome = PARTIAL
    else:
        outcome = COMPLETE

    _write_summary(
        db, plan, results, outcome, now,
        forced=forced_names, verdict=verdict, retry_count=retry_count,
    )
    _print_summary(results, plan, outcome, forced_names, verdict, retry_count)

    return outcome


# ---------------------------------------------------------------------------
# Verification + single retry (rule 36)
# ---------------------------------------------------------------------------

def _verify_with_retry(
    config: Config,
    db: Path,
    plan: Plan,
    ran_scorer: bool,
) -> tuple[Verdict | None, int, list["_TaskResult"]]:
    """Run the Verifier; on failure retry the failing agent ONCE, verify again.

    Returns (final_verdict, retry_count, task_results_for_summary).
    final_verdict is None only when there was nothing to verify.

    Rule 36: at most one retry for the whole cycle. If the second verdict
    still fails, we stop and let the caller mark the cycle degraded — we
    never loop and never raise.
    """
    verifier = Verifier()
    task_results: list[_TaskResult] = []

    _header("Verification")
    first = _run_verifier(verifier, config, db)
    task_results.append(first.task_result)

    if first.verdict.passed:
        return first.verdict, 0, task_results

    # ── Failed. Decide whether we can adjust and retry (rule 36). ────────────
    adjusted = _apply_adjusted_context(first.verdict, config, db, ran_scorer)
    if adjusted is None:
        # No adjustment available for this failure → one verdict, no retry.
        _print_dim("  ⚠  no adjusted-context retry available for this failure")
        return first.verdict, 0, task_results

    retry_agent_name, retry_task = adjusted
    _print_dim(
        f"  ↻  RETRY (1/1): re-running {retry_agent_name} with adjusted context"
    )
    retry_tr = _execute_task(retry_task, config, db)
    task_results.append(retry_tr)

    # Verify exactly once more.
    _header("Verification (after retry)")
    second = _run_verifier(verifier, config, db)
    task_results.append(second.task_result)

    return second.verdict, 1, task_results


class _VerifyRun:
    def __init__(self, verdict: Verdict, task_result: "_TaskResult") -> None:
        self.verdict = verdict
        self.task_result = task_result


def _run_verifier(verifier: Verifier, config: Config, db: Path) -> _VerifyRun:
    """Run the Verifier as a task and return its verdict + task result."""
    started = datetime.now(timezone.utc)
    verdict = verifier.verify(config, db)
    result = verifier.run(config, db, {})   # produces the AgentResult + notes
    finished = datetime.now(timezone.utc)
    ms = _ms_between(started, finished)

    icon = "✓" if verdict.passed else "✗"
    print(f"  {icon}  {result.notes}  [{ms}ms]")

    tr = _TaskResult("Verifier", "pass" if verdict.passed else "fail",
                     ms, result.notes, 0)
    return _VerifyRun(verdict, tr)


def _apply_adjusted_context(
    verdict: Verdict,
    config: Config,
    db: Path,
    ran_scorer: bool,
) -> tuple[str, Task] | None:
    """Prepare the single retry for the failing check (rule 36).

    Currently one adjustment is defined:
      score_spread failed → clear scores and re-run the Scorer with
      widen_distribution set, so it contrast-stretches the batch.

    Returns (agent_name, task) to run, or None if no adjustment applies.
    """
    failed_names = {c.name for c in verdict.failed_checks}

    if "score_spread" in failed_names and ran_scorer:
        # Adjusted context: the just-written scores were too bunched. Clear
        # them so the Scorer re-processes the same listings, and pass the
        # widen flag so it stretches the distribution (see Scorer._widen_scores).
        cleared = clear_all_scores(db)
        _print_dim(f"  ↻  cleared {cleared} score(s) for widen retry")
        retry_task = Task(
            agent_name="Scorer",
            goal="re-score with a widened distribution (verification retry)",
            stop_conditions={
                "max_items": config.scoring_batch_size,
                "max_seconds": config.score_max_seconds,
                "widen_distribution": 1,
            },
            reason="retry: score_spread failed verification",
        )
        return "Scorer", retry_task

    return None


# ---------------------------------------------------------------------------
# Dry-run empty state (used only when the DB doesn't exist yet, so a dry run
# never has to create the file). Mirrors a brand-new system: nothing done yet.
# ---------------------------------------------------------------------------

def _empty_state(now: datetime) -> SystemState:
    return SystemState(
        now=now,
        last_fetch_at=None,
        hours_since_fetch=None,
        unscored_count=0,
        gaps_computed_at=None,
        gaps_stale=True,
        last_cycle_verdict=None,
        last_cycle_at=None,
    )


# ---------------------------------------------------------------------------
# Operator overrides (--force). Applied AFTER build_plan; rules untouched.
# ---------------------------------------------------------------------------

def _apply_forced(
    plan: Plan,
    force_agents: list[str],
    config: Config,
) -> list[str]:
    """Add each forced agent to the plan's run set.

    A forced agent that the planner skipped is flipped to run with reason
    "forced by operator". A forced agent already running is left as-is.
    An unknown agent name raises — better to fail loudly than silently
    force nothing.

    Returns the list of agent names that were actually forced (flipped from
    skipped to run), for the override warning and the summary row.
    """
    if not force_agents:
        return []

    known = {t.agent_name for t in plan.tasks}
    forced: list[str] = []

    for name in force_agents:
        if name not in known:
            raise KeyError(
                f"--force {name!r}: unknown agent. "
                f"Known agents: {', '.join(sorted(known))}."
            )
        for task in plan.tasks:
            if task.agent_name == name and task.skipped:
                task.skipped = False
                task.reason = "forced by operator"
                forced.append(name)

    return forced


def _print_explain(state, plan: Plan, config: Config) -> None:
    """Print every SystemState value next to the decision it drove (rule: debug).

    This is the "why did it skip that?" tool. Each line pairs a raw state
    value (with its timestamp where relevant) to the planner's resulting
    reason for the agent that value governs.
    """
    _header("Explain — state values → decisions")

    reason_by_agent = {t.agent_name: t.reason for t in plan.tasks}

    def _ts(dt) -> str:
        return "never" if dt is None else dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    print("  now:")
    print(f"    value    : {_ts(state.now)}")
    print()

    print("  Fetcher  ← last_fetch_at / hours_since_fetch")
    print(f"    last_fetch_at     : {_ts(state.last_fetch_at)}")
    hrs = "n/a" if state.hours_since_fetch is None else f"{state.hours_since_fetch:.2f}"
    print(f"    hours_since_fetch : {hrs}")
    print(f"    fetch_interval_hrs: {config.fetch_interval_hours}")
    print(f"    → decision        : {reason_by_agent.get('Fetcher', '(not planned)')}")
    print()

    print("  Scorer   ← unscored_count")
    print(f"    unscored_count    : {state.unscored_count}")
    print(f"    scoring_batch_size: {config.scoring_batch_size}")
    print(f"    → decision        : {reason_by_agent.get('Scorer', '(not planned)')}")
    print()

    print("  GapAnalyzer ← gaps_computed_at / gaps_stale")
    print(f"    gaps_computed_at  : {_ts(state.gaps_computed_at)}")
    print(f"    gaps_stale        : {state.gaps_stale}")
    print(f"    → decision        : {reason_by_agent.get('GapAnalyzer', '(not planned)')}")
    print()

    print("  Context (not a direct trigger)")
    print(f"    last_cycle_verdict: {state.last_cycle_verdict or 'none'}")
    print(f"    last_cycle_at     : {_ts(state.last_cycle_at)}")


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

class _TaskResult:
    """What happened when one planned task ran."""

    def __init__(
        self,
        agent_name: str,
        status: str,            # "pass" | "fail"
        duration_ms: int,
        notes: str,
        records_touched: int,
    ) -> None:
        self.agent_name = agent_name
        self.status = status
        self.duration_ms = duration_ms
        self.notes = notes
        self.records_touched = records_touched


def _execute_task(task: Task, config: Config, db: Path) -> _TaskResult:
    """Run one task. A failure is logged and contained (rule 32)."""
    agent = _resolve_agent(task.agent_name, config)
    _print_dim(f"\n  → {task.agent_name}  (goal: {task.goal})")

    started = datetime.now(timezone.utc)
    try:
        result: AgentResult = agent.run(config, db, task.stop_conditions)
    except Exception as exc:
        finished = datetime.now(timezone.utc)
        ms = _ms_between(started, finished)
        notes = f"{type(exc).__name__}: {exc}"
        print(f"     ✗  FAILED — {notes}  [{ms}ms]")
        # Contained failure: the cycle continues with remaining tasks.
        return _TaskResult(task.agent_name, "fail", ms, notes, 0)

    finished = datetime.now(timezone.utc)
    ms = _ms_between(started, finished)
    status = "pass" if result.status == "ok" else "fail"
    icon = "✓" if status == "pass" else "✗"
    print(f"     {icon}  {result.notes}  [{ms}ms]")

    return _TaskResult(
        task.agent_name, status, ms, result.notes, result.records_touched
    )


def _resolve_agent(name: str, config: Config) -> Agent:
    """Resolve a plan agent-name to a registry instance.

    The only indirection: when the plan asks for "Fetcher" and the config
    prefers the offline mock, resolve to MockFetcher. The registry and the
    planner both stay untouched.
    """
    if name == "Fetcher" and config.use_mock_fetcher:
        name = "MockFetcher"
    for agent in _REGISTRY:
        if agent.name == name:
            return agent
    raise KeyError(f"Agent '{name}' not found in registry.")


# ---------------------------------------------------------------------------
# Cycle summary — exactly one row (rule 33)
# ---------------------------------------------------------------------------

def _write_summary(
    db: Path,
    plan: Plan,
    results: list["_TaskResult"],
    outcome: str,
    now: datetime,
    forced: list[str] | None = None,
    verdict: Verdict | None = None,
    retry_count: int = 0,
) -> None:
    """Write exactly one cycle_log summary row capturing the whole cycle."""
    ran = [
        {
            "agent": r.agent_name,
            "status": r.status,
            "duration_ms": r.duration_ms,
            "records": r.records_touched,
        }
        for r in results
    ]
    skipped = [
        {"agent": t.agent_name, "reason": t.reason}
        for t in plan.tasks if t.skipped
    ]

    # Verification record (rules 33, 37, 38): verdict, which checks failed,
    # and the retry count so the whole cycle is diagnosable from this one row.
    verdict_passed = bool(verdict.passed) if verdict is not None else None
    failed_checks = [
        {"check": c.name, "observed": c.observed, "threshold": c.threshold}
        for c in (verdict.failed_checks if verdict else [])
    ]

    summary = {
        "outcome": outcome,
        "ran": ran,
        "skipped": skipped,
        # Operator overrides recorded so they're visible in the log later.
        "forced": forced or [],
        # Verification outcome. verdict_passed gates the dashboard (rule 38).
        "verdict_passed": verdict_passed,
        "verdict_summary": verdict.summary if verdict else None,
        "failed_checks": failed_checks,
        "retry_count": retry_count,
    }

    total_records = sum(r.records_touched for r in results)
    # cycle_log.status only accepts pass|fail. A cycle counts as "pass" only
    # when it completed AND verification passed (or there was nothing to
    # verify). partial and degraded both map to "fail".
    if outcome in (PARTIAL, DEGRADED):
        log_status = "fail"
    else:
        log_status = "pass"

    finished = datetime.now(timezone.utc)
    log_cycle(
        db,
        agent="cycle",
        started_at=now,
        finished_at=finished,
        records_touched=total_records,
        status=log_status,
        notes=json.dumps(summary),
    )


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

_W = 66


def _banner(text: str) -> None:
    print()
    print("═" * _W)
    print(f"  {text}")
    print("═" * _W)


def _header(text: str) -> None:
    print()
    print(f"── {text} " + "─" * max(0, _W - len(text) - 4))


def _print_dim(text: str) -> None:
    print(text)


def _print_state(state) -> None:
    _header("Current state")
    fetch = "never" if state.last_fetch_at is None else \
        state.last_fetch_at.strftime("%Y-%m-%d %H:%M UTC")
    hrs = "n/a" if state.hours_since_fetch is None else f"{state.hours_since_fetch:.1f}h ago"
    print(f"  Last fetch     : {fetch}  ({hrs})")
    print(f"  Unscored rows  : {state.unscored_count}")
    gaps = "never" if state.gaps_computed_at is None else \
        state.gaps_computed_at.strftime("%Y-%m-%d %H:%M UTC")
    print(f"  Gaps computed  : {gaps}  (stale={state.gaps_stale})")
    print(f"  Last cycle     : {state.last_cycle_verdict or 'none'}")


def _print_summary(
    results: list["_TaskResult"],
    plan: Plan,
    outcome: str,
    forced: list[str] | None = None,
    verdict: Verdict | None = None,
    retry_count: int = 0,
) -> None:
    _header("Cycle summary")

    col_agent = 16
    col_status = 8
    col_dur = 9

    print(
        f"  {'Agent':<{col_agent}}{'Status':<{col_status}}{'Duration':>{col_dur}}  Notes"
    )
    print("  " + "─" * (_W - 2))
    for r in results:
        icon = "pass" if r.status == "pass" else "FAIL"
        note = r.notes if len(r.notes) <= 30 else r.notes[:29] + "…"
        print(
            f"  {r.agent_name:<{col_agent}}{icon:<{col_status}}"
            f"{r.duration_ms:>{col_dur - 2}}ms  {note}"
        )

    skipped = [t for t in plan.tasks if t.skipped]
    for t in skipped:
        print(f"  {t.agent_name:<{col_agent}}{'skip':<{col_status}}{'—':>{col_dur}}  {t.reason}")

    print("  " + "─" * (_W - 2))
    if forced:
        print(f"  Overridden: forced {', '.join(forced)} (operator)")

    if verdict is not None:
        vlabel = "PASS ✓" if verdict.passed else "FAIL ✗"
        print(f"  Verdict: {vlabel} — {verdict.summary}  (retries: {retry_count})")

    label = {
        COMPLETE: "COMPLETE ✓",
        PARTIAL: "PARTIAL ✗ (a task failed; others continued)",
        NOTHING_TO_DO: "NOTHING TO DO ✓",
        DEGRADED: "DEGRADED ✗ (verification failed after retry; last good data kept)",
    }[outcome]
    print(f"  Outcome: {label}")
    print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() * 1000)
