"""Skill canonicalisation — pure functions, no network, no model calls.

canonical() is the single transformation every skill string passes through
before being stored, compared, or counted. Same input → same output, always.

Rule 23: skill names are normalised through an explicit alias map in
config.yaml. String-similarity heuristics are never used to merge skills
automatically — that produces silent, hard-to-audit errors.

CLI audit:
    python -m edgedash.skills --audit
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# canonical()
# ---------------------------------------------------------------------------

# Matches a parenthetical qualifier at the end: "substation design (hv)" → drop " (hv)"
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")

# Characters that should be stripped from both ends (not from the middle —
# "mv/lv" must survive, but a stray leading/trailing slash should not).
_EDGE_PUNCT = re.compile(r"^[^\w/]+|[^\w/]+$")


def canonical(raw: str, aliases: dict[str, str]) -> str:
    """Return the canonical form of a skill string.

    Steps (in order):
      1. Lowercase and strip surrounding whitespace.
      2. Drop parenthetical qualifiers: "substation design (hv)" → "substation design".
      3. Collapse runs of internal whitespace to a single space.
      4. Strip leading/trailing punctuation (but preserve internal / for mv/lv).
      5. Look up in aliases; if present, return the alias target.

    Pure function — no side effects, no I/O.
    """
    if not raw or not raw.strip():
        return ""

    s = raw.lower().strip()
    s = _PAREN_RE.sub("", s)          # drop (qualifiers)
    s = re.sub(r"\s+", " ", s).strip()  # collapse whitespace
    s = _EDGE_PUNCT.sub("", s).strip()  # strip edge punctuation

    return aliases.get(s, s)


# ---------------------------------------------------------------------------
# Storage helpers for the audit (read-only, via storage module per rule 2)
# ---------------------------------------------------------------------------

def _load_all_skills_from_cache(db_path: Path) -> list[str]:
    """Read every required_skills list from extraction_cache. Read-only."""
    # Import here (not at module top) so this module stays importable
    # without a DB present — tests never need this function.
    from edgedash.storage import _connect  # type: ignore[attr-defined]

    skills: list[str] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT result_json FROM extraction_cache"
        ).fetchall()

    for row in rows:
        try:
            data = json.loads(row["result_json"])
            for skill in data.get("required_skills") or []:
                if isinstance(skill, str) and skill.strip():
                    skills.append(skill.strip())
        except (json.JSONDecodeError, TypeError):
            continue

    return skills


# ---------------------------------------------------------------------------
# CLI audit
# ---------------------------------------------------------------------------

def _audit(db_path: Path, aliases: dict[str, str], top_n: int = 40) -> None:
    raw_skills = _load_all_skills_from_cache(db_path)

    if not raw_skills:
        print("No extracted skills found in the database.")
        print("Run  python run_cycle.py  first to populate the extraction cache.")
        return

    counts: Counter[str] = Counter(raw_skills)
    total_unique = len(counts)
    total_occurrences = sum(counts.values())

    W = 64
    print()
    print("═" * W)
    print(f"  Skill audit — {total_occurrences} occurrences, {total_unique} unique strings")
    print("═" * W)

    # ── Top N raw strings with canonical mapping ──────────────────────────
    print()
    print(f"── Top {top_n} raw skill strings " + "─" * max(0, W - 22 - len(str(top_n))))
    print()
    col_skill = 36
    col_count = 6
    print(f"  {'Raw string':<{col_skill}}  {'Count':>{col_count}}  Canonical")
    print("  " + "─" * (W - 2))

    for raw, count in counts.most_common(top_n):
        canon = canonical(raw, aliases)
        changed = "→ " + canon if canon != raw.lower().strip() else ""
        raw_display = raw[:col_skill - 1] + "…" if len(raw) > col_skill else raw
        print(f"  {raw_display:<{col_skill}}  {count:>{col_count}}  {changed}")

    # ── Singletons ────────────────────────────────────────────────────────
    singletons = sorted(raw for raw, n in counts.items() if n == 1)
    print()
    print(f"── Singleton strings (seen once — likely junk, typos, or full sentences)")
    print(f"   {len(singletons)} of {total_unique} unique strings appear only once.")
    print()
    if singletons:
        for s in singletons[:80]:   # cap display to 80 lines
            print(f"  · {s}")
        if len(singletons) > 80:
            print(f"  … and {len(singletons) - 80} more")
    else:
        print("  None — good signal quality.")

    print()
    print("  To canonicalise a collision, add it to skill_aliases in config.yaml.")
    print()


# ---------------------------------------------------------------------------
# Alias suggestion (the ONLY model call in this module)
# ---------------------------------------------------------------------------

# Schema for the single LLM call. complete_json validates the top-level dict;
# we ask for a "proposals" array and validate its shape ourselves after.
_SUGGEST_SCHEMA: dict = {
    "required": ["proposals"],
    "properties": {"proposals": {"type": "array"}},
}


def _unmapped_canonical_skills(
    db_path: Path,
    aliases: dict[str, str],
) -> Counter[str]:
    """Canonical skill strings present in the DB that the alias map does not touch.

    A string is 'already handled' if it is either a key in the alias map
    (a known variant) or a value (a chosen canonical target). Everything else
    is a candidate the model may propose a grouping for.
    """
    raw_skills = _load_all_skills_from_cache(db_path)

    # Strings the user has already made a deliberate choice about.
    known: set[str] = set(aliases.keys()) | set(aliases.values())

    counts: Counter[str] = Counter()
    for raw in raw_skills:
        canon = canonical(raw, aliases)
        if canon and canon not in known:
            counts[canon] += 1
    return counts


def _build_suggest_prompt(skills_with_counts: list[tuple[str, int]]) -> str:
    lines = [f'  "{s}"  (seen {n}x)' for s, n in skills_with_counts]
    skill_block = "\n".join(lines)
    return (
        "You are helping group job-listing skill strings that refer to the "
        "SAME underlying skill (e.g. spelling variants, abbreviations, or "
        "obvious synonyms).\n\n"
        "Rules:\n"
        "- Only group strings that clearly mean the same skill.\n"
        "- If two strings are related but distinct skills, do NOT group them.\n"
        "- When unsure, mark the proposal confidence as \"low\".\n"
        "- Pick the clearest, most standard string as the canonical form.\n"
        "- Do not invent strings that are not in the list below.\n\n"
        "Return JSON of the exact form:\n"
        '{"proposals": [\n'
        '  {"canonical": "<string>", "variants": ["<string>", ...], '
        '"confidence": "high" | "low"}\n'
        "]}\n\n"
        "Skill strings (with how often each appeared):\n"
        f"{skill_block}\n"
    )


def _valid_proposal(p: object) -> bool:
    return (
        isinstance(p, dict)
        and isinstance(p.get("canonical"), str)
        and isinstance(p.get("variants"), list)
        and all(isinstance(v, str) for v in p.get("variants", []))
        and p.get("confidence") in ("high", "low")
    )


def _find_conflicts(
    proposal: dict,
    aliases: dict[str, str],
) -> list[str]:
    """Return strings in this proposal that already have a DIFFERENT mapping.

    A conflict is any variant (or the proposed canonical) that the user has
    already mapped to a canonical form that differs from this proposal's.
    """
    proposed_canon = proposal["canonical"].lower().strip()
    members = [proposed_canon] + [
        v.lower().strip() for v in proposal["variants"]
    ]
    conflicts: list[str] = []
    for member in members:
        if member in aliases and aliases[member] != proposed_canon:
            conflicts.append(
                f'"{member}" is already mapped to "{aliases[member]}"'
            )
        # Also flag if the member is itself a canonical target the user chose,
        # and the proposal would fold it under a different canonical.
        if member in set(aliases.values()) and member != proposed_canon:
            conflicts.append(
                f'"{member}" is already one of your canonical targets'
            )
    return conflicts


def _suggest_aliases(db_path: Path, aliases: dict[str, str], config) -> None:
    from edgedash.llm import LLMError, complete_json

    counts = _unmapped_canonical_skills(db_path, aliases)
    if not counts:
        print("No unmapped skill strings found — nothing to suggest.")
        print("Either the DB is empty or every string is already in your alias map.")
        return

    # Cap the payload so we stay well inside a single free-tier call.
    top = counts.most_common(60)

    print("  Collected "
          f"{len(counts)} unmapped canonical skill strings "
          f"(sending top {len(top)} to the model)…")
    print()

    prompt = _build_suggest_prompt(top)
    try:
        result = complete_json(prompt, _SUGGEST_SCHEMA, config, max_retries=1)
    except LLMError as exc:
        print(f"  Model call failed: {exc}")
        sys.exit(1)

    proposals = [p for p in result.get("proposals", []) if _valid_proposal(p)]
    # Only keep proposals that actually group 2+ strings.
    proposals = [p for p in proposals if len(p["variants"]) >= 1]

    _print_suggestions(proposals, aliases)


def _print_suggestions(proposals: list[dict], aliases: dict[str, str]) -> None:
    W = 72
    print("═" * W)
    print("  SUGGESTED ALIASES — REVIEW BEFORE USING")
    print("═" * W)
    print()
    print("  These are MODEL SUGGESTIONS, not decisions. Read every line.")
    print("  Merging two DISTINCT skills is worse than leaving them separate —")
    print("  it hides a real gap. When in doubt, do not add the entry.")
    print("  Nothing has been written to config.yaml. This is copy-paste only.")
    print()

    if not proposals:
        print("  The model proposed no groupings.")
        print()
        return

    # Split proposals into clean vs conflicting.
    clean: list[dict] = []
    conflicting: list[tuple[dict, list[str]]] = []
    for p in proposals:
        conflicts = _find_conflicts(p, aliases)
        if conflicts:
            conflicting.append((p, conflicts))
        else:
            clean.append(p)

    if conflicting:
        print("─" * W)
        print("  ⚠  CONFLICTS WITH YOUR EXISTING ALIAS MAP — DO NOT PASTE BLINDLY")
        print("─" * W)
        for p, conflicts in conflicting:
            print()
            print(f"  Proposed canonical: \"{p['canonical']}\"  "
                  f"(confidence: {p['confidence']})")
            for c in conflicts:
                print(f"    ⚠  {c}")
            print("    This contradicts a choice you already made. Skipping YAML.")
        print()

    print("─" * W)
    print("  READY-TO-PASTE (into skill_aliases in config.yaml)")
    print("─" * W)
    print()

    if not clean:
        print("  # (no conflict-free proposals)")
        print()
        return

    for p in clean:
        canon = p["canonical"].lower().strip()
        conf = p["confidence"]
        marker = "" if conf == "high" else "   # ⚠ low confidence — verify"
        print(f"  # {canon}{marker}")
        for variant in p["variants"]:
            v = variant.lower().strip()
            if v == canon:
                continue  # no self-mapping needed
            print(f'  "{v}": "{canon}"')
        print()

    print("─" * W)
    print(f"  {len(clean)} clean proposal(s), {len(conflicting)} flagged as conflicts.")
    print("  Copy only the lines you agree with. Leave the rest.")
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m edgedash.skills",
        description="Skill string tools — read-only, never writes any file.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--audit",
        action="store_true",
        help="Print top raw skill strings and singletons from the extraction cache.",
    )
    group.add_argument(
        "--suggest-aliases",
        action="store_true",
        help="Ask the model to propose alias groupings (one LLM call). "
             "Prints ready-to-paste YAML; writes nothing.",
    )
    args = parser.parse_args()

    from edgedash.config import load_config
    cfg = load_config()

    if not cfg.abs_db_path.exists():
        print(f"No database at {cfg.abs_db_path}.")
        print("Run  python run_cycle.py  first.")
        sys.exit(1)

    if args.audit:
        _audit(cfg.abs_db_path, cfg.skill_aliases)
    elif args.suggest_aliases:
        _suggest_aliases(cfg.abs_db_path, cfg.skill_aliases, cfg)
