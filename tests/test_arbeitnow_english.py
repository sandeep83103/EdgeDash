"""Tests for the English-language filter in edgedash/sources/arbeitnow.py.

Pure function _is_english() — no network. Verifies German listings are
dropped and English listings are kept.
"""

from __future__ import annotations

from edgedash.sources.arbeitnow import _is_english


def _job(title: str, description: str, tags=None) -> dict:
    return {"title": title, "description": description, "tags": tags or []}


def test_english_electrical_listing_kept():
    job = _job(
        "Lead Electrical Design Engineer",
        "Lead the electrical detail design for offshore oil and gas projects. "
        "Prepare single line diagrams, load lists, and cable sizing calculations.",
    )
    assert _is_english(job) is True


def test_german_listing_dropped():
    job = _job(
        "Technischer Senior Projektmanager (m/w/d)",
        "Wir suchen eine Person mit Erfahrung und Kenntnissen fuer unser Team, "
        "die sich mit der Datenintegration und dem Projektmanagement auskennt.",
    )
    assert _is_english(job) is False


def test_german_with_umlauts_dropped():
    job = _job(
        "Softwareentwickler",
        "Du entwickelst Webanwendungen und arbeitest mit der Software-Architektur "
        "unseres Teams. Sehr gute Kenntnisse sind für diese Aufgaben nötig.",
    )
    assert _is_english(job) is False


def test_short_english_title_kept():
    # Few words, no German markers → English.
    job = _job("Substation Design Engineer", "ETAP and IEC 60364 experience.")
    assert _is_english(job) is True


def test_mostly_english_with_one_german_word_kept():
    # A single stray German word must not trip the threshold (needs >= 4).
    job = _job(
        "Electrical Engineer",
        "Design substations and cable routing. The word und appears once here.",
    )
    assert _is_english(job) is True
