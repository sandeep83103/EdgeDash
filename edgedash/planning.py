"""Deterministic planning — pure function of (state, config). No I/O, no LLM.

build_plan() decides which agents run this cycle based purely on the
SystemState snapshot and config thresholds. Every agent appears in the Plan
either as a task to run or as skipped-with-a-reason (rule 31) — never
silently absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from edgedash.config import Config
from edgedash.state import SystemState

_FETCH = "Fetcher"
_SCORE = "Scorer"
_ANALYSE = "GapAnalyzer"


@dataclass
class Task:
    agent_name: str
    goal: str
    stop_conditions: dict[str, int]
    reason: str
    skipped: bool = False


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)

    @property
    def to_run(self) -> list[Task]:
        return [t for t in self.tasks if not t.skipped]

    def render(self) -> str:
        """One line per agent: RUN/SKIP, goal, stop conditions, reason."""
        lines: list[str] = []
        header = "Plan:"
        lines.append(header)
        for t in self.tasks:
            marker = "SKIP" if t.skipped else "RUN "
            if t.skipped:
                lines.append(f"  [{marker}] {t.agent_name:<12} — {t.reason}")
            else:
                stops = ", ".join(f"{k}={v}" for k, v in t.stop_conditions.items())
                lines.append(
                    f"  [{marker}] {t.agent_name:<12} {t.goal}"
                )
                lines.append(f"           stop: {stops}")
                lines.append(f"           reason: {t.reason}")
        run_count = len(self.to_run)
        lines.append(f"  → {run_count} agent(s) to run, "
                     f"{len(self.tasks) - run_count} skipped")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------

def build_plan(state: SystemState, config: Config) -> Plan:
    """Return an ordered Plan. Pure function — no side effects."""
    return Plan(tasks=[
        _plan_fetch(state, config),
        _plan_score(state, config),
        _plan_analyse(state, config),
    ])


def _plan_fetch(state: SystemState, config: Config) -> Task:
    stops = {
        "max_pages":    config.fetch_max_pages,
        "max_listings": config.fetch_max_listings,
    }
    interval = config.fetch_interval_hours

    if state.hours_since_fetch is None:
        return Task(
            agent_name=_FETCH,
            goal="fetch listings from all enabled sources",
            stop_conditions=stops,
            reason="last_fetch_at=never",
        )

    if state.hours_since_fetch >= interval:
        return Task(
            agent_name=_FETCH,
            goal="fetch listings from all enabled sources",
            stop_conditions=stops,
            reason=(
                f"hours_since_fetch={state.hours_since_fetch:.1f} "
                f">= fetch_interval_hours={interval}"
            ),
        )

    return Task(
        agent_name=_FETCH,
        goal="fetch listings from all enabled sources",
        stop_conditions=stops,
        reason=(
            f"skipped: hours_since_fetch={state.hours_since_fetch:.1f} "
            f"< fetch_interval_hours={interval}"
        ),
        skipped=True,
    )


def _plan_score(state: SystemState, config: Config) -> Task:
    stops = {
        "max_items":   config.scoring_batch_size,
        "max_seconds": config.score_max_seconds,
    }
    if state.unscored_count > 0:
        return Task(
            agent_name=_SCORE,
            goal="score unscored listings against profile",
            stop_conditions=stops,
            reason=f"unscored_count={state.unscored_count}",
        )
    return Task(
        agent_name=_SCORE,
        goal="score unscored listings against profile",
        stop_conditions=stops,
        reason="skipped: unscored_count=0",
        skipped=True,
    )


def _plan_analyse(state: SystemState, config: Config) -> Task:
    stops = {"max_seconds": config.analyse_max_seconds}

    if state.gaps_computed_at is None:
        return Task(
            agent_name=_ANALYSE,
            goal="compute skill-gap snapshot",
            stop_conditions=stops,
            reason="gaps_computed_at=never",
        )

    if state.gaps_stale:
        return Task(
            agent_name=_ANALYSE,
            goal="compute skill-gap snapshot",
            stop_conditions=stops,
            reason="gaps_stale=True (new scores since last snapshot)",
        )

    return Task(
        agent_name=_ANALYSE,
        goal="compute skill-gap snapshot",
        stop_conditions=stops,
        reason="skipped: gaps_stale=False",
        skipped=True,
    )
