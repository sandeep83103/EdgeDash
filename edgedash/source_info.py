"""Human-readable descriptions of the configured job sources.

The dashboard shows which sources a search draws from. Source identifiers live
in config.yaml (rule 3) and map to Source classes in the SOURCES registry
(rule 9); this module only attaches a friendly label + note for display. It
adds no source-specific fetching logic — display metadata only.
"""

from __future__ import annotations

# id → (display name, short note). Unknown ids fall back to the raw id.
_LABELS: dict[str, tuple[str, str]] = {
    "arbeitnow": ("Arbeitnow", "Free public job board — no API key needed"),
    "apify": ("Apify (Indeed scraper)", "Requires APIFY_TOKEN in .env"),
    "naukri": ("Naukri.com", "India's largest job portal — via Apify, or best-effort direct"),
    "mock": ("Mock fetcher", "Offline sample data for local development"),
}


def source_display(source_id: str) -> tuple[str, str]:
    """Return (display_name, note) for a source id."""
    return _LABELS.get(source_id, (source_id, "Custom source"))


def describe_sources(source_ids: list[str], use_mock: bool) -> list[dict]:
    """Build display rows for the sources a cycle will actually query."""
    if use_mock:
        name, note = source_display("mock")
        return [{"id": "mock", "name": name, "note": note}]

    rows: list[dict] = []
    for sid in source_ids:
        name, note = source_display(sid)
        rows.append({"id": sid, "name": name, "note": note})
    return rows
