"""Tests for edgedash/skills.py — canonical() only.

Pure function, no DB, no network. Fast and deterministic.
"""

from __future__ import annotations

import pytest

from edgedash.skills import canonical

# A representative alias map for testing — mirrors the real config.yaml entries.
_ALIASES: dict[str, str] = {
    # Drawings
    "single line diagram":          "sld",
    "one line diagram":             "sld",
    "single-line diagram":          "sld",
    # Heat tracing
    "heat tracing":                 "electrical heat tracing",
    "eht":                          "electrical heat tracing",
    "trace heating":                "electrical heat tracing",
    # Hazardous area
    "atex":                         "hazardous area layouts",
    "hazardous area":               "hazardous area layouts",
    "area classification":          "hazardous area layouts",
    # Procurement
    "mto":                          "material requisitions",
    "bill of materials":            "material requisitions",
    "tbe":                          "technical bid evaluation",
    "vdr":                          "vendor document review",
    # Sizing / calculations
    "cable sizing":                 "equipment sizing",
    "load calculation":             "equipment sizing",
    "electrical calculations":      "equipment sizing",
    # Substation
    "substation engineering":       "substation design",
    "switchgear design":            "substation design",
    # FEED / detail design
    "front-end engineering design": "feed",
    "detailed design":              "detail design",
    "detail engineering":           "detail design",
    # Load list
    "load schedule":                "load list",
    "electrical load list":         "load list",
    # Earthing
    "earthing design":              "earthing layouts",
    "grounding design":             "earthing layouts",
    # Lighting
    "lighting design":              "lighting layouts",
    "dialux":                       "lighting layouts",
}


# ---------------------------------------------------------------------------
# Case 1: Case folding — mixed case becomes lowercase
# ---------------------------------------------------------------------------

def test_case_folding():
    assert canonical("ETAP", _ALIASES) == "etap"
    assert canonical("Substation Design", _ALIASES) == "substation design"
    assert canonical("EARTHING LAYOUTS", _ALIASES) == "earthing layouts"


# ---------------------------------------------------------------------------
# Case 2: Whitespace — leading, trailing, and internal are all collapsed
# ---------------------------------------------------------------------------

def test_strip_leading_trailing_whitespace():
    assert canonical("  etap  ", _ALIASES) == "etap"


def test_collapse_internal_whitespace():
    assert canonical("power   layouts", _ALIASES) == "power layouts"


def test_tab_and_newline_stripped():
    assert canonical("\tetap\n", _ALIASES) == "etap"


# ---------------------------------------------------------------------------
# Case 3: Parenthetical qualifiers are dropped
# ---------------------------------------------------------------------------

def test_parenthetical_stripped():
    assert canonical("substation design (hv)", _ALIASES) == "substation design"
    assert canonical("etap (v22)", _ALIASES) == "etap"


def test_parenthetical_with_spaces():
    assert canonical("  sld  (drawings)  ", _ALIASES) == "sld"


def test_no_parenthetical_unchanged():
    assert canonical("eplan", _ALIASES) == "eplan"


# ---------------------------------------------------------------------------
# Case 4: Aliased term maps to canonical form
# ---------------------------------------------------------------------------

def test_alias_sld_variants():
    assert canonical("single line diagram", _ALIASES) == "sld"
    assert canonical("Single Line Diagram", _ALIASES) == "sld"
    assert canonical("single-line diagram", _ALIASES) == "sld"
    assert canonical("one line diagram", _ALIASES) == "sld"


def test_alias_heat_tracing():
    assert canonical("heat tracing", _ALIASES) == "electrical heat tracing"
    assert canonical("EHT", _ALIASES) == "electrical heat tracing"
    assert canonical("trace heating", _ALIASES) == "electrical heat tracing"


def test_alias_hazardous_area():
    assert canonical("ATEX", _ALIASES) == "hazardous area layouts"
    assert canonical("hazardous area", _ALIASES) == "hazardous area layouts"
    assert canonical("area classification", _ALIASES) == "hazardous area layouts"


def test_alias_procurement():
    assert canonical("MTO", _ALIASES) == "material requisitions"
    assert canonical("bill of materials", _ALIASES) == "material requisitions"
    assert canonical("TBE", _ALIASES) == "technical bid evaluation"
    assert canonical("VDR", _ALIASES) == "vendor document review"


def test_alias_equipment_sizing():
    assert canonical("cable sizing", _ALIASES) == "equipment sizing"
    assert canonical("load calculation", _ALIASES) == "equipment sizing"
    assert canonical("electrical calculations", _ALIASES) == "equipment sizing"


def test_alias_substation():
    assert canonical("substation engineering", _ALIASES) == "substation design"
    assert canonical("switchgear design", _ALIASES) == "substation design"


def test_alias_feed_and_detail_design():
    assert canonical("front-end engineering design", _ALIASES) == "feed"
    assert canonical("detailed design", _ALIASES) == "detail design"
    assert canonical("detail engineering", _ALIASES) == "detail design"


def test_alias_load_list():
    assert canonical("load schedule", _ALIASES) == "load list"
    assert canonical("electrical load list", _ALIASES) == "load list"


def test_alias_earthing():
    assert canonical("earthing design", _ALIASES) == "earthing layouts"
    assert canonical("grounding design", _ALIASES) == "earthing layouts"


def test_alias_lighting():
    assert canonical("lighting design", _ALIASES) == "lighting layouts"
    assert canonical("dialux", _ALIASES) == "lighting layouts"


# ---------------------------------------------------------------------------
# Case 5: Term with no alias passes through unchanged
# ---------------------------------------------------------------------------

def test_no_alias_passthrough():
    assert canonical("etap", _ALIASES) == "etap"
    assert canonical("autocad electrical", _ALIASES) == "autocad electrical"
    assert canonical("protection relay", _ALIASES) == "protection relay"


# ---------------------------------------------------------------------------
# Case 6: Empty string and whitespace-only return empty string
# ---------------------------------------------------------------------------

def test_empty_string():
    assert canonical("", _ALIASES) == ""


def test_whitespace_only():
    assert canonical("   ", _ALIASES) == ""


def test_none_equivalent_empty():
    # Callers sometimes pass stripped values; guard against accidental None
    # by ensuring the contract is clear: only str input is accepted.
    # This test documents the expected behaviour, not an error path.
    assert canonical("", {}) == ""


# ---------------------------------------------------------------------------
# Edge cases: punctuation stripping, alias after normalisation
# ---------------------------------------------------------------------------

def test_leading_trailing_punctuation_stripped():
    # A comma or period at the edge (from sentence extraction) should vanish.
    assert canonical(",etap.", _ALIASES) == "etap"
    assert canonical(";eplan;", _ALIASES) == "eplan"


def test_internal_slash_preserved():
    # mv/lv must survive — the slash is meaningful.
    assert canonical("mv/lv", _ALIASES) == "mv/lv"


def test_alias_applied_after_normalisation():
    # "  EHT  (design) " → strip → lowercase → drop parens → alias
    assert canonical("  EHT  (design) ", _ALIASES) == "electrical heat tracing"
    # "  Single Line Diagram  (rev b) " → all transforms then alias
    assert canonical("  Single Line Diagram  (rev b) ", _ALIASES) == "sld"
