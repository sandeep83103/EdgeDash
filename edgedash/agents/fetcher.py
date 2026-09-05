"""Fetcher agent — drives all enabled job-board sources.

Iterates config.sources, calls each Source.fetch(), merges results,
writes via storage. One dead source never kills the cycle (rule 12).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.sources.base import SOURCES
from edgedash.storage import log_cycle, make_listing_id, upsert_listings


class Fetcher:
    name: str = "Fetcher"

    def run(
        self,
        config: Config,
        db_path: Path,
        stop_conditions: dict[str, int],
    ) -> AgentResult:
        # Orchestrator-supplied limits (rule 29). max_listings caps the total
        # merged result across all sources before it reaches storage.
        max_listings = stop_conditions.get("max_listings")

        # Collect rows and per-source metadata separately so the final
        # notes string can report both total rows and genuinely new rows.
        source_rows: dict[str, list[dict]] = {}   # source_name -> rows
        source_errors: dict[str, str] = {}         # source_name -> error label

        for source_name in config.sources:
            rows, error = _fetch_source(source_name, config, db_path)
            if error is not None:
                source_errors[source_name] = error
            else:
                source_rows[source_name] = rows

        # Stamp stable ids before upsert so dedup is correct.
        all_rows: list[dict] = []
        for rows in source_rows.values():
            for row in rows:
                if not row.get("id"):
                    row["id"] = make_listing_id(row["source"], row["url"])
            all_rows.extend(rows)

        # Respect max_listings: truncate the merged batch to the cap the
        # Orchestrator gave us. We trim per-source proportionally by simply
        # capping the combined list, keeping source order stable.
        capped = False
        if max_listings is not None and len(all_rows) > max_listings:
            keep_ids = {id(r) for r in all_rows[:max_listings]}
            for name, rows in source_rows.items():
                source_rows[name] = [r for r in rows if id(r) in keep_ids]
            capped = True

        # Upsert once for all sources combined; track new count per source
        # by upserting each source's batch individually (still one DB open).
        new_per_source: dict[str, int] = {}
        for source_name, rows in source_rows.items():
            if rows:
                new_per_source[source_name] = upsert_listings(db_path, rows)
            else:
                new_per_source[source_name] = 0

        total_new = sum(new_per_source.values())

        # Build the notes string: "arbeitnow: 47 rows (12 new) | apify: FAILED (timeout)"
        parts: list[str] = []
        for source_name in config.sources:
            if source_name in source_errors:
                parts.append(f"{source_name}: FAILED ({source_errors[source_name]})")
            else:
                fetched = len(source_rows.get(source_name, []))
                new = new_per_source.get(source_name, 0)
                parts.append(f"{source_name}: {fetched} rows ({new} new)")

        notes = " | ".join(parts) if parts else "no sources configured"
        if capped:
            notes += f" | capped at max_listings={max_listings}"
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=total_new,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Per-source fetch (isolated so an exception can never escape the loop)
# ---------------------------------------------------------------------------

def _fetch_source(
    source_name: str,
    config: Config,
    db_path: Path,
) -> tuple[list[dict], str | None]:
    """Fetch one source. Returns (rows, None) on success or ([], error_label) on failure."""
    if source_name not in SOURCES:
        msg = f"not in registry — skipping"
        print(f"  ⚠  [{source_name}] {msg}")
        _log_failure(db_path, source_name, f"unknown source: {msg}")
        return [], "unknown source"

    source = SOURCES[source_name]()
    started = datetime.now(timezone.utc)

    try:
        rows = source.fetch(config)
    except Exception as exc:
        finished = datetime.now(timezone.utc)
        error_label = type(exc).__name__
        full_msg = f"{error_label}: {exc}"
        print(f"  ⚠  [{source_name}] fetch failed — {full_msg}")
        log_cycle(
            db_path,
            agent=f"Fetcher/{source_name}",
            started_at=started,
            finished_at=finished,
            records_touched=0,
            status="fail",
            notes=full_msg,
        )
        return [], error_label

    finished = datetime.now(timezone.utc)
    log_cycle(
        db_path,
        agent=f"Fetcher/{source_name}",
        started_at=started,
        finished_at=finished,
        records_touched=len(rows),
        status="pass",
        notes=f"fetched {len(rows)} rows",
    )
    return rows, None


def _log_failure(db_path: Path, source_name: str, msg: str) -> None:
    now = datetime.now(timezone.utc)
    log_cycle(
        db_path,
        agent=f"Fetcher/{source_name}",
        started_at=now,
        finished_at=now,
        records_touched=0,
        status="fail",
        notes=msg,
    )
