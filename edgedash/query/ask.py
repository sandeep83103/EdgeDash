"""Two-call natural-language query pipeline (rules 42-45).

The model appears EXACTLY twice per question and never touches the database:
    ROUTE  — pick a tool + params from the registry (or null if none fits).
    (our deterministic code runs the chosen tool)
    PHRASE — turn ONLY the returned rows into 2-3 sentences of prose.

Route → run → phrase. The model is bookends, never the engine.

Public API:
    ask(question) -> Answer(.text, .rows, .tool_used, .params)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from edgedash.config import load_config
from edgedash.llm import LLMError, complete_json
from edgedash.query.tools import TOOLS
from edgedash import storage


@dataclass
class Answer:
    text: str
    rows: list[dict] = field(default_factory=list)
    tool_used: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


# ---------------------------------------------------------------------------
# Abuse guards — all enforced BEFORE any model call (public endpoint hardening)
# ---------------------------------------------------------------------------

_MAX_QUESTION_CHARS = 300

# Control characters to strip (everything below space except tab, plus DEL).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Obvious instruction-injection patterns. Matched case-insensitively against
# the cleaned input. Deliberately narrow and specific — this is a tripwire for
# blatant attempts, not a content classifier. A hit skips the model entirely.
_INJECTION_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "disregard previous",
    "disregard the above",
    "system prompt",
    "you are now",
    "act as",
    "pretend to be",
    "forget your instructions",
    "override your",
)


def _clean_input(raw: str) -> str:
    """Strip control characters and trim. Does not alter meaningful content."""
    if not isinstance(raw, str):
        return ""
    return _CONTROL_CHARS_RE.sub("", raw).strip()


def _reject_reason(cleaned: str) -> str | None:
    """Return a rejection reason if the input should be blocked, else None.

    Order matters: cheapest/structural checks first, injection last. All of
    these run before any model call.
    """
    if not cleaned:
        return "empty input"
    if len(cleaned) > _MAX_QUESTION_CHARS:
        return f"too long ({len(cleaned)} > {_MAX_QUESTION_CHARS} chars)"
    lowered = cleaned.lower()
    for pat in _INJECTION_PATTERNS:
        if pat in lowered:
            return "suspicious input"
    return None


# ---------------------------------------------------------------------------
# ROUTE
# ---------------------------------------------------------------------------

_ROUTE_SCHEMA = {
    "required": ["tool", "params", "confidence"],
    "properties": {
        # tool is str|null — a union the simple validator can't express, so we
        # only require presence here and check the union ourselves after.
        "params": {"type": "object"},
        "confidence": {"type": "string"},
    },
}


def _build_route_prompt(question: str) -> str:
    lines = [
        "You are a router. Choose ONE tool from the registry below that answers",
        "the user's question, or return null if none genuinely fits.",
        "",
        "RULES:",
        "- Pick a tool ONLY if it directly answers the question. If no tool fits,",
        '  set "tool" to null. Do NOT pick the closest or most similar tool.',
        "  Do NOT guess.",
        '- Fill "params" using only the parameters that tool declares. Omit a',
        "  parameter to accept its default. Never invent parameters a tool does",
        "  not list.",
        "- You are selecting a tool, nothing more. You do not write queries, SQL,",
        "  or code, and you never see the database.",
        '- Set "confidence" to "high" only if the tool clearly answers the',
        '  question; otherwise "low".',
        "",
        "AVAILABLE TOOLS:",
    ]
    for spec in TOOLS.values():
        props = spec.parameters.get("properties", {})
        lines.append(f"- {spec.name}: {spec.description}")
        lines.append(f"    params: {json.dumps(props)}")
    lines += [
        "",
        "Return JSON exactly of this form:",
        '{"tool": "<tool name>" or null, "params": { ... }, '
        '"confidence": "high" | "low"}',
        "",
        "USER QUESTION:",
        question,
    ]
    return "\n".join(lines)


def _route(question: str, config) -> tuple[str | None, dict]:
    """Return (tool_name_or_None, params). Raises LLMError on a bad tool name."""
    result = complete_json(_build_route_prompt(question), _ROUTE_SCHEMA, config)

    tool = result.get("tool")
    params = result.get("params") or {}

    if tool is None:
        return None, {}

    # The routed name must be a real registry entry. Anything else is a hard
    # error, never a fallback (rule 45 / rule 40 — no free-form dispatch).
    if not isinstance(tool, str) or tool not in TOOLS:
        raise LLMError(f"router returned an unknown tool: {tool!r}")

    if not isinstance(params, dict):
        params = {}

    return tool, params


# ---------------------------------------------------------------------------
# PHRASE
# ---------------------------------------------------------------------------

_PHRASE_SCHEMA = {
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


def _build_phrase_prompt(question: str, rows: list[dict], summary: str) -> str:
    return "\n".join([
        "Answer the user's question in 2-3 plain sentences, using ONLY the data",
        "rows provided below.",
        "",
        "STRICT RULES:",
        "- Use only numbers and values present in these rows. Do NOT estimate,",
        "  extrapolate, calculate beyond them, or add any outside context or",
        "  general knowledge.",
        "- If the rows are empty, say plainly that the data does not contain an",
        "  answer to this question. Do not invent one.",
        "- You may state what was looked at using the provided summary.",
        "",
        f"SUMMARY OF WHAT WAS LOOKED AT: {summary}",
        "",
        "DATA ROWS (JSON):",
        json.dumps(rows, default=str),
        "",
        "USER QUESTION:",
        question,
        "",
        'Return JSON: {"answer": "<your 2-3 sentence answer>"}',
    ])


def _phrase(question: str, rows: list[dict], summary: str, config) -> str:
    result = complete_json(
        _build_phrase_prompt(question, rows, summary), _PHRASE_SCHEMA, config
    )
    return str(result.get("answer", "")).strip()


# ---------------------------------------------------------------------------
# No-match fixed message (rule 45) — NO model call here
# ---------------------------------------------------------------------------

def _no_match_answer(question: str) -> Answer:
    lines = [
        "I can't answer that from the data I have. I can only answer questions",
        "that map to one of these tools:",
        "",
    ]
    for spec in TOOLS.values():
        # First sentence of each description, in plain English.
        first_sentence = spec.description.split(".")[0].strip()
        lines.append(f"  • {first_sentence}.")
    lines += [
        "",
        "Try rephrasing your question to match one of the above.",
    ]
    return Answer(
        text="\n".join(lines),
        rows=[],
        tool_used=None,
        params={},
        summary="no matching tool",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _utc_midnight_iso() -> str:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat()


def daily_cap_reached(config=None) -> bool:
    """True if today's logged questions have hit the global cap (rule 3).

    Read-only; the app uses this to disable the ask box while keeping the
    dashboard up. Counts every logged question today, rejections included.
    """
    cfg = config or load_config()
    db = cfg.abs_db_path
    # Read-only: the dashboard never creates schema (rule 49). If the table
    # is missing or the DB is unreachable, treat as "not capped" — the app
    # wraps this call and the scheduler owns schema creation.
    try:
        used = storage.count_queries_since(db, _utc_midnight_iso())
    except Exception:
        return False
    return used >= cfg.daily_question_cap


def _log(db, *, question, tool_chosen, params, answerable, started) -> None:
    """Best-effort query_log write; a logging failure never sinks the answer."""
    duration_ms = int(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000
    )
    try:
        storage.log_query(
            db,
            question=question,
            tool_chosen=tool_chosen,
            params=params,
            answerable=answerable,
            duration_ms=duration_ms,
        )
    except Exception:
        pass


def ask(question: str, config=None) -> Answer:
    """Answer a natural-language question via route → run → phrase (rules 42-45).

    Abuse guards run BEFORE any model call: daily cap, then input validation
    and an injection tripwire. A blocked question makes no model call, is
    logged with its reason, and returns the standard can't-answer message —
    the filter is never explained back to the caller.

    Logs every question to query_log (rules 5, and guard rule 4).
    """
    cfg = config or load_config()
    db = cfg.abs_db_path
    # The dashboard is read-only for schema (rule 49): ask() no longer creates
    # tables. The scheduler owns schema creation. The one write ask() performs
    # is the query_log audit row (required by rules 4/5), and that is wrapped
    # so a missing table degrades gracefully rather than crashing the page.
    started = datetime.now(timezone.utc)

    # ── Guard 0: global daily cap (checked before ANY model call, rule 3) ──
    try:
        _today = storage.count_queries_since(db, _utc_midnight_iso())
    except Exception:
        _today = 0  # cannot read the log → do not block on the cap
    if _today >= cfg.daily_question_cap:
        _log(db, question=str(question)[:_MAX_QUESTION_CHARS],
             tool_chosen="rejected: daily cap reached", params={},
             answerable=False, started=started)
        return Answer(
            text=(
                "The daily question limit has been reached. The dashboard data "
                "is still available above; please try the ask box again "
                "tomorrow."
            ),
            summary="daily cap reached",
        )

    # ── Guard 1: input validation + injection tripwire (before model) ─────
    cleaned = _clean_input(question)
    reason = _reject_reason(cleaned)
    if reason is not None:
        # Log the real reason (rule 4) but never explain the filter to the
        # caller — a rejected question gets the standard can't-answer message.
        _log(db, question=cleaned[:_MAX_QUESTION_CHARS] or "(empty)",
             tool_chosen=f"rejected: {reason}", params={},
             answerable=False, started=started)
        return _no_match_answer(cleaned)

    question = cleaned  # from here on, work with the cleaned text

    tool_name: str | None = None
    params: dict[str, Any] = {}
    answerable = False

    try:
        # ── ROUTE (model call 1) ──────────────────────────────────────────
        tool_name, params = _route(question, cfg)

        if tool_name is None:
            answer = _no_match_answer(question)  # fixed message, no model call
        else:
            # ── EXECUTE — registry lookup only; never eval/getattr on input ─
            spec = TOOLS[tool_name]
            result = spec.func(db, **params)     # tool clamps its own params
            rows = result["rows"]
            summary = result["summary"]
            answerable = True

            # ── PHRASE (model call 2) ─────────────────────────────────────
            text = _phrase(question, rows, summary, cfg)
            answer = Answer(
                text=text, rows=rows, tool_used=tool_name,
                params=params, summary=summary,
            )
    finally:
        _log(db, question=question, tool_chosen=tool_name, params=params,
             answerable=answerable, started=started)

    return answer
