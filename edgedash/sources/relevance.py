"""Shared keyword-relevance filter for sources whose upstream API does not
filter by keyword (e.g. Apify, Naukri).

Arbeitnow filters inside its own paginator; the Apify/Naukri actors return
whatever the search term surfaced, which can include off-target roles (a broad
term like "Engineer" pulls in "Software Engineer"). This filter keeps only
listings whose title or description contains at least one configured keyword
as a whole word/phrase — the same whole-word rule Arbeitnow uses, so a keyword
like "FEED" never matches inside "feedback".

Kept separate so the Fetcher stays source-agnostic (rule 9) and every source
that needs filtering shares one implementation.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from edgedash.config import Config


def _whole_word_match(keyword: str, haystack: str) -> bool:
    pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
    return re.search(pattern, haystack) is not None


def matches_keywords(row: dict, config: "Config") -> bool:
    """True if a normalised row's title/description matches any keyword.

    Rows with no keywords configured pass through unchanged (nothing to filter
    against). Matching is case-insensitive and whole-word.
    """
    keywords = [kw.lower() for kw in config.keywords]
    if not keywords:
        return True
    haystack = " ".join([
        row.get("title") or "",
        row.get("description") or "",
    ]).lower()
    return any(_whole_word_match(kw, haystack) for kw in keywords)


def filter_relevant(rows: list[dict], config: "Config") -> list[dict]:
    """Keep only rows that match at least one configured keyword."""
    return [r for r in rows if matches_keywords(r, config)]
