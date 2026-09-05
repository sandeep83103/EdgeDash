"""LLM-backed classifier: is a skill string an Electrical Engineering skill?

The model classifies individual skill STRINGS only — it never produces or
ranks any aggregate number (rule 22). Every verdict is cached in the database
keyed on the skill string, so each unique skill is sent to the model at most
once, ever, across all runs (rule 18). All model access goes through
edgedash.llm — this file imports no LLM SDK (rule 15).

Public API:
    classify_skills(skills, config, db_path) -> dict[str, bool]
    is_electrical_skill(skill, config, db_path) -> bool

Definition of an electrical-engineering skill (what we keep):
    A concrete technical skill, tool, standard, or deliverable used by an
    electrical design engineer — e.g. "sld", "load list", "etap",
    "hazardous area classification", "iec 60364", "cable sizing".

What we reject:
    - Non-English strings.
    - Language requirements ("english", "deutsch").
    - Soft skills ("communication", "teamwork").
    - Management / sales / business skills ("project management",
      "stakeholder engagement", "prospecting").
    - Unrelated technical domains (healthcare IT, web dev, food science).
    - Full sentences captured as a "skill".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from edgedash.llm import LLMError, complete_json
from edgedash.storage import get_cached_skill_class, store_skill_class

if TYPE_CHECKING:
    from edgedash.config import Config

# ---------------------------------------------------------------------------
# Cheap deterministic pre-filter — reject obvious non-skills before the model.
# These never need an API call; they are unambiguous.
# ---------------------------------------------------------------------------

_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")   # umlauts/accents → non-English
_MAX_WORDS = 6                                # skills are short; sentences aren't


def _obviously_not_a_skill(skill: str) -> bool:
    s = skill.strip()
    if not s:
        return True
    if _NON_ASCII_RE.search(s):      # non-English characters
        return True
    if len(s.split()) > _MAX_WORDS:  # sentence-like fragment
        return True
    return False


# ---------------------------------------------------------------------------
# Prompt + schema for the batch classifier
# ---------------------------------------------------------------------------

_SCHEMA: dict = {
    "required": ["electrical_skills"],
    "properties": {"electrical_skills": {"type": "array"}},
}

_PROMPT_TEMPLATE = """\
You are classifying skill strings for an ELECTRICAL DESIGN ENGINEER working in
oil & gas / EPC projects.

From the list below, return ONLY the strings that are genuine electrical
engineering skills, tools, standards, or deliverables. Examples of what to KEEP:
single line diagram, load list, cable sizing, earthing, hazardous area
classification, substation design, ETAP, EPLAN, DIALux, IEC 60364, protection
coordination, transformer sizing, electrical heat tracing.

REJECT (do not return) anything that is:
- not written in English
- a spoken-language requirement (english, german, deutsch, englisch)
- a soft skill (communication, teamwork, leadership, attention to detail)
- a management, sales, or business skill (project management, stakeholder
  engagement, prospecting, account management, reporting)
- from an unrelated technical field (web development, healthcare IT, food
  science, general IT/software)
- a full sentence rather than a skill name

Return a JSON object with one key "electrical_skills" whose value is a JSON
array containing exactly the input strings that are electrical engineering
skills, copied verbatim. If none qualify, return an empty array.

INPUT SKILLS:
{skills}"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_skills(
    skills: list[str],
    config: "Config",
    db_path: Path,
) -> dict[str, bool]:
    """Classify each skill as electrical (True) or not (False).

    Uses the cache first; only unknown skills that pass the cheap pre-filter
    are sent to the model, in a single batched call. Every verdict is cached.

    Deterministic per skill after first classification. If the model call
    fails, unknown skills are conservatively marked False (excluded) rather
    than crashing the cycle (rule 17 spirit) — a false-negative just hides a
    gap; it never corrupts the aggregate.
    """
    verdicts: dict[str, bool] = {}
    to_ask: list[str] = []

    for raw in skills:
        skill = raw.strip().lower()
        if not skill:
            continue
        if skill in verdicts:
            continue

        cached = get_cached_skill_class(db_path, skill)
        if cached is not None:
            verdicts[skill] = cached
            continue

        # Cheap deterministic rejection — cache and skip the model.
        if _obviously_not_a_skill(skill):
            store_skill_class(db_path, skill, False)
            verdicts[skill] = False
            continue

        to_ask.append(skill)

    if to_ask:
        model_verdicts, model_ok = _classify_batch(to_ask, config)
        for skill in to_ask:
            verdict = model_verdicts.get(skill, False)
            verdicts[skill] = verdict
            # Only cache real answers. If the model failed, leave uncached so
            # a later successful run reclassifies instead of persisting a guess.
            if model_ok:
                store_skill_class(db_path, skill, verdict)

    return verdicts


def is_electrical_skill(
    skill: str,
    config: "Config",
    db_path: Path,
) -> bool:
    """Convenience single-skill wrapper around classify_skills()."""
    return classify_skills([skill], config, db_path).get(skill.strip().lower(), False)


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------

def _classify_batch(
    skills: list[str],
    config: "Config",
) -> tuple[dict[str, bool], bool]:
    """Ask the model which of `skills` are electrical.

    Returns (verdicts, model_ok). model_ok is False when the model call
    failed, signalling the caller not to cache the (all-False) fallback.
    """
    listed = "\n".join(f"- {s}" for s in skills)
    prompt = _PROMPT_TEMPLATE.format(skills=listed)

    try:
        result = complete_json(prompt, _SCHEMA, config, max_retries=1)
    except LLMError:
        return {s: False for s in skills}, False

    kept_raw = result.get("electrical_skills") or []
    kept = {
        str(item).strip().lower()
        for item in kept_raw
        if isinstance(item, str)
    }
    return {s: (s in kept) for s in skills}, True
