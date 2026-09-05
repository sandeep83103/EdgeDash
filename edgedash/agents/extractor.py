"""Extraction step — the only part of the Scorer that calls a model.

Reads a job listing, sends the description to the LLM, and returns
structured facts. No score is computed here (rule 16).

The model is told only: "you are reading a document". It has no knowledge
of a candidate, a profile, or weights — those never appear in the prompt.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from edgedash.config import Config
from edgedash.llm import LLMError, complete_json
from edgedash.storage import get_cached_extraction, store_extraction

# ---------------------------------------------------------------------------
# Extraction schema  (rule 16: no score field, ever)
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA: dict[str, Any] = {
    "required": [
        "required_skills",
        "nice_to_have",
        "seniority",
        "years_required",
        "remote_ok",
    ],
    "properties": {
        "required_skills": {"type": "array"},
        "nice_to_have":    {"type": "array"},
        # seniority: validated manually — jsonschema enum not supported by our
        # lightweight validator, so we check it in _validate_extraction().
        "seniority":       {"type": "string"},
        # years_required and remote_ok may be int/bool/null — no type
        # constraint in the schema; _validate_extraction() checks nullability.
        "years_required":  {},
        "remote_ok":       {},
    },
}

_VALID_SENIORITY = {"junior", "mid", "senior", "lead", "unknown"}

# ---------------------------------------------------------------------------
# Prompt  (rule 16: no mention of candidate, profile, or scoring weights)
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are a document parser. Extract structured facts from the job description below.

Rules:
- Only extract what the description explicitly states. Do not infer, guess,
  or fill in typical industry assumptions.
- If a field is not mentioned in the description, use null or an empty list.
- Do not evaluate any candidate. No candidate exists. You are reading a document.
- Strip all HTML tags from text before processing.

Return a single JSON object with exactly these fields:
  required_skills  — list of strings: skills the role explicitly requires
  nice_to_have     — list of strings: skills described as preferred, bonus,
                     or nice to have
  seniority        — one of: "junior", "mid", "senior", "lead", "unknown"
  years_required   — integer if the description states a specific number,
                     otherwise null
  remote_ok        — true if remote is explicitly offered, false if explicitly
                     excluded, null if not mentioned

JOB DESCRIPTION:
{description}"""

# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    no_tags = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", no_tags).strip()


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def _description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Post-extraction normalisation and validation
# ---------------------------------------------------------------------------

def _normalise(result: dict[str, Any]) -> dict[str, Any]:
    """Lowercase all skill strings; coerce seniority to a known value."""
    result["required_skills"] = [
        s.lower().strip() for s in (result.get("required_skills") or [])
        if isinstance(s, str) and s.strip()
    ]
    result["nice_to_have"] = [
        s.lower().strip() for s in (result.get("nice_to_have") or [])
        if isinstance(s, str) and s.strip()
    ]
    seniority = str(result.get("seniority") or "unknown").lower().strip()
    result["seniority"] = seniority if seniority in _VALID_SENIORITY else "unknown"

    yr = result.get("years_required")
    result["years_required"] = int(yr) if isinstance(yr, (int, float)) else None

    ro = result.get("remote_ok")
    result["remote_ok"] = bool(ro) if isinstance(ro, bool) else None

    return result


def _validate_extraction(result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(result.get("required_skills"), list):
        errors.append("required_skills must be a list")
    if not isinstance(result.get("nice_to_have"), list):
        errors.append("nice_to_have must be a list")
    if result.get("seniority") not in _VALID_SENIORITY:
        errors.append(
            f"seniority must be one of {sorted(_VALID_SENIORITY)}, "
            f"got {result.get('seniority')!r}"
        )
    yr = result.get("years_required")
    if yr is not None and not isinstance(yr, (int, float)):
        errors.append("years_required must be an integer or null")
    ro = result.get("remote_ok")
    if ro is not None and not isinstance(ro, bool):
        errors.append("remote_ok must be a boolean or null")
    return errors


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def extract(
    listing: dict[str, Any],
    config: Config,
    db_path: Path,
) -> dict[str, Any] | None:
    """Extract structured facts from a listing's description.

    Returns the normalised extraction dict, or None if the model fails
    after retries (caller logs this as a per-listing failure per rule 17).
    """
    raw_description = listing.get("description") or ""
    clean_description = _strip_html(raw_description).strip()

    if not clean_description:
        return {
            "required_skills": [],
            "nice_to_have":    [],
            "seniority":       "unknown",
            "years_required":  None,
            "remote_ok":       None,
        }

    desc_hash = _description_hash(clean_description)

    # Cache hit — no model call.
    cached = get_cached_extraction(db_path, desc_hash)
    if cached is not None:
        return cached

    # Cache miss — call the model.
    prompt = _PROMPT_TEMPLATE.format(description=clean_description)
    try:
        raw_result = complete_json(prompt, EXTRACTION_SCHEMA, config, max_retries=1)
    except LLMError:
        raise  # caller handles per rule 17

    errors = _validate_extraction(raw_result)
    if errors:
        raise LLMError(
            f"Extraction result failed post-normalisation validation: "
            f"{'; '.join(errors)}"
        )

    result = _normalise(raw_result)
    store_extraction(db_path, desc_hash, result)
    return result
