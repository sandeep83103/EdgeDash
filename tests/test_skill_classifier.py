"""Tests for edgedash/skill_classifier.py.

The LLM is faked via monkeypatch — no network, no API key, no cost.
A temp SQLite DB (pytest tmp_path) exercises the real cache path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edgedash import skill_classifier
from edgedash.config import Config
from edgedash.storage import init_db, get_cached_skill_class


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> Config:
    base = dict(
        target_role="Lead Electrical Design Engineer",
        target_city="Remote",
        keywords=["FEED"],
        my_skills=["sld"],
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


@pytest.fixture
def db(tmp_path) -> Path:
    p = tmp_path / "test.db"
    init_db(p)
    return p


# A canned electrical vocabulary the fake model "knows".
_ELECTRICAL = {
    "sld", "load list", "cable sizing", "earthing layouts",
    "substation design", "etap", "hazardous area classification",
    "iec 60364", "protection coordination",
}


def _fake_complete_json(prompt, schema, config=None, *, max_retries=1):
    """Stand-in for llm.complete_json — returns electrical skills from the prompt."""
    # The prompt lists skills as "- <skill>" lines; echo back the electrical ones.
    kept = []
    for line in prompt.splitlines():
        line = line.strip()
        if line.startswith("- "):
            skill = line[2:].strip()
            if skill in _ELECTRICAL:
                kept.append(skill)
    return {"electrical_skills": kept}


# ---------------------------------------------------------------------------
# Core classification
# ---------------------------------------------------------------------------

def test_electrical_skills_kept(db, monkeypatch):
    monkeypatch.setattr(skill_classifier, "complete_json", _fake_complete_json)
    result = skill_classifier.classify_skills(
        ["sld", "cable sizing", "etap"], _cfg(), db
    )
    assert result == {"sld": True, "cable sizing": True, "etap": True}


def test_non_electrical_skills_dropped(db, monkeypatch):
    monkeypatch.setattr(skill_classifier, "complete_json", _fake_complete_json)
    result = skill_classifier.classify_skills(
        ["project management", "prospecting", "communication"], _cfg(), db
    )
    assert result == {
        "project management": False,
        "prospecting": False,
        "communication": False,
    }


def test_mixed_batch(db, monkeypatch):
    monkeypatch.setattr(skill_classifier, "complete_json", _fake_complete_json)
    result = skill_classifier.classify_skills(
        ["sld", "project management", "etap", "leadership"], _cfg(), db
    )
    assert result["sld"] is True
    assert result["etap"] is True
    assert result["project management"] is False
    assert result["leadership"] is False


# ---------------------------------------------------------------------------
# Cheap pre-filter — never reaches the model
# ---------------------------------------------------------------------------

def test_non_english_rejected_without_model(db, monkeypatch):
    # Model that raises if called — proves the non-ASCII pre-filter short-circuits.
    def _boom(*a, **k):
        raise AssertionError("model should not be called for non-English input")

    monkeypatch.setattr(skill_classifier, "complete_json", _boom)
    # "kommunikationsstärke" contains ä → non-ASCII → rejected before the model.
    result = skill_classifier.classify_skills(["kommunikationsstärke"], _cfg(), db)
    assert result["kommunikationsstärke"] is False


def test_empty_and_whitespace_skipped(db, monkeypatch):
    monkeypatch.setattr(skill_classifier, "complete_json", _fake_complete_json)
    result = skill_classifier.classify_skills(["", "   ", "sld"], _cfg(), db)
    assert "sld" in result
    assert result["sld"] is True
    assert "" not in result


def test_long_sentence_rejected_without_model(db, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("model should not be called for a sentence")

    monkeypatch.setattr(skill_classifier, "complete_json", _boom)
    sentence = "ability to thrive in a fast paced dynamic work environment daily"
    result = skill_classifier.classify_skills([sentence], _cfg(), db)
    assert result[sentence] is False


# ---------------------------------------------------------------------------
# Caching behaviour (rule 18)
# ---------------------------------------------------------------------------

def test_verdict_is_cached(db, monkeypatch):
    monkeypatch.setattr(skill_classifier, "complete_json", _fake_complete_json)
    skill_classifier.classify_skills(["sld", "prospecting"], _cfg(), db)

    assert get_cached_skill_class(db, "sld") is True
    assert get_cached_skill_class(db, "prospecting") is False


def test_cache_hit_skips_model(db, monkeypatch):
    # First call populates the cache.
    monkeypatch.setattr(skill_classifier, "complete_json", _fake_complete_json)
    skill_classifier.classify_skills(["sld"], _cfg(), db)

    # Second call: model raises if invoked — proves the cache served the verdict.
    def _boom(*a, **k):
        raise AssertionError("model should not be called on a cache hit")

    monkeypatch.setattr(skill_classifier, "complete_json", _boom)
    result = skill_classifier.classify_skills(["sld"], _cfg(), db)
    assert result["sld"] is True


# ---------------------------------------------------------------------------
# Failure handling — model error must NOT be cached (rule 17 spirit)
# ---------------------------------------------------------------------------

def test_model_failure_not_cached(db, monkeypatch):
    from edgedash.llm import LLMError

    def _fail(*a, **k):
        raise LLMError("simulated outage")

    monkeypatch.setattr(skill_classifier, "complete_json", _fail)
    result = skill_classifier.classify_skills(["cable sizing"], _cfg(), db)

    # This run excludes it (conservative)...
    assert result["cable sizing"] is False
    # ...but it is NOT cached, so a later good run can reclassify.
    assert get_cached_skill_class(db, "cable sizing") is None
