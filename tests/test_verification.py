"""Tests for edgedash/verification.py — pure checks, no clock/network/DB.

Each check has a passing case and a failing case; check_score_spread also
has the fewer-than-5-scores trivial-pass case.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from edgedash.config import Config
from edgedash.verification import (
    check_extraction_sanity,
    check_freshness,
    check_gap_sample_size,
    check_score_spread,
    run_all_checks,
)

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


# ---------------------------------------------------------------------------
# check_score_spread
# ---------------------------------------------------------------------------

def test_score_spread_pass():
    # Wide range and healthy stdev.
    scores = [20, 40, 55, 70, 90]
    r = check_score_spread(scores, _cfg())
    assert r.passed is True
    assert "PASS" in r.message


def test_score_spread_fail_bunched():
    # All clustered in a 4-point band → range and stdev both too low.
    scores = [50, 51, 52, 53, 54]
    r = check_score_spread(scores, _cfg())
    assert r.passed is False
    assert "score inflation" in r.message
    assert r.observed["range"] == 4


def test_score_spread_fewer_than_5_trivial_pass():
    scores = [50, 90, 10]  # only 3 scores
    r = check_score_spread(scores, _cfg())
    assert r.passed is True
    assert "trivially" in r.message
    assert "fewer than 5" in r.message


# ---------------------------------------------------------------------------
# check_extraction_sanity
# ---------------------------------------------------------------------------

def test_extraction_sanity_pass():
    facts = [
        {"required_skills": ["sld", "load list"]},
        {"required_skills": ["substation design"]},
        {"required_skills": ["earthing"]},
        {"required_skills": ["cable sizing"]},
        {"required_skills": []},  # 1 of 5 empty = 20%, at the limit (<=)
    ]
    r = check_extraction_sanity(facts, _cfg())
    assert r.passed is True


def test_extraction_sanity_fail_too_many_empty():
    facts = [
        {"required_skills": []},
        {"required_skills": []},
        {"required_skills": ["sld"]},
        {"required_skills": ["load list"]},
    ]  # 2 of 4 empty = 50% > 20%
    r = check_extraction_sanity(facts, _cfg())
    assert r.passed is False
    assert "broken extractor" in r.message


def test_extraction_sanity_fail_sentence_as_skills():
    # One listing with 25 "skills" — a sentence split into tokens.
    facts = [
        {"required_skills": ["sld", "load list"]},
        {"required_skills": [f"word{i}" for i in range(25)]},
    ]
    r = check_extraction_sanity(facts, _cfg())
    assert r.passed is False
    assert "sentence returned as skills" in r.message
    assert r.observed["max_skills"] == 25


# ---------------------------------------------------------------------------
# check_gap_sample_size
# ---------------------------------------------------------------------------

def test_gap_sample_size_pass():
    gaps = [
        {"skill": "sld", "listings_blocked": 7},
        {"skill": "etap", "listings_blocked": 2},
    ]
    r = check_gap_sample_size(gaps, _cfg())
    assert r.passed is True


def test_gap_sample_size_fail_rumour():
    gaps = [
        {"skill": "sld", "listings_blocked": 1},  # top gap from 1 listing
        {"skill": "etap", "listings_blocked": 1},
    ]
    r = check_gap_sample_size(gaps, _cfg())
    assert r.passed is False
    assert "ranking a rumour" in r.message
    assert r.observed == 1


# ---------------------------------------------------------------------------
# check_freshness  (now is a parameter)
# ---------------------------------------------------------------------------

def test_freshness_pass():
    latest = _NOW - timedelta(days=1)
    r = check_freshness(latest, _cfg(), _NOW)
    assert r.passed is True


def test_freshness_fail_stale():
    latest = _NOW - timedelta(days=5)  # older than max_data_age_days=3
    r = check_freshness(latest, _cfg(), _NOW)
    assert r.passed is False
    assert "stale data" in r.message


def test_freshness_fail_never_fetched():
    r = check_freshness(None, _cfg(), _NOW)
    assert r.passed is False
    assert r.observed == "never"
    assert "never" in r.message.lower()


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------

def test_run_all_checks_pass():
    scores = [20, 40, 55, 70, 90]
    facts = [{"required_skills": ["sld"]} for _ in range(5)]
    gaps = [{"skill": "sld", "listings_blocked": 5}]
    latest = _NOW - timedelta(days=1)

    verdict = run_all_checks(scores, facts, gaps, latest, _cfg(), _NOW)
    assert verdict.passed is True
    assert verdict.failed_checks == []
    assert "all 4 checks passed" in verdict.summary


def test_run_all_checks_fail_collects_failures():
    scores = [50, 51, 52, 53, 54]           # spread fails
    facts = [{"required_skills": []} for _ in range(5)]  # extraction fails
    gaps = [{"skill": "sld", "listings_blocked": 1}]      # sample fails
    latest = _NOW - timedelta(days=10)       # freshness fails

    verdict = run_all_checks(scores, facts, gaps, latest, _cfg(), _NOW)
    assert verdict.passed is False
    assert len(verdict.failed_checks) == 4
    assert "FAILED 4/4" in verdict.summary
