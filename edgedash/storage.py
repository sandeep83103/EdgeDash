"""Storage module — the ONLY place a database driver is imported (rule 2).

Backend selection (rule 47): if DATABASE_URL is set in the environment we use
hosted Postgres; otherwise we fall back to a local SQLite file for offline
development. The active backend is logged once at import time, every run.

All SQL dialect differences (autoincrement, INSERT OR ... vs ON CONFLICT,
placeholders, GROUP_CONCAT vs STRING_AGG, multi-statement DDL) are handled
inside this module only. Every public function keeps its original signature —
callers pass a `path`/handle exactly as before and never know the backend.

CLI:
    python -m edgedash.storage --migrate   create every table (idempotent)
    python -m edgedash.storage --check     backend, connectivity, row counts
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ── Backend selection (rule 47, rule 48: env read in one place) ───────────────

_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_BACKEND = "postgres" if _DATABASE_URL else "sqlite"

# Log which backend is active, every time, at import. Never log the URL itself
# (rule 48) — only the host:port/dbname, and only for Postgres.
def _safe_pg_target(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        host = parts.hostname or "?"
        port = parts.port or 5432
        name = (parts.path or "/").lstrip("/") or "?"
        return f"{host}:{port}/{name}"
    except Exception:
        return "(unparseable target)"


if _BACKEND == "postgres":
    logger.info("storage backend: postgres → %s", _safe_pg_target(_DATABASE_URL))
else:
    logger.info("storage backend: sqlite (local file; set DATABASE_URL for postgres)")


def active_backend() -> str:
    """Return the active backend name: 'postgres' or 'sqlite'."""
    return _BACKEND


# ── Schema ────────────────────────────────────────────────────────────────────
# Written in a dialect-neutral base form; _ddl_for(backend) adapts the
# autoincrement primary keys per backend. Everything else (TEXT, INTEGER, REAL,
# CHECK, NOT NULL, DEFAULT) is valid in both SQLite and Postgres.

def _ddl_statements(backend: str) -> list[str]:
    # Autoincrement integer PK: SQLite uses INTEGER PRIMARY KEY AUTOINCREMENT;
    # Postgres uses BIGSERIAL PRIMARY KEY.
    auto_pk = (
        "INTEGER PRIMARY KEY AUTOINCREMENT"
        if backend == "sqlite"
        else "BIGSERIAL PRIMARY KEY"
    )
    return [
        f"""
        CREATE TABLE IF NOT EXISTS listings (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            company     TEXT NOT NULL,
            location    TEXT,
            url         TEXT NOT NULL,
            description TEXT,
            source      TEXT NOT NULL,
            posted_at   TEXT,
            fetched_at  TEXT NOT NULL,
            fit_score   INTEGER,
            fit_reason  TEXT,
            scored_at   TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS skill_gaps (
            skill       TEXT PRIMARY KEY,
            frequency   INTEGER NOT NULL DEFAULT 1,
            last_seen   TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS cycle_log (
            id              {auto_pk},
            agent           TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            records_touched INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL CHECK(status IN ('pass', 'fail')),
            notes           TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS extraction_cache (
            description_hash TEXT PRIMARY KEY,
            extracted_at     TEXT NOT NULL,
            result_json      TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS gap_snapshots (
            id               {auto_pk},
            run_id           TEXT NOT NULL,
            computed_at      TEXT NOT NULL,
            skill            TEXT NOT NULL,
            listings_blocked INTEGER NOT NULL,
            opportunity_cost REAL NOT NULL,
            mean_score       REAL NOT NULL,
            top_score        INTEGER NOT NULL,
            also_nice        INTEGER NOT NULL DEFAULT 0,
            confidence       TEXT NOT NULL,
            example_ids      TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS skill_classification (
            skill         TEXT PRIMARY KEY,
            is_electrical INTEGER NOT NULL,
            classified_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS query_log (
            id           {auto_pk},
            asked_at     TEXT NOT NULL,
            question     TEXT NOT NULL,
            tool_chosen  TEXT,
            params_json  TEXT NOT NULL,
            answerable   INTEGER NOT NULL,
            duration_ms  INTEGER NOT NULL
        )
        """,
    ]


# Every table name, for --check row counts and _row_count.
_TABLES = [
    "listings", "skill_gaps", "cycle_log", "extraction_cache",
    "gap_snapshots", "skill_classification", "query_log",
]


# ── Connection adapter ────────────────────────────────────────────────────────
# _connect() returns a _Conn regardless of backend. _Conn exposes the same
# surface the rest of this module already used on a sqlite3 connection —
# .execute(sql, params), .executemany(sql, seq), plus context-manager commit —
# and normalises placeholders and row access so call sites are backend-blind.

class _Cursor:
    """Thin cursor wrapper: rows come back as dict-accessible mappings."""

    def __init__(self, raw, backend: str) -> None:
        self._raw = raw
        self._backend = backend

    def fetchone(self):
        row = self._raw.fetchone()
        if row is None:
            return None
        return _Row(row, self._backend)

    def fetchall(self):
        return [_Row(r, self._backend) for r in self._raw.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount


class _Row:
    """Row supporting BOTH integer-index and column-name access on either
    backend. sqlite3.Row already does both. psycopg dict_row is a dict (name
    access only), so we synthesise positional access from insertion order —
    which matches the SELECT column order, so row[0] works uniformly.
    """

    def __init__(self, raw, backend: str) -> None:
        self._raw = raw
        self._is_dict = isinstance(raw, dict)

    def __getitem__(self, key):
        if isinstance(key, int) and self._is_dict:
            # dict preserves insertion (column) order in Python 3.7+.
            return list(self._raw.values())[key]
        return self._raw[key]

    def keys(self):
        return self._raw.keys()


class _Conn:
    """Backend-neutral connection facade."""

    def __init__(self, raw, backend: str) -> None:
        self._raw = raw
        self._backend = backend

    def _adapt(self, sql: str) -> str:
        # Postgres uses %s placeholders and %(name)s named params. Our SQL uses
        # ? and :name. Translate only for Postgres; SQLite gets the SQL as-is.
        if self._backend != "postgres":
            return sql
        import re as _re
        # Named :param → %(param)s (do this before ? handling).
        sql = _re.sub(r":(\w+)", r"%(\1)s", sql)
        # Positional ? → %s
        sql = sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Any = ()):
        cur = self._raw.cursor()
        cur.execute(self._adapt(sql), params)
        return _Cursor(cur, self._backend)

    def executemany(self, sql: str, seq: Any):
        cur = self._raw.cursor()
        cur.executemany(self._adapt(sql), list(seq))
        return _Cursor(cur, self._backend)

    def executescript(self, statements: list[str]) -> None:
        cur = self._raw.cursor()
        for stmt in statements:
            cur.execute(stmt)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def _open_sqlite(path: str | Path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _open_postgres():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - only in deployment
        raise RuntimeError(
            "DATABASE_URL is set but the 'psycopg' driver is not installed. "
            "Add psycopg[binary] to the deployment environment."
        ) from exc
    # Bounded connect so an unreachable DB fails fast (a few seconds) and the
    # dashboard can show its "database not reachable" status promptly, rather
    # than hanging on the default TCP timeout (rule 50).
    conn = psycopg.connect(
        _DATABASE_URL,
        row_factory=dict_row,
        autocommit=False,
        connect_timeout=5,
    )
    return conn


@contextmanager
def _connect(path: str | Path) -> Iterator[_Conn]:
    """Yield a backend-neutral connection, committing on clean exit.

    The `path` argument is honoured for SQLite (local file) and IGNORED for
    Postgres (connection comes from DATABASE_URL) — this keeps every existing
    call site, which passes a path, working unchanged.
    """
    if _BACKEND == "postgres":
        raw = _open_postgres()
    else:
        raw = _open_sqlite(path)
    conn = _Conn(raw, _BACKEND)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Public interface ──────────────────────────────────────────────────────────

def init_db(path: str | Path) -> None:
    """Create all tables if they do not already exist.

    Safe to call repeatedly on either backend — every statement uses
    CREATE TABLE IF NOT EXISTS, and _migrate() additively patches older
    databases created before newer columns existed.
    """
    with _connect(path) as conn:
        conn.executescript(_ddl_statements(_BACKEND))
        _migrate(conn)


def _migrate(conn: _Conn) -> None:
    """Apply additive migrations to pre-existing databases (idempotent).

    The current base DDL already includes every table and the listings
    scored_at column, so on a fresh database this is a no-op. It exists to
    patch databases created by earlier versions. Backend-aware where the two
    dialects differ.
    """
    # scored_at column for databases created before it was added to the base
    # DDL. SQLite has no ADD COLUMN IF NOT EXISTS (older versions), so we
    # attempt and swallow the "duplicate column" error; Postgres supports the
    # IF NOT EXISTS form directly.
    if _BACKEND == "postgres":
        conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS scored_at TEXT")
    else:
        try:
            conn.execute("ALTER TABLE listings ADD COLUMN scored_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists — safe to ignore


def log_query(
    path: str | Path,
    *,
    question: str,
    tool_chosen: str | None,
    params: dict[str, Any],
    answerable: bool,
    duration_ms: int,
) -> None:
    """Record one natural-language question (rule 5). Parameterised, read-safe."""
    sql = """
        INSERT INTO query_log
            (asked_at, question, tool_chosen, params_json, answerable, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    with _connect(path) as conn:
        conn.execute(sql, (
            _utcnow(),
            question,
            tool_chosen,
            json.dumps(params),
            1 if answerable else 0,
            int(duration_ms),
        ))


def count_queries_since(path: str | Path, since_iso: str) -> int:
    """Count query_log rows recorded on/after since_iso. Read-only, bound param.

    Used for the global daily cap: pass UTC-midnight ISO to get today's total.
    Counts EVERY logged question, including rejections, so a flood of abusive
    input still counts against the cap.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM query_log WHERE asked_at >= ?",
            (since_iso,),
        ).fetchone()
    return int(row[0])


def make_listing_id(source: str, url: str) -> str:
    """Return a stable, deterministic ID for a job listing.

    Using a hash of source + url ensures the same posting is never
    double-counted even if fetched on different runs.
    """
    digest = hashlib.sha256(f"{source}|{url}".encode()).hexdigest()
    return digest[:24]


def upsert_listings(path: str | Path, rows: list[dict[str, Any]]) -> int:
    """Insert new listings, silently skip duplicates.

    Returns the count of genuinely NEW rows inserted so the caller can
    observe deduplication without querying the table separately.
    """
    if not rows:
        return 0

    # Dedup on the primary key. SQLite: INSERT OR IGNORE; Postgres:
    # INSERT ... ON CONFLICT (id) DO NOTHING. Both leave existing rows intact.
    _ignore = (
        "INSERT OR IGNORE INTO listings"
        if _BACKEND == "sqlite"
        else "INSERT INTO listings"
    )
    _on_conflict = "" if _BACKEND == "sqlite" else " ON CONFLICT (id) DO NOTHING"
    sql = f"""
        {_ignore}
            (id, title, company, location, url, description,
             source, posted_at, fetched_at, fit_score, fit_reason)
        VALUES
            (:id, :title, :company, :location, :url, :description,
             :source, :posted_at, :fetched_at, :fit_score, :fit_reason){_on_conflict}
    """
    now = _utcnow()
    prepped = []
    for row in rows:
        prepped.append({
            "id":          row.get("id") or make_listing_id(row["source"], row["url"]),
            "title":       row["title"],
            "company":     row["company"],
            "location":    row.get("location"),
            "url":         row["url"],
            "description": row.get("description"),
            "source":      row["source"],
            "posted_at":   row.get("posted_at"),
            "fetched_at":  row.get("fetched_at", now),
            "fit_score":   row.get("fit_score"),
            "fit_reason":  row.get("fit_reason"),
        })

    with _connect(path) as conn:
        before = _row_count(conn, "listings")
        conn.executemany(sql, prepped)
        after = _row_count(conn, "listings")

    return after - before


def count_unscored(path: str | Path) -> int:
    """Return the number of listings that have not yet been scored."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE fit_score IS NULL"
        ).fetchone()
    return int(row[0])


def last_fetch_time(path: str | Path) -> datetime | None:
    """Return the UTC datetime of the most recent fetch, or None if empty."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) FROM listings"
        ).fetchone()
    raw: str | None = row[0]
    if raw is None:
        return None
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def last_score_time(path: str | Path) -> datetime | None:
    """Return the UTC datetime of the most recently scored listing, or None.

    Cheap: a single MAX(scored_at) aggregate, no row loads.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(scored_at) FROM listings WHERE scored_at IS NOT NULL"
        ).fetchone()
    raw: str | None = row[0]
    if raw is None:
        return None
    return _parse_iso(raw)


def gaps_computed_at(path: str | Path) -> datetime | None:
    """Return the UTC datetime of the most recent gap snapshot, or None.

    Cheap: a single MAX(computed_at) aggregate over gap_snapshots.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(computed_at) FROM gap_snapshots"
        ).fetchone()
    raw: str | None = row[0]
    if raw is None:
        return None
    return _parse_iso(raw)


def last_cycle(path: str | Path) -> tuple[str, datetime] | None:
    """Return (status, finished_at) of the most recent cycle_log row, or None.

    Cheap: one row via ORDER BY id DESC LIMIT 1. status is 'pass' or 'fail'.
    """
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT status, finished_at FROM cycle_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None or row["finished_at"] is None:
        return None
    return row["status"], _parse_iso(row["finished_at"])


def log_cycle(
    path: str | Path,
    *,
    agent: str,
    started_at: datetime,
    finished_at: datetime,
    records_touched: int,
    status: str,
    notes: str | None = None,
) -> None:
    """Write one row to cycle_log for observability and auditing."""
    if status not in ("pass", "fail"):
        raise ValueError(f"status must be 'pass' or 'fail', got {status!r}")

    sql = """
        INSERT INTO cycle_log
            (agent, started_at, finished_at, records_touched, status, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    with _connect(path) as conn:
        conn.execute(sql, (
            agent,
            started_at.isoformat(),
            finished_at.isoformat(),
            records_touched,
            status,
            notes,
        ))


def last_passing_cycle(path: str | Path) -> dict[str, Any] | None:
    """Return the most recent cycle whose verification verdict PASSED (rule 38).

    Looks only at cycle-summary rows (agent='cycle') and inspects the JSON
    notes for a truthy "verdict_passed". Returns a dict with the parsed
    summary plus finished_at, or None if no cycle has ever passed verification.

    The dashboard reads ONLY the cycle this returns — a failed/degraded cycle
    never overwrites the last known-good data. Stale verified beats fresh
    unverified.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT finished_at, notes FROM cycle_log "
            "WHERE agent = 'cycle' ORDER BY id DESC"
        ).fetchall()

    for row in rows:
        if not row["notes"]:
            continue
        try:
            summary = json.loads(row["notes"])
        except (json.JSONDecodeError, TypeError):
            continue
        if summary.get("verdict_passed") is True:
            summary["finished_at"] = row["finished_at"]
            return summary
    return None


def recent_cycles(path: str | Path, limit: int = 30) -> list[dict[str, Any]]:
    """Return the most recent cycle-summary rows (agent='cycle'), newest first.

    Read-only. Unlike last_passing_cycle, this returns ALL cycles including
    failed and degraded ones — the activity log needs to show failures
    (the rule 38 exception). Each item is the parsed JSON summary plus the
    row's finished_at and cycle_log status.
    """
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT finished_at, status, notes FROM cycle_log "
            "WHERE agent = 'cycle' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            summary = json.loads(row["notes"]) if row["notes"] else {}
        except (json.JSONDecodeError, TypeError):
            summary = {}
        summary["finished_at"] = row["finished_at"]
        summary["log_status"] = row["status"]
        out.append(summary)
    return out


def count_listings(path: str | Path) -> tuple[int, int]:
    """Return (total_listings, total_scored). Cheap COUNT aggregates."""
    with _connect(path) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0])
        scored = int(
            conn.execute(
                "SELECT COUNT(*) FROM listings WHERE fit_score IS NOT NULL"
            ).fetchone()[0]
        )
    return total, scored


def get_listings(
    path: str | Path,
    *,
    limit: int = 100,
    min_score: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch listings, optionally filtered by minimum fit score."""
    if min_score is not None:
        sql = """
            SELECT * FROM listings
            WHERE fit_score >= ?
            ORDER BY fit_score DESC, fetched_at DESC
            LIMIT ?
        """
        params: tuple[Any, ...] = (min_score, limit)
    else:
        sql = """
            SELECT * FROM listings
            ORDER BY fetched_at DESC
            LIMIT ?
        """
        params = (limit,)

    with _connect(path) as conn:
        rows = conn.execute(sql, params).fetchall()

    return [dict(row) for row in rows]


def get_unscored_listings(
    path: str | Path,
    limit: int,
) -> list[dict[str, Any]]:
    """Return listings where fit_score IS NULL, oldest-fetched first."""
    sql = """
        SELECT * FROM listings
        WHERE fit_score IS NULL
        ORDER BY fetched_at ASC
        LIMIT ?
    """
    with _connect(path) as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(row) for row in rows]


def save_score(
    path: str | Path,
    listing_id: str,
    score: int,
    reason: str,
) -> None:
    """Write fit_score, fit_reason, and scored_at for one listing."""
    sql = """
        UPDATE listings
        SET fit_score  = ?,
            fit_reason = ?,
            scored_at  = ?
        WHERE id = ?
    """
    with _connect(path) as conn:
        conn.execute(sql, (score, reason, _utcnow(), listing_id))


def clear_score(path: str | Path, listing_id: str) -> bool:
    """Clear the score fields for one listing. Returns True if a row was found."""
    sql = """
        UPDATE listings
        SET fit_score  = NULL,
            fit_reason = NULL,
            scored_at  = NULL
        WHERE id = ?
    """
    with _connect(path) as conn:
        cursor = conn.execute(sql, (listing_id,))
        return cursor.rowcount > 0


def clear_all_scores(path: str | Path) -> int:
    """Clear score fields on every listing. Returns the count of rows updated."""
    sql = """
        UPDATE listings
        SET fit_score  = NULL,
            fit_reason = NULL,
            scored_at  = NULL
        WHERE fit_score IS NOT NULL
           OR fit_reason IS NOT NULL
           OR scored_at  IS NOT NULL
    """
    with _connect(path) as conn:
        cursor = conn.execute(sql)
        return cursor.rowcount


def get_scored_with_extractions(
    path: str | Path,
) -> list[dict[str, Any]]:
    """Return every scored listing joined with its cached extraction facts.

    Listings with no fit_score or no extraction cache entry are excluded —
    the GapAnalyzer only works on fully-processed rows.
    """
    sql = """
        SELECT
            l.id, l.fit_score, l.title, l.company,
            ec.result_json
        FROM listings l
        JOIN extraction_cache ec ON ec.description_hash = (
            SELECT description_hash
            FROM extraction_cache
            -- match on a re-hash of the stripped description stored at
            -- extract time — we join via the hash that was stored
            LIMIT 0  -- placeholder; real join is done in Python below
        )
        WHERE l.fit_score IS NOT NULL
    """
    # The extraction_cache is keyed on description hash, not listing id.
    # We must re-hash in Python to join. Fetch both tables separately.
    with _connect(path) as conn:
        listings = conn.execute(
            "SELECT id, fit_score, title, company, description "
            "FROM listings WHERE fit_score IS NOT NULL"
        ).fetchall()
        cache_rows = conn.execute(
            "SELECT description_hash, result_json FROM extraction_cache"
        ).fetchall()

    cache: dict[str, dict] = {}
    for row in cache_rows:
        try:
            cache[row["description_hash"]] = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    import hashlib
    import re as _re
    _tag_re = _re.compile(r"<[^>]+>")
    _sp_re  = _re.compile(r"\s+")

    result: list[dict[str, Any]] = []
    for listing in listings:
        raw_desc = listing["description"] or ""
        clean = _sp_re.sub(" ", _tag_re.sub(" ", raw_desc)).strip()
        desc_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        facts = cache.get(desc_hash)
        if facts is None:
            continue
        result.append({
            "id":        listing["id"],
            "fit_score": listing["fit_score"],
            "title":     listing["title"],
            "company":   listing["company"],
            "facts":     facts,
        })
    return result


def save_gap_snapshot(
    path: str | Path,
    run_id: str,
    gaps: list[dict[str, Any]],
) -> None:
    """Append a gap snapshot for this run. Never overwrites previous runs (rule 25)."""
    if not gaps:
        return
    computed_at = _utcnow()
    sql = """
        INSERT INTO gap_snapshots
            (run_id, computed_at, skill, listings_blocked,
             opportunity_cost, mean_score, top_score,
             also_nice, confidence, example_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = [
        (
            run_id,
            computed_at,
            g["skill"],
            g["listings_blocked"],
            round(g["opportunity_cost"], 4),
            round(g["mean_score"], 2),
            g["top_score"],
            g.get("also_nice", 0),
            g["confidence"],
            json.dumps(g["example_ids"]),
        )
        for g in gaps
    ]
    with _connect(path) as conn:
        conn.executemany(sql, rows)


def get_latest_gap_snapshot(
    path: str | Path,
) -> list[dict[str, Any]]:
    """Return all rows from the most recent gap snapshot, ranked by opportunity_cost."""
    return _gap_snapshot_by_order(path, newest=True)


def get_earliest_gap_snapshot(
    path: str | Path,
) -> list[dict[str, Any]]:
    """Return all rows from the oldest gap snapshot, ranked by opportunity_cost."""
    return _gap_snapshot_by_order(path, newest=False)


def count_gap_snapshots(path: str | Path) -> int:
    """Return the number of distinct gap-analysis runs recorded."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT run_id) FROM gap_snapshots"
        ).fetchone()
    return int(row[0])


def _gap_snapshot_by_order(
    path: str | Path,
    newest: bool,
) -> list[dict[str, Any]]:
    order = "DESC" if newest else "ASC"
    with _connect(path) as conn:
        picked = conn.execute(
            f"SELECT run_id FROM gap_snapshots ORDER BY computed_at {order} LIMIT 1"
        ).fetchone()
        if picked is None:
            return []
        rows = conn.execute(
            """
            SELECT skill, listings_blocked, opportunity_cost,
                   mean_score, top_score, also_nice, confidence,
                   example_ids, computed_at
            FROM gap_snapshots
            WHERE run_id = ?
            ORDER BY opportunity_cost DESC
            """,
            (picked["run_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Extraction cache ─────────────────────────────────────────────────────────

def get_cached_extraction(
    path: str | Path,
    description_hash: str,
) -> dict[str, Any] | None:
    """Return cached extraction result for a description hash, or None."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT result_json FROM extraction_cache WHERE description_hash = ?",
            (description_hash,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["result_json"])


def get_cached_skill_class(
    path: str | Path,
    skill: str,
) -> bool | None:
    """Return cached electrical/non-electrical verdict for a skill, or None."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT is_electrical FROM skill_classification WHERE skill = ?",
            (skill,),
        ).fetchone()
    if row is None:
        return None
    return bool(row["is_electrical"])


def store_skill_class(
    path: str | Path,
    skill: str,
    is_electrical: bool,
) -> None:
    """Persist a skill classification verdict, keyed on the skill string."""
    if _BACKEND == "sqlite":
        sql = """
            INSERT OR REPLACE INTO skill_classification
                (skill, is_electrical, classified_at)
            VALUES (?, ?, ?)
        """
    else:
        sql = """
            INSERT INTO skill_classification
                (skill, is_electrical, classified_at)
            VALUES (?, ?, ?)
            ON CONFLICT (skill) DO UPDATE SET
                is_electrical = EXCLUDED.is_electrical,
                classified_at = EXCLUDED.classified_at
        """
    with _connect(path) as conn:
        conn.execute(sql, (skill, 1 if is_electrical else 0, _utcnow()))


def store_extraction(
    path: str | Path,
    description_hash: str,
    result: dict[str, Any],
) -> None:
    """Persist an extraction result keyed on description hash."""
    if _BACKEND == "sqlite":
        sql = """
            INSERT OR REPLACE INTO extraction_cache
                (description_hash, extracted_at, result_json)
            VALUES (?, ?, ?)
        """
    else:
        sql = """
            INSERT INTO extraction_cache
                (description_hash, extracted_at, result_json)
            VALUES (?, ?, ?)
            ON CONFLICT (description_hash) DO UPDATE SET
                extracted_at = EXCLUDED.extracted_at,
                result_json  = EXCLUDED.result_json
        """
    with _connect(path) as conn:
        conn.execute(sql, (
            description_hash,
            _utcnow(),
            json.dumps(result),
        ))


# ── Diagnostic queries (read-only) ───────────────────────────────────────────

def count_listings_by_source(path: str | Path) -> dict[str, int]:
    """Return {source: count} for every source in the listings table."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM listings GROUP BY source ORDER BY source"
        ).fetchall()
    return {row["source"]: row["n"] for row in rows}


def cross_source_duplicates(path: str | Path) -> list[dict[str, Any]]:
    """Return (title, company, sources, count) for jobs seen on >1 source.

    A cross-source duplicate is a (title, company) pair that appears in
    at least two distinct sources. These are probable duplicate listings
    that dedup by URL alone cannot catch.
    """
    # SQLite aggregates distinct strings with GROUP_CONCAT; Postgres uses
    # STRING_AGG(DISTINCT col, ','). Same result shape either way.
    _agg = (
        "GROUP_CONCAT(DISTINCT source)"
        if _BACKEND == "sqlite"
        else "STRING_AGG(DISTINCT source, ',')"
    )
    sql = f"""
        SELECT
            title,
            company,
            {_agg} AS sources,
            COUNT(DISTINCT source)        AS source_count,
            COUNT(*)                      AS total_rows
        FROM listings
        GROUP BY LOWER(title), LOWER(company)
        HAVING COUNT(DISTINCT source) > 1
        ORDER BY source_count DESC, total_rows DESC
    """
    with _connect(path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


def recent_listings(path: str | Path, limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recently fetched listings, newest first."""
    sql = """
        SELECT source, title, company, fetched_at
        FROM listings
        ORDER BY fetched_at DESC
        LIMIT ?
    """
    with _connect(path) as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(row) for row in rows]


def listings_with_bad_fields(path: str | Path) -> list[dict[str, Any]]:
    """Return listings where url, title, or company is NULL or empty string."""
    sql = """
        SELECT id, source, title, company, url
        FROM listings
        WHERE
            url     IS NULL OR TRIM(url)     = ''
            OR title   IS NULL OR TRIM(title)   = ''
            OR company IS NULL OR TRIM(company) = ''
        ORDER BY fetched_at DESC
    """
    with _connect(path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


# ── Query-tool read helpers (read-only, parameterised — rules 40, 41) ─────────
# Each takes typed params bound via placeholders. No string interpolation of
# any caller-supplied value ever reaches SQL.

def companies_since(path: str | Path, since_iso: str) -> list[dict[str, Any]]:
    """Companies with listings posted on/after since_iso, with counts.

    `since_iso` is an ISO-8601 timestamp computed by the caller (never a raw
    model value). Compared against posted_at; rows with a NULL posted_at are
    excluded because we cannot place them in the window.
    """
    sql = """
        SELECT company, COUNT(*) AS n
        FROM listings
        WHERE posted_at IS NOT NULL AND posted_at >= ?
        GROUP BY company
        ORDER BY n DESC, company ASC
    """
    with _connect(path) as conn:
        rows = conn.execute(sql, (since_iso,)).fetchall()
    return [dict(r) for r in rows]


def listings_by_ids(path: str | Path, ids: list[str]) -> list[dict[str, Any]]:
    """Return scored listing rows for a set of listing IDs, highest score first.

    The IN clause is built from one placeholder per id — the ids are bound,
    never interpolated. Used by the gap drill-down (rule 26).
    """
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        "SELECT id, title, company, fit_score, fit_reason "
        "FROM listings WHERE id IN (" + placeholders + ") "
        "ORDER BY fit_score DESC"
    )
    with _connect(path) as conn:
        rows = conn.execute(sql, tuple(ids)).fetchall()
    return [dict(r) for r in rows]


def gap_trend_rows(
    path: str | Path,
    skill: str,
    since_iso: str,
) -> list[dict[str, Any]]:
    """Per-snapshot opportunity_cost for one skill since since_iso, oldest first.

    Both skill and since_iso are bound parameters. Returns one row per gap
    snapshot in the window in which the skill appeared.
    """
    sql = """
        SELECT computed_at, opportunity_cost, listings_blocked
        FROM gap_snapshots
        WHERE skill = ? AND computed_at >= ?
        ORDER BY computed_at ASC
    """
    with _connect(path) as conn:
        rows = conn.execute(sql, (skill, since_iso)).fetchall()
    return [dict(r) for r in rows]


def skill_demand_counts(path: str | Path, canonical_skill: str) -> dict[str, int]:
    """Count how often canonical_skill appears in required vs nice_to_have.

    Reads extraction_cache and canonicalises each stored skill string through
    the SAME canonical() the caller used, so matching is consistent. The skill
    is compared in Python, never interpolated into SQL.
    """
    from edgedash.config import load_config
    from edgedash.skills import canonical

    aliases = load_config().skill_aliases

    with _connect(path) as conn:
        cache_rows = conn.execute(
            "SELECT result_json FROM extraction_cache"
        ).fetchall()

    required = 0
    nice = 0
    for row in cache_rows:
        try:
            facts = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        for s in facts.get("required_skills") or []:
            if isinstance(s, str) and canonical(s, aliases) == canonical_skill:
                required += 1
        for s in facts.get("nice_to_have") or []:
            if isinstance(s, str) and canonical(s, aliases) == canonical_skill:
                nice += 1

    return {"required": required, "nice_to_have": nice}


# ── Private helpers ───────────────────────────────────────────────────────────

def _row_count(conn: "_Conn", table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(raw: str) -> datetime:
    """Parse a stored ISO-8601 timestamp, always returning a UTC-aware datetime.

    Handles both offset-aware strings (from .isoformat() on aware datetimes)
    and naive strings (older rows), coercing naive to UTC.
    """
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── CLI: --migrate / --check ──────────────────────────────────────────────────

def _resolve_path() -> str:
    """SQLite file path from config (ignored by Postgres). Local import keeps
    storage importable without a config present in some contexts."""
    try:
        from edgedash.config import load_config
        return str(load_config().abs_db_path)
    except Exception:
        return "edgedash.db"


def migrate() -> None:
    """Create every table on the active backend. Safe to run repeatedly."""
    path = _resolve_path()
    init_db(path)


def check() -> dict[str, Any]:
    """Return backend, connectivity, and per-table row counts.

    Never raises on a connection failure — reports it, so a broken database
    degrades to a status message rather than a traceback (rule 50).
    """
    path = _resolve_path()
    report: dict[str, Any] = {"backend": _BACKEND, "connected": False, "tables": {}}
    if _BACKEND == "postgres":
        report["target"] = _safe_pg_target(_DATABASE_URL)
    else:
        report["target"] = path
    try:
        with _connect(path) as conn:
            for table in _TABLES:
                try:
                    report["tables"][table] = _row_count(conn, table)
                except Exception as exc:  # table may not exist yet
                    report["tables"][table] = f"(error: {type(exc).__name__})"
        report["connected"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if "--migrate" in sys.argv:
        print(f"backend: {_BACKEND}")
        migrate()
        print("migrate: all tables ensured (idempotent).")
    elif "--check" in sys.argv:
        rep = check()
        print(f"backend   : {rep['backend']}")
        print(f"target    : {rep.get('target')}")
        print(f"connected : {rep['connected']}")
        if "error" in rep:
            print(f"error     : {rep['error']}")
        else:
            print("row counts:")
            for table, n in rep["tables"].items():
                print(f"  {table:<22} {n}")
    else:
        print("Usage: python -m edgedash.storage [--migrate | --check]")
