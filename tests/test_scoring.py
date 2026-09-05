"""Pytest tests for edgedash/scoring.py.

score_listing() is a pure function — no DB, no network, no mocking needed.
Six cases as specified, plus a few edge-condition extras for free.

Domain: Lead Electrical Design Engineer (oil & gas / EPC).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from edgedash.config import Config
from edgedash.scoring import build_reason, score_listing


# ---------------------------------------------------------------------------
# Minimal Config fixture — constructed directly to avoid touching config.yaml
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> Config:
    base = dict(
        target_role="Lead Electrical Design Engineer",
        target_city="Remote",
        keywords=["Detail Design", "FEED"],
        my_skills=["sld", "load list", "power layouts", "substation design"],
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


def _listing(**overrides) -> dict:
    base = dict(
        id="abc123",
        title="Lead Electrical Design Engineer",
        company="Wood",
        location="Remote",
        url="https://example.com/job/1",
        description="A job",
        source="test",
        posted_at="2026-09-05T00:00:00+00:00",
        fetched_at="2026-09-05T00:00:00+00:00",
    )
    base.update(overrides)
    return base


def _facts(**overrides) -> dict:
    base = dict(
        required_skills=["sld", "load list", "power layouts"],
        nice_to_have=["etap"],
        seniority="lead",
        years_required=None,
        remote_ok=None,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Case 1: Perfect match — all required skills, exact seniority, in city
# ---------------------------------------------------------------------------

def test_perfect_match():
    cfg = _cfg()
    listing = _listing(location="Remote", posted_at="2026-09-05T00:00:00+00:00")
    facts = _facts(
        required_skills=["sld", "load list", "power layouts"],
        seniority="lead",
        remote_ok=True,
    )
    result = score_listing(listing, facts, cfg)

    assert result["score"] >= 85, f"Expected high score, got {result['score']}"
    assert result["components"]["skill_match"] == 1.0
    assert result["components"]["seniority_fit"] == 1.0
    assert "3/3 required skills" in result["reason"]
    assert "seniority fits" in result["reason"]


# ---------------------------------------------------------------------------
# Case 2: Zero match — none of the required skills are in my profile
# ---------------------------------------------------------------------------

def test_zero_skill_match():
    cfg = _cfg(my_skills=["autocad", "excel"])
    listing = _listing()
    facts = _facts(required_skills=["plc programming", "scada", "instrumentation"])
    result = score_listing(listing, facts, cfg)

    # skill_match component must be exactly 0.0
    assert result["components"]["skill_match"] == 0.0
    # Score is lower than a perfect-match result even if other components carry it
    assert result["score"] < score_listing(
        _listing(),
        _facts(required_skills=["sld", "load list", "power layouts"]),
        _cfg(),
    )["score"]
    assert "0/3 required skills" in result["reason"]
    assert "gap:" in result["reason"]
    assert "plc programming" in result["reason"]


# ---------------------------------------------------------------------------
# Case 3: Empty required_skills — must not divide by zero; score is neutral
# ---------------------------------------------------------------------------

def test_empty_required_skills():
    cfg = _cfg()
    listing = _listing()
    facts = _facts(required_skills=[], nice_to_have=[])
    result = score_listing(listing, facts, cfg)

    # Should not raise, score should be in valid range
    assert 0 <= result["score"] <= 100
    assert result["components"]["skill_match"] == pytest.approx(0.7, abs=0.01)
    assert "no required skills listed" in result["reason"]


# ---------------------------------------------------------------------------
# Case 4: null posted_at — must not crash; recency is neutral (0.5)
# ---------------------------------------------------------------------------

def test_null_posted_at():
    cfg = _cfg()
    listing = _listing(posted_at=None)
    facts = _facts()
    result = score_listing(listing, facts, cfg)

    assert 0 <= result["score"] <= 100
    assert result["components"]["recency"] == pytest.approx(0.5, abs=0.01)
    assert "posting date unknown" in result["reason"]


# ---------------------------------------------------------------------------
# Case 5: null remote_ok — location falls back to city match check
# ---------------------------------------------------------------------------

def test_null_remote_ok_city_match():
    cfg = _cfg(target_city="Aberdeen")
    listing = _listing(location="Aberdeen")
    facts = _facts(remote_ok=None)
    result = score_listing(listing, facts, cfg)

    # City match should give 1.0 location_fit
    assert result["components"]["location_fit"] == pytest.approx(1.0)
    assert "in Aberdeen" in result["reason"]


def test_null_remote_ok_unknown_location():
    cfg = _cfg(target_city="Aberdeen")
    listing = _listing(location=None)
    facts = _facts(remote_ok=None)
    result = score_listing(listing, facts, cfg)

    assert result["components"]["location_fit"] == pytest.approx(0.5)
    assert "location unknown" in result["reason"]


# ---------------------------------------------------------------------------
# Case 6: Seniority three bands off — score should be 0.0
# ---------------------------------------------------------------------------

def test_seniority_three_bands_off():
    # target=junior (index 0), facts=lead (index 3) — distance 3 → 0.0
    cfg = _cfg(target_seniority="junior")
    listing = _listing()
    facts = _facts(seniority="lead")
    result = score_listing(listing, facts, cfg)

    assert result["components"]["seniority_fit"] == pytest.approx(0.0)
    assert "seniority mismatch" in result["reason"]


def test_seniority_two_bands_off():
    cfg = _cfg(target_seniority="junior")
    listing = _listing()
    facts = _facts(seniority="senior")
    result = score_listing(listing, facts, cfg)

    assert result["components"]["seniority_fit"] == pytest.approx(0.25)


def test_seniority_one_band_off():
    cfg = _cfg(target_seniority="mid")
    listing = _listing()
    facts = _facts(seniority="senior")
    result = score_listing(listing, facts, cfg)

    assert result["components"]["seniority_fit"] == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Extras: score is always in [0, 100] regardless of inputs
# ---------------------------------------------------------------------------

def test_score_bounds_all_bad():
    cfg = _cfg(my_skills=[], target_seniority="junior")
    listing = _listing(location="Tokyo", posted_at=None)
    facts = _facts(
        required_skills=["plc programming", "scada", "dcs"],
        seniority="lead",
        remote_ok=False,
    )
    result = score_listing(listing, facts, cfg)
    assert 0 <= result["score"] <= 100


def test_score_bounds_all_good():
    cfg = _cfg(my_skills=["sld", "load list", "power layouts"])
    listing = _listing(location=None, posted_at="2026-09-05T00:00:00+00:00")
    facts = _facts(
        required_skills=["sld", "load list", "power layouts"],
        seniority="lead",
        remote_ok=True,
    )
    result = score_listing(listing, facts, cfg)
    assert 0 <= result["score"] <= 100
    assert result["score"] >= 90


# ---------------------------------------------------------------------------
# build_reason: gap clause names actual missing skills (rule 19)
# ---------------------------------------------------------------------------

def test_reason_names_missing_skills():
    cfg = _cfg(my_skills=["sld"])
    listing = _listing(posted_at="2026-09-03T00:00:00+00:00")
    facts = _facts(required_skills=["sld", "scada", "plc programming"])
    result = score_listing(listing, facts, cfg)

    assert "scada" in result["reason"]
    assert "plc programming" in result["reason"]
    assert "1/3 required skills" in result["reason"]


def test_reason_caps_gap_at_five():
    cfg = _cfg(my_skills=[])
    listing = _listing()
    facts = _facts(
        required_skills=["a", "b", "c", "d", "e", "f", "g"],
        nice_to_have=[],
    )
    result = score_listing(listing, facts, cfg)
    # Gap clause should mention "+2 more" for skills beyond 5
    assert "+2 more" in result["reason"]
