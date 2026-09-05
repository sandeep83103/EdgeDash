"""ArbeitnowSource — free public job board, no API key required.

API docs: https://www.arbeitnow.com/api/job-board-api
Response shape per job:
    slug, company_name, title, description, location, url,
    created_at (unix timestamp), tags (list[str]), remote (bool)
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from edgedash.sources.base import Source, register
from edgedash.sources.http import SourceError, get_json

if TYPE_CHECKING:
    from edgedash.config import Config

_API_URL = "https://www.arbeitnow.com/api/job-board-api"
_PAGE_CAP = 5
_MIN_RESULTS_BEFORE_RELAX = 5
_RATE_LIMIT_SECS = 1.0


@register
class ArbeitnowSource(Source):
    name = "arbeitnow"

    def fetch(self, config: "Config") -> list[dict]:
        raw_jobs = _paginate(config)
        print(f"  [arbeitnow] {len(raw_jobs)} raw results fetched.")

        # Drop non-English listings up front so German roles never enter the DB.
        english = [j for j in raw_jobs if _is_english(j)]
        dropped = len(raw_jobs) - len(english)
        if dropped:
            print(f"  [arbeitnow] dropped {dropped} non-English listing(s).")

        strict = _filter(english, config, location_strict=True)
        if len(strict) >= _MIN_RESULTS_BEFORE_RELAX:
            print(f"  [arbeitnow] {len(strict)} survived English + keyword + city filter.")
            return [_normalise(j) for j in strict]

        relaxed = _filter(english, config, location_strict=False)
        print(
            f"  [arbeitnow] City filter would leave {len(strict)} result(s) — "
            f"relaxing location filter. {len(relaxed)} survived English + keyword filter."
        )
        return [_normalise(j) for j in relaxed]


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------

def _paginate(config: "Config") -> list[dict]:
    all_jobs: list[dict] = []
    keywords = [kw.lower() for kw in config.keywords]

    for page in range(1, _PAGE_CAP + 1):
        try:
            data = get_json(_API_URL, params={"page": page})
        except SourceError:
            raise  # caller (Fetcher) handles per-source errors

        jobs: list[dict] = data.get("data", [])
        if not jobs:
            break

        all_jobs.extend(jobs)

        # Stop early if this page had no keyword matches — the feed is
        # roughly time-ordered, so further pages won't improve.
        page_matches = [j for j in jobs if _matches_keywords(j, keywords)]
        if not page_matches:
            break

        if page < _PAGE_CAP:
            time.sleep(_RATE_LIMIT_SECS)

    return all_jobs


# ---------------------------------------------------------------------------
# English-language detection
# ---------------------------------------------------------------------------

# Common German function words. If several appear as whole words in the
# title+description, the listing is German and out of scope for an English
# search. These are words that essentially never occur in English prose.
_GERMAN_MARKERS: frozenset[str] = frozenset({
    "und", "oder", "mit", "der", "die", "das", "für", "von", "im", "ist",
    "sind", "wir", "sie", "eine", "einen", "einem", "nicht", "auch", "auf",
    "als", "bei", "aus", "dem", "den", "des", "zur", "zum", "über", "durch",
    "werden", "wird", "kenntnisse", "erfahrung", "aufgaben", "unser",
    "unsere", "deine", "deinen", "sowie", "sehr", "gute",
})

_GERMAN_HITS_THRESHOLD = 4   # this many distinct markers → treat as German


def _is_english(job: dict) -> bool:
    """Heuristic: True if the listing text reads as English.

    Combines title, description, and tags, tokenises to words, and counts
    distinct German function-word markers. Four or more distinct markers
    means the posting is written in German and is dropped.
    """
    text = " ".join([
        job.get("title", "") or "",
        job.get("description", "") or "",
        " ".join(job.get("tags", []) or []),
    ]).lower()

    # Cheap tokenisation — keep latin letters plus German umlauts/ß.
    words = set(re.findall(r"[a-zäöüß]+", text))
    german_hits = len(words & _GERMAN_MARKERS)
    return german_hits < _GERMAN_HITS_THRESHOLD


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """True if any keyword appears as a WHOLE WORD/PHRASE in the listing.

    Whole-word matching is essential: the substring test previously let a
    keyword like "feed" (Front-End Engineering Design) match inside words
    such as "feedback" or "newsfeed", flooding the DB with irrelevant jobs.
    """
    haystack = " ".join([
        job.get("title", ""),
        job.get("description", ""),
        " ".join(job.get("tags", [])),
    ]).lower()
    return any(_whole_word_match(kw, haystack) for kw in keywords)


def _whole_word_match(keyword: str, haystack: str) -> bool:
    # \b anchors both ends to word boundaries. re.escape keeps phrases safe.
    pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
    return re.search(pattern, haystack) is not None


def _matches_location(job: dict, city: str) -> bool:
    location = (job.get("location") or "").lower()
    return (
        city.lower() in location
        or location == "remote"
        or job.get("remote", False)
    )


def _filter(
    jobs: list[dict],
    config: "Config",
    location_strict: bool,
) -> list[dict]:
    keywords = [kw.lower() for kw in config.keywords]
    result = []
    for job in jobs:
        if not _matches_keywords(job, keywords):
            continue
        if location_strict and not _matches_location(job, config.target_city):
            continue
        result.append(job)
    return result


# ---------------------------------------------------------------------------
# Normalisation (steering rule 10)
# ---------------------------------------------------------------------------

def _normalise(job: dict) -> dict:
    posted_at = _unix_to_iso(job.get("created_at"))
    return {
        "source":      "arbeitnow",
        "external_id": job.get("slug") or None,
        "title":       job.get("title") or None,
        "company":     job.get("company_name") or None,
        "location":    job.get("location") or None,
        "url":         job.get("url") or None,
        "description": job.get("description") or None,
        "posted_at":   posted_at,
        "raw":         job,
    }


def _unix_to_iso(value: int | str | None) -> str | None:
    """Convert a Unix timestamp (int or numeric string) to ISO-8601 UTC."""
    if value is None:
        return None
    try:
        import datetime
        return datetime.datetime.fromtimestamp(
            int(value), tz=datetime.timezone.utc
        ).isoformat()
    except (ValueError, OSError, OverflowError):
        return None
