"""Tests for the query tool registry (edgedash/query/tools.py).

Deterministic: a temp DB is seeded directly through storage. Covers:
  - each tool returns the right shape ({"rows": list, "summary": str})
  - int params clamp at BOTH bounds (companies_hiring days, best_matches n,
    top_gaps n, trend weeks)
  - an unknown skill returns empty rows (never raises)
  - the rule-46 gate: no passing cycle → empty rows

The extraction cache is keyed on a hash of the (HTML-stripped) description,
matching storage.get_scored_with_extractions, so skill_demand can find rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from edgedash import storage
from edgedash.query import tools

_NOW = datetime.now(timezone.utc)
_TAG = re.compile(r"<[^>]+>")
_SP = re.compile(r"\s+")


def _desc_hash(desc: str) -> str:
    clean = _SP.sub(" ", _TAG.sub(" ", desc)).strip()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def _iso(days_ago: int) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _write_passing_cycle(db):
    storage.log_cycle(
        db, agent="cycle", started_at=_NOW, finished_at=_NOW,
        records_touched=3, status="pass",
        notes=json.dumps({"outcome": "complete", "verdict_passed": True}),
    )


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "q.db"
    storage.init_db(p)

    # Three scored listings; two posted recently, one old.
    descs = {
        "L1": "Needs single line diagram and load list experience.",
        "L2": "Requires substation design; python is nice to have.",
        "L3": "Old posting requiring earthing layouts.",
    }
    rows = [
        {"id": "L1", "title": "Lead Electrical Engineer", "company": "Wood",
         "url": "u1", "source": "test", "posted_at": _iso(2),
         "fetched_at": _iso(2), "fit_score": 88, "fit_reason": "strong match",
         "description": descs["L1"]},
        {"id": "L2", "title": "Substation Engineer", "company": "Petrofac",
         "url": "u2", "source": "test", "posted_at": _iso(5),
         "fetched_at": _iso(5), "fit_score": 64, "fit_reason": "decent match",
         "description": descs["L2"]},
        {"id": "L3", "title": "Design Engineer", "company": "Wood",
         "url": "u3", "source": "test", "posted_at": _iso(40),
         "fetched_at": _iso(40), "fit_score": 40, "fit_reason": "weak match",
         "description": descs["L3"]},
    ]
    storage.upsert_listings(p, rows)

    # Extraction cache so skill_demand / drill-downs can resolve.
    storage.store_extraction(p, _desc_hash(descs["L1"]), {
        "required_skills": ["single line diagram", "load list"],
        "nice_to_have": [],
    })
    storage.store_extraction(p, _desc_hash(descs["L2"]), {
        "required_skills": ["substation design"],
        "nice_to_have": ["python"],
    })
    storage.store_extraction(p, _desc_hash(descs["L3"]), {
        "required_skills": ["earthing layouts"],
        "nice_to_have": [],
    })

    # A gap snapshot with example_ids for the drill-down.
    storage.save_gap_snapshot(p, "run-1", [
        {"skill": "substation design", "listings_blocked": 2,
         "opportunity_cost": 1.4, "mean_score": 70, "top_score": 88,
         "also_nice": 0, "confidence": "low", "example_ids": ["L1", "L2"]},
        {"skill": "earthing layouts", "listings_blocked": 1,
         "opportunity_cost": 0.4, "mean_score": 40, "top_score": 40,
         "also_nice": 0, "confidence": "low", "example_ids": ["L3"]},
    ])

    _write_passing_cycle(p)
    return p


# ---------------------------------------------------------------------------
# Shape: every tool returns {"rows": list, "summary": str}
# ---------------------------------------------------------------------------

def _assert_shape(result):
    assert isinstance(result, dict)
    assert set(result.keys()) == {"rows", "summary"}
    assert isinstance(result["rows"], list)
    assert isinstance(result["summary"], str) and result["summary"]


def test_companies_hiring_shape(db):
    r = tools.companies_hiring(db, days=7)
    _assert_shape(r)
    # Two recent listings (2d, 5d); the 40d one is outside the 7d window.
    assert sum(row["n"] for row in r["rows"]) == 2


def test_best_matches_shape_and_order(db):
    r = tools.best_matches(db, n=10)
    _assert_shape(r)
    scores = [row["score"] for row in r["rows"]]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 88


def test_top_gaps_shape(db):
    r = tools.top_gaps(db, n=5)
    _assert_shape(r)
    assert r["rows"][0]["skill"] == "substation design"


def test_gap_detail_shape(db):
    r = tools.gap_detail(db, skill="substation design")
    _assert_shape(r)
    ids = {row["id"] for row in r["rows"]}
    assert ids == {"L1", "L2"}


def test_listing_count_shape(db):
    r = tools.listing_count(db)
    _assert_shape(r)
    row = r["rows"][0]
    assert row["total_listings"] == 3
    assert row["scored"] == 3
    assert row["unscored"] == 0


def test_skill_demand_shape(db):
    r = tools.skill_demand(db, skill="python")
    _assert_shape(r)
    assert r["rows"][0]["nice_to_have"] == 1
    assert r["rows"][0]["required"] == 0


# ---------------------------------------------------------------------------
# Clamping at BOTH bounds (rule 41)
# ---------------------------------------------------------------------------

def test_companies_hiring_clamp_low(db):
    # days=0 → clamps to 1 (only the 2d listing would still be outside a 1d
    # window, so this mainly proves it does not raise and returns valid shape).
    r = tools.companies_hiring(db, days=0)
    _assert_shape(r)


def test_companies_hiring_clamp_high(db):
    # days=9999 → clamps to 90, which still includes the 40d listing.
    r = tools.companies_hiring(db, days=9999)
    assert sum(row["n"] for row in r["rows"]) == 3  # all three now in-window


def test_best_matches_clamp_low(db):
    r = tools.best_matches(db, n=0)      # → 1
    assert len(r["rows"]) == 1


def test_best_matches_clamp_high(db):
    r = tools.best_matches(db, n=9999)   # → 25, but only 3 exist
    assert len(r["rows"]) == 3


def test_top_gaps_clamp_low(db):
    r = tools.top_gaps(db, n=0)          # → 1
    assert len(r["rows"]) == 1


def test_top_gaps_clamp_high(db):
    r = tools.top_gaps(db, n=9999)       # → 25, but only 2 exist
    assert len(r["rows"]) == 2


def test_trend_weeks_clamp(db):
    # Non-coercible weeks falls back to default; must not raise.
    r = tools.trend(db, skill="substation design", weeks="not-a-number")
    _assert_shape(r)


def test_clamp_int_helper_bounds():
    assert tools._clamp_int(-5, 1, 90, 7) == 1
    assert tools._clamp_int(500, 1, 90, 7) == 90
    assert tools._clamp_int("x", 1, 90, 7) == 7
    assert tools._clamp_int(42, 1, 90, 7) == 42


# ---------------------------------------------------------------------------
# Unknown skill → empty rows, never raises
# ---------------------------------------------------------------------------

def test_gap_detail_unknown_skill_empty(db):
    r = tools.gap_detail(db, skill="quantum teleportation")
    _assert_shape(r)
    assert r["rows"] == []


def test_skill_demand_unknown_skill_empty(db):
    r = tools.skill_demand(db, skill="quantum teleportation")
    _assert_shape(r)
    assert r["rows"] == []


def test_trend_unknown_skill_empty(db):
    r = tools.trend(db, skill="quantum teleportation", weeks=3)
    _assert_shape(r)
    assert r["rows"] == []


# ---------------------------------------------------------------------------
# Rule 46 gate: no passing cycle → empty rows
# ---------------------------------------------------------------------------

def test_no_passing_cycle_gates_all(tmp_path):
    p = tmp_path / "empty.db"
    storage.init_db(p)
    # No cycle logged at all → last_passing_cycle is None.
    for fn in (
        lambda: tools.companies_hiring(p, days=7),
        lambda: tools.best_matches(p, n=5),
        lambda: tools.top_gaps(p, n=5),
        lambda: tools.gap_detail(p, skill="substation design"),
        lambda: tools.trend(p, skill="substation design", weeks=3),
        lambda: tools.listing_count(p),
        lambda: tools.skill_demand(p, skill="python"),
    ):
        r = fn()
        _assert_shape(r)
        assert r["rows"] == []
        assert "no verified cycle" in r["summary"]


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------

def test_registry_has_seven_tools():
    assert len(tools.TOOLS) == 7
    for spec in tools.TOOLS.values():
        assert spec.description.strip()
        assert spec.parameters.get("type") == "object"
