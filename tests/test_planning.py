"""Tests for edgedash/planning.build_plan — pure function, no DB, no I/O.

SystemState and Config are constructed directly. The four required cases:
  1. everything stale       → fetch, score, analyse all RUN
  2. nothing to do          → all three SKIP
  3. only unscored listings → score RUN, fetch & analyse SKIP
  4. gaps stale, nothing unscored → analyse RUN, fetch & score SKIP
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from edgedash.config import Config
from edgedash.planning import build_plan
from edgedash.state import SystemState

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(**overrides) -> Config:
    base = dict(
        target_role="Lead Electrical Design Engineer",
        target_city="Remote",
        keywords=[],
        my_skills=[],
        experience_years=21,
        db_path="edgedash.db",
        min_fit_score=50,
        sources=["arbeitnow"],
        use_mock_fetcher=False,
        llm_provider="gemini",
        llm_model="gemini-3.6-flash",
        scoring_batch_size=25,
        target_seniority="lead",
        weight_skill_match=0.45,
        weight_seniority_fit=0.25,
        weight_location_fit=0.15,
        weight_recency=0.15,
        skill_aliases={},
        fetch_interval_hours=6,
        fetch_max_pages=5,
        fetch_max_listings=200,
        score_max_seconds=120,
        analyse_max_seconds=60,
        min_score_spread=10,
        min_score_stdev=5,
        max_empty_extraction_pct=20,
        max_skills_per_listing=20,
        min_gap_sample=3,
        max_data_age_days=3,
        daily_question_cap=200,
        score_spread_gain=1.8,
    )
    base.update(overrides)
    return Config(**base)


def _state(**overrides) -> SystemState:
    base = dict(
        now=_NOW,
        last_fetch_at=_NOW - timedelta(hours=1),
        hours_since_fetch=1.0,
        unscored_count=0,
        gaps_computed_at=_NOW - timedelta(hours=1),
        gaps_stale=False,
        last_cycle_verdict="pass",
        last_cycle_at=_NOW - timedelta(hours=1),
    )
    base.update(overrides)
    return SystemState(**base)


def _by_name(plan, name):
    return next(t for t in plan.tasks if t.agent_name == name)


# ---------------------------------------------------------------------------
# Case 1: everything stale — all three run
# ---------------------------------------------------------------------------

def test_everything_stale_all_run():
    state = _state(
        hours_since_fetch=12.0,      # >= interval → fetch
        unscored_count=41,           # > 0 → score
        gaps_stale=True,             # → analyse
        gaps_computed_at=_NOW - timedelta(hours=12),
    )
    plan = build_plan(state, _cfg())

    assert len(plan.to_run) == 3
    assert not _by_name(plan, "Fetcher").skipped
    assert not _by_name(plan, "Scorer").skipped
    assert not _by_name(plan, "GapAnalyzer").skipped
    assert "hours_since_fetch=12.0" in _by_name(plan, "Fetcher").reason
    assert "unscored_count=41" in _by_name(plan, "Scorer").reason


# ---------------------------------------------------------------------------
# Case 2: nothing to do — all three skipped, but PRESENT with reasons
# ---------------------------------------------------------------------------

def test_nothing_to_do_all_skipped():
    state = _state(
        hours_since_fetch=1.0,       # < interval → skip fetch
        unscored_count=0,            # → skip score
        gaps_stale=False,            # → skip analyse
    )
    plan = build_plan(state, _cfg())

    assert len(plan.to_run) == 0
    assert len(plan.tasks) == 3     # rule 31: all present, none absent
    assert all(t.skipped for t in plan.tasks)
    assert "skipped: hours_since_fetch=1.0" in _by_name(plan, "Fetcher").reason
    assert "skipped: unscored_count=0" in _by_name(plan, "Scorer").reason
    assert "skipped: gaps_stale=False" in _by_name(plan, "GapAnalyzer").reason


# ---------------------------------------------------------------------------
# Case 3: only unscored listings — score runs, others skip
# ---------------------------------------------------------------------------

def test_only_unscored_listings():
    state = _state(
        hours_since_fetch=1.0,       # skip fetch
        unscored_count=7,            # run score
        gaps_stale=False,            # skip analyse
    )
    plan = build_plan(state, _cfg())

    assert _by_name(plan, "Fetcher").skipped
    assert not _by_name(plan, "Scorer").skipped
    assert _by_name(plan, "GapAnalyzer").skipped
    assert [t.agent_name for t in plan.to_run] == ["Scorer"]
    assert "unscored_count=7" in _by_name(plan, "Scorer").reason


# ---------------------------------------------------------------------------
# Case 4: gaps stale but nothing unscored — analyse runs, others skip
# ---------------------------------------------------------------------------

def test_gaps_stale_nothing_unscored():
    state = _state(
        hours_since_fetch=2.0,       # skip fetch
        unscored_count=0,            # skip score
        gaps_stale=True,             # run analyse
        gaps_computed_at=_NOW - timedelta(hours=5),
    )
    plan = build_plan(state, _cfg())

    assert _by_name(plan, "Fetcher").skipped
    assert _by_name(plan, "Scorer").skipped
    assert not _by_name(plan, "GapAnalyzer").skipped
    assert [t.agent_name for t in plan.to_run] == ["GapAnalyzer"]
    assert "gaps_stale=True" in _by_name(plan, "GapAnalyzer").reason


# ---------------------------------------------------------------------------
# Edge: never fetched, never analysed
# ---------------------------------------------------------------------------

def test_never_fetched_runs_fetch():
    state = _state(
        last_fetch_at=None,
        hours_since_fetch=None,
        unscored_count=0,
        gaps_computed_at=None,
        gaps_stale=True,
    )
    plan = build_plan(state, _cfg())

    assert not _by_name(plan, "Fetcher").skipped
    assert "last_fetch_at=never" in _by_name(plan, "Fetcher").reason
    assert not _by_name(plan, "GapAnalyzer").skipped
    assert "gaps_computed_at=never" in _by_name(plan, "GapAnalyzer").reason


# ---------------------------------------------------------------------------
# Stop conditions come from config
# ---------------------------------------------------------------------------

def test_stop_conditions_from_config():
    cfg = _cfg(
        fetch_max_pages=3,
        fetch_max_listings=99,
        scoring_batch_size=10,
        score_max_seconds=45,
        analyse_max_seconds=30,
    )
    state = _state(hours_since_fetch=12.0, unscored_count=5, gaps_stale=True)
    plan = build_plan(state, cfg)

    fetch = _by_name(plan, "Fetcher")
    assert fetch.stop_conditions == {"max_pages": 3, "max_listings": 99}

    score = _by_name(plan, "Scorer")
    assert score.stop_conditions == {"max_items": 10, "max_seconds": 45}

    analyse = _by_name(plan, "GapAnalyzer")
    assert analyse.stop_conditions == {"max_seconds": 30}


# ---------------------------------------------------------------------------
# render() shows skipped agents (rule 31) and is non-empty
# ---------------------------------------------------------------------------

def test_render_includes_skipped():
    state = _state(hours_since_fetch=1.0, unscored_count=0, gaps_stale=False)
    out = build_plan(state, _cfg()).render()

    assert "Fetcher" in out
    assert "Scorer" in out
    assert "GapAnalyzer" in out
    assert "SKIP" in out
    assert "3 skipped" in out
