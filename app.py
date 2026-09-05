"""EdgeDash — agent activity dashboard (Streamlit, read-only).

This app READS through the storage module only. It never writes and never
runs a cycle — the scheduler writes, the dashboard reads. There is no
"run cycle" button by design (rule 49).

Hostile-startup hardening (rule 50): the page always renders. If the database
is missing, unreachable, or mid-migration, or if any single panel fails, the
user sees a clear status message — never a stack trace. Failure detail is
logged server-side only. No secret is ever printed or rendered (rule 48).

Run it with:
    streamlit run app.py
"""

from __future__ import annotations

import logging
import time

import streamlit as st

from edgedash.config import load_config
from edgedash import storage

logger = logging.getLogger("edgedash.dashboard")

# Per-session rate limit for the ask box (rule 1): max N questions per window.
_ASK_MAX_PER_WINDOW = 10
_ASK_WINDOW_SECONDS = 10 * 60

# Short TTL so reruns don't hammer the DB, but the view still refreshes
# within a few seconds of a scheduler-written cycle.
_TTL = 10  # seconds

# Where "no cycles yet" points people. Cosmetic; safe to show.
_FIRST_RUN_HINT = "the next scheduled run"
_GITHUB_URL = "https://github.com/sandeep83103/EdgeDash"


# ---------------------------------------------------------------------------
# Cached read functions (read-only; storage module is the only DB door).
# These may raise if the DB is unreachable — every caller wraps them.
# ---------------------------------------------------------------------------

@st.cache_data(ttl=_TTL)
def _db_path() -> str:
    return str(load_config().abs_db_path)


@st.cache_data(ttl=_TTL)
def _counts(db: str) -> tuple[int, int]:
    return storage.count_listings(db)


@st.cache_data(ttl=_TTL)
def _recent_cycles(db: str, limit: int = 30) -> list[dict]:
    return storage.recent_cycles(db, limit)


@st.cache_data(ttl=_TTL)
def _last_passing(db: str) -> dict | None:
    return storage.last_passing_cycle(db)


@st.cache_data(ttl=_TTL)
def _top_listings(db: str, limit: int = 10) -> list[dict]:
    return storage.get_listings(db, limit=limit, min_score=0)


@st.cache_data(ttl=_TTL)
def _top_gaps(db: str) -> list[dict]:
    return storage.get_latest_gap_snapshot(db)


# ---------------------------------------------------------------------------
# Small formatting helpers (pure, cannot raise on well-formed input)
# ---------------------------------------------------------------------------

def _fmt_ts(raw: str | None) -> str:
    if not raw:
        return "—"
    return str(raw)[:19].replace("T", " ") + " UTC"


def _agents_ran(cycle: dict) -> str:
    ran = cycle.get("ran") or []
    return ", ".join(r.get("agent", "?") for r in ran) if ran else "—"


def _skipped_str(cycle: dict) -> str:
    skipped = cycle.get("skipped") or []
    return ", ".join(s.get("agent", "?") for s in skipped) if skipped else "—"


def _failed_check_str(cycle: dict) -> str:
    fails = cycle.get("failed_checks") or []
    if not fails:
        return "—"
    return " · ".join(
        f"{f.get('check', '?')} (observed {f.get('observed')})" for f in fails
    )


def _verdict_str(cycle: dict) -> str:
    vp = cycle.get("verdict_passed")
    if vp is True:
        return "pass"
    if vp is False:
        return "fail"
    return "—"


def _total_duration_ms(cycle: dict) -> int:
    return sum(int(r.get("duration_ms", 0)) for r in (cycle.get("ran") or []))


def _outcome(cycle: dict) -> str:
    return cycle.get("outcome", "?")


# ---------------------------------------------------------------------------
# Guarded data load — the ONE place the DB is touched for the main panels.
# Returns (data, error_kind). error_kind is None on success, else a short
# machine tag we map to a friendly message. Never re-raises.
# ---------------------------------------------------------------------------

def _load_dashboard_data():
    try:
        db = _db_path()
    except Exception:
        logger.exception("dashboard: failed to resolve database configuration")
        return None, "not_configured"

    try:
        cycles = _recent_cycles(db, 30)
        passing = _last_passing(db)
        total, scored = _counts(db)
    except Exception:
        # Missing/unreachable/mid-migration DB all land here. Log detail
        # server-side; the user gets a clean status. Never surface `exc`.
        logger.exception("dashboard: database read failed during startup")
        return None, "unreachable"

    return {
        "db": db,
        "cycles": cycles,
        "passing": passing,
        "total": total,
        "scored": scored,
    }, None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="EdgeDash — Agent Activity", layout="wide")
st.title("EdgeDash — Agent Activity")

data, err = _load_dashboard_data()


def _render_footer(passing: dict | None) -> None:
    st.divider()
    last = _fmt_ts(passing["finished_at"]) if passing else "none yet"
    st.caption(
        f"Last successful cycle: {last}  ·  "
        f"[GitHub repo]({_GITHUB_URL})  ·  "
        "Read-only dashboard — the scheduler writes; this view only reads."
    )


# ── Hostile-startup states: render a message, never a traceback (rule 50) ────
if err == "not_configured":
    st.warning(
        "The dashboard isn't connected to a database yet. Once the "
        "`DATABASE_URL` is configured and the first cycle has run, activity "
        "will appear here."
    )
    _render_footer(None)
    st.stop()

if err == "unreachable":
    st.warning(
        "The database is not reachable right now. This is usually temporary — "
        "please refresh in a moment. If it persists, the scheduled job may "
        "not have run yet."
    )
    _render_footer(None)
    st.stop()

db = data["db"]
cycles = data["cycles"]
passing = data["passing"]
total = data["total"]
scored = data["scored"]

# ── Empty database: no cycles at all ─────────────────────────────────────────
if not cycles:
    st.info(
        f"No cycles yet — the first run is scheduled for {_FIRST_RUN_HINT}. "
        "Activity, matches, and skill gaps will appear here once it completes."
    )
    _render_footer(passing)
    st.stop()


# ── 1. Header strip ─────────────────────────────────────────────────────────
def _panel_header():
    newest = cycles[0]
    newest_failed = _outcome(newest) in ("degraded", "partial") or \
        newest.get("verdict_passed") is False

    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Last successful cycle",
              _fmt_ts(passing["finished_at"]) if passing else "none yet")
    h2.metric("Total listings", total)
    h3.metric("Total scored", scored)
    h4.metric("Verified verdict", "PASS ✓" if passing else "none yet")

    if newest_failed:
        if passing:
            st.warning(
                f"⚠ The most recent cycle ({_fmt_ts(newest['finished_at'])}) "
                f"did not pass verification (outcome: **{_outcome(newest)}**). "
                f"The panels below show data from the last verified cycle: "
                f"**{_fmt_ts(passing['finished_at'])}**."
            )
        else:
            st.error(
                f"⚠ The most recent cycle ({_fmt_ts(newest['finished_at'])}) "
                f"did not pass verification (outcome: **{_outcome(newest)}**), "
                f"and no earlier cycle has ever passed. No verified data to "
                f"show in the panels below yet."
            )


def _panel_activity_log():
    st.subheader("Agent activity log")
    st.caption(
        "Every cycle, including failed and degraded ones (rule 38 exception). "
        "Most recent 30."
    )
    log_rows = [{
        "When": _fmt_ts(c.get("finished_at")),
        "Outcome": _outcome(c),
        "Agents run": _agents_ran(c),
        "Skipped": _skipped_str(c),
        "Verdict": _verdict_str(c),
        "Failed check": _failed_check_str(c),
        "Retries": c.get("retry_count", 0),
        "Duration": f"{_total_duration_ms(c)} ms",
    } for c in cycles]

    import pandas as pd

    def _style_row(row):
        outcome = row["Outcome"]
        if outcome == "degraded":
            color = "background-color: #5c1a1a"
        elif outcome == "partial":
            color = "background-color: #5c3a1a"
        elif row["Verdict"] == "fail":
            color = "background-color: #4a1a1a"
        else:
            color = ""
        return [color] * len(row)

    styled = pd.DataFrame(log_rows).style.apply(_style_row, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True, height=520)


def _panel_top_listings():
    st.subheader("Top 10 scored listings")
    if passing is None:
        st.info("No verified cycle yet — nothing to show.")
        return
    listings = _top_listings(db, 10)
    if not listings:
        st.info("No scored listings in the verified cycle.")
        return
    st.dataframe([{
        "Score": l.get("fit_score"),
        "Title": l.get("title"),
        "Company": l.get("company"),
        "Reason": l.get("fit_reason") or "—",
    } for l in listings], use_container_width=True, hide_index=True)


def _panel_top_gaps():
    st.subheader("Top 10 skill gaps")
    if passing is None:
        st.info("No verified cycle yet — nothing to show.")
        return
    gaps = _top_gaps(db)[:10]
    if not gaps:
        st.info("No skill gaps in the verified cycle.")
        return
    st.dataframe([{
        "Skill": g.get("skill"),
        "Listings": g.get("listings_blocked"),
        "Opportunity": round(g.get("opportunity_cost", 0), 2),
        "Confidence": g.get("confidence"),
    } for g in gaps], use_container_width=True, hide_index=True)


def _safe_panel(render, label: str) -> None:
    """Run one panel; a failure logs server-side and shows a small notice,
    but never takes down the rest of the page (rule 50)."""
    try:
        render()
    except Exception:
        logger.exception("dashboard: panel %r failed to render", label)
        st.warning(f"The {label} panel is temporarily unavailable.")


_safe_panel(_panel_header, "header")
st.divider()
_safe_panel(_panel_activity_log, "activity log")
st.divider()

left, right = st.columns(2)
with left:
    _safe_panel(_panel_top_listings, "top listings")
with right:
    _safe_panel(_panel_top_gaps, "top gaps")

st.divider()


# ── 4. Ask your data ────────────────────────────────────────────────────────
# The data panels above have already rendered. Nothing in this section can
# take the dashboard down — only the ask box itself degrades.
def _panel_ask():
    st.subheader("Ask your data")
    st.caption(
        "Natural-language questions, answered only from verified data. Every "
        "answer shows the rows it came from."
    )

    def _session_rate_limited() -> float:
        now = time.time()
        stamps = [t for t in st.session_state.get("ask_stamps", [])
                  if now - t < _ASK_WINDOW_SECONDS]
        st.session_state["ask_stamps"] = stamps
        if len(stamps) >= _ASK_MAX_PER_WINDOW:
            return max(0.0, _ASK_WINDOW_SECONDS - (now - stamps[0]))
        return 0.0

    def _record() -> None:
        st.session_state.setdefault("ask_stamps", []).append(time.time())

    # Daily cap: disable the box but keep the dashboard up (rule 3).
    try:
        from edgedash.query.ask import daily_cap_reached
        capped = daily_cap_reached()
    except Exception:
        logger.exception("dashboard: daily-cap check failed")
        capped = False

    if capped:
        st.info(
            "The daily question limit has been reached, so the ask box is "
            "paused until tomorrow. All dashboard data above remains available."
        )
        return

    examples = [
        "Who is hiring in the last 7 days?",
        "What are my top skill gaps?",
        "What are my best matching jobs?",
    ]
    ex_cols = st.columns(len(examples))
    clicked = None
    for col, q in zip(ex_cols, examples):
        if col.button(q, use_container_width=True):
            clicked = q

    typed = st.text_input(
        "Your question", key="ask_input", placeholder="Ask about your data…"
    )
    question = clicked or (typed.strip() if typed else "")
    if not question:
        return

    wait = _session_rate_limited()
    if wait > 0:
        mins = int(wait // 60) + 1
        st.warning(
            f"You've reached {_ASK_MAX_PER_WINDOW} questions in "
            f"{_ASK_WINDOW_SECONDS // 60} minutes. Please wait about "
            f"{mins} more minute(s) before asking again."
        )
        return

    _record()
    from edgedash.query.ask import ask as _ask

    answer = None
    with st.spinner("Thinking…"):
        try:
            answer = _ask(question)
        except Exception:
            # Generic message only — never surface the exception text, which
            # could carry configuration detail (rule 48/50).
            logger.exception("dashboard: ask() failed")
            st.error("Couldn't answer that right now. Please try again shortly.")

    if answer is not None:
        st.markdown(f"**Q:** {question}")
        st.write(answer.text)
        if answer.rows:  # rule 44: always show the rows behind the answer
            st.caption(
                "Data behind this answer"
                + (f" · via `{answer.tool_used}`" if answer.tool_used else "")
            )
            st.dataframe(answer.rows, use_container_width=True, hide_index=True)
        elif answer.tool_used is not None:
            st.caption(f"No rows returned by `{answer.tool_used}`.")


_safe_panel(_panel_ask, "ask box")

_render_footer(passing)
