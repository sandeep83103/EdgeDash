"""Tests for edgedash/health.py — pure assess() and dashboard_status().

`now` is always passed in, so every case is deterministic with no clock or DB.
One case exercises report() against a missing DB to confirm the unreachable
path degrades to a single clean failure rather than raising.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from edgedash import health

_NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
_MAX_DAYS = 3


def _hrs(n): return _NOW - timedelta(hours=n)
def _days(n): return _NOW - timedelta(days=n)


def _assess(**over):
    base = dict(
        now=_NOW,
        db_reachable=True,
        newest_listing_at=_hrs(1),
        last_pass_at=_hrs(1),
        recent_verdicts=[True, True, True],
        max_data_age_days=_MAX_DAYS,
    )
    base.update(over)
    return health.assess(**base)


def _by(report, name):
    return next(c for c in report.checks if c.name == name)


# ── Healthy baseline ─────────────────────────────────────────────────────────

def test_all_healthy():
    r = _assess()
    assert r.healthy is True
    assert all(c.healthy for c in r.checks)


# ── 1. Stale listings ────────────────────────────────────────────────────────

def test_stale_listings_fresh_ok():
    assert _by(_assess(newest_listing_at=_days(2)), "stale_listings").healthy is True


def test_stale_listings_too_old_fails():
    r = _assess(newest_listing_at=_days(5))
    c = _by(r, "stale_listings")
    assert c.healthy is False
    assert r.healthy is False
    assert "5.0 day" in c.message


def test_no_listings_fails():
    c = _by(_assess(newest_listing_at=None), "stale_listings")
    assert c.healthy is False
    assert "no listings" in c.observed


# ── 2. No recent successful cycle ────────────────────────────────────────────

def test_recent_cycle_ok():
    assert _by(_assess(last_pass_at=_hrs(10)), "no_recent_cycle").healthy is True


def test_no_cycle_in_48h_fails():
    r = _assess(last_pass_at=_hrs(60))
    c = _by(r, "no_recent_cycle")
    assert c.healthy is False
    assert r.healthy is False


def test_never_passed_fails():
    c = _by(_assess(last_pass_at=None), "no_recent_cycle")
    assert c.healthy is False
    assert c.observed == "never"


# ── 3. Last three failed ─────────────────────────────────────────────────────

def test_last_three_all_failed_fails():
    r = _assess(recent_verdicts=[False, False, False])
    c = _by(r, "last_three_failed")
    assert c.healthy is False
    assert r.healthy is False


def test_two_failed_one_pass_ok():
    # Not all three failed → healthy on this check.
    c = _by(_assess(recent_verdicts=[False, False, True]), "last_three_failed")
    assert c.healthy is True


def test_fewer_than_three_cycles_ok():
    # Can't be "last three failed" with only two cycles.
    c = _by(_assess(recent_verdicts=[False, False]), "last_three_failed")
    assert c.healthy is True


# ── 4. DB unreachable ────────────────────────────────────────────────────────

def test_db_unreachable_single_failure():
    r = _assess(db_reachable=False)
    assert r.healthy is False
    assert len(r.checks) == 1
    assert r.checks[0].name == "db_unreachable"


# ── dashboard_status: green / amber / red ────────────────────────────────────

def test_status_green_within_24h():
    level, _ = health.dashboard_status(
        now=_NOW, last_pass_at=_hrs(5), recent_verdicts=[True, True, True])
    assert level == "green"


def test_status_amber_when_stale():
    level, _ = health.dashboard_status(
        now=_NOW, last_pass_at=_hrs(40), recent_verdicts=[True, False, True])
    assert level == "amber"


def test_status_amber_when_never_passed():
    level, _ = health.dashboard_status(
        now=_NOW, last_pass_at=None, recent_verdicts=[])
    assert level == "amber"


def test_status_red_when_last_three_failed():
    # Red takes priority even if a stale pass exists in the window.
    level, _ = health.dashboard_status(
        now=_NOW, last_pass_at=_hrs(100), recent_verdicts=[False, False, False])
    assert level == "red"


# ── report() unreachable path (no DB file / bad path) ────────────────────────

def test_report_unreachable_when_db_read_fails(monkeypatch):
    # Force _gather to raise → report() must return the single db_unreachable
    # failure, never propagate the exception.
    monkeypatch.setattr(health, "_gather",
                        lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
    r = health.report("ignored", now=_NOW)
    assert r.healthy is False
    assert r.checks[0].name == "db_unreachable"
