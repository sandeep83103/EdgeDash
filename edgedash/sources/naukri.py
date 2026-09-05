"""NaukriSource — job listings from Naukri.com (India's largest job portal).

Naukri has no free public API. Two access paths are supported, in order:

  1. Apify actor (preferred): if APIFY_TOKEN is set, run a Naukri scraper actor
     on Apify. The actor id is configurable via NAUKRI_APIFY_ACTOR so you can
     swap actors without editing code. This is the reliable, hosted path.

  2. Direct endpoint (best-effort fallback): Naukri's own internal search API
     at /jobapi/v3/search, the same endpoint the website uses. It requires
     Naukri-specific headers and is not an official public API, so it may be
     blocked or change without notice. On any failure this source logs and
     returns [] — a dead or blocked source never kills the cycle (rule 12).

Credentials come from the environment only (rule 13). If neither path is
available, the source logs "skipping" and returns [] without raising.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from edgedash.sources.base import Source, register
from edgedash.sources.http import SourceError, get_json
from edgedash.sources.relevance import filter_relevant

if TYPE_CHECKING:
    from edgedash.config import Config

logger = logging.getLogger(__name__)

# Direct internal search endpoint (unofficial). Best-effort fallback only.
_SEARCH_URL = "https://www.naukri.com/jobapi/v3/search"
_RESULTS_CAP = 100                 # hard ceiling — respectful client (rule 14)

# Headers Naukri's endpoint expects. clientid/appid are the public web values
# the site itself sends; they are not secrets.
_NAUKRI_HEADERS = {
    "Accept": "application/json",
    "appid": "109",
    "systemid": "Naukri",
    "Referer": "https://www.naukri.com/",
}


@register
class NaukriSource(Source):
    name = "naukri"

    def fetch(self, config: "Config") -> list[dict]:
        token = os.environ.get("APIFY_TOKEN", "").strip()
        if token:
            return self._fetch_via_apify(config, token)
        return self._fetch_direct(config)

    # ── Path 1: Apify actor (preferred) ──────────────────────────────────────
    def _fetch_via_apify(self, config: "Config", token: str) -> list[dict]:
        from edgedash.sources.apify import _call_actor

        actor_id = os.environ.get(
            "NAUKRI_APIFY_ACTOR", "scraper-engine~naukri-job-scraper"
        ).strip()
        url = (
            f"https://api.apify.com/v2/acts/{actor_id}"
            "/run-sync-get-dataset-items"
        )
        params = {"token": token, "format": "json"}
        body = {
            "keywords": config.keywords or [config.target_role],
            "location": config.target_city,
            "maxItems": _RESULTS_CAP,
        }
        try:
            raw_items = _call_actor(params, body, override_url=url)
        except SourceError as exc:
            logger.error("naukri: apify actor failed: %s", exc)
            print(f"  [naukri] apify actor failed — skipping. ({exc})")
            return []

        print(f"  [naukri] {len(raw_items)} raw results via apify.")
        normalised = [_normalise(_from_apify(item)) for item in raw_items]
        return _keep_relevant(normalised, config)

    # ── Path 2: direct internal endpoint (best-effort) ───────────────────────
    def _fetch_direct(self, config: "Config") -> list[dict]:
        keyword = " ".join(config.keywords[:1]) or config.target_role
        params = {
            "noOfResults": _RESULTS_CAP,
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": keyword,
            "location": config.target_city,
            "seoKey": "jobs",
            "src": "directSearch",
        }
        try:
            data = get_json(_SEARCH_URL, params=params, headers=_NAUKRI_HEADERS)
        except SourceError as exc:
            # Naukri commonly blocks non-browser clients — treat as a skip,
            # not a crash. Loud in the log, quiet to the cycle (rules 6/12).
            logger.warning("naukri: direct endpoint unavailable: %s", exc)
            print(f"  [naukri] direct endpoint unavailable — skipping. ({exc})")
            return []

        jobs = data.get("jobDetails", []) if isinstance(data, dict) else []
        print(f"  [naukri] {len(jobs)} raw results via direct endpoint.")
        normalised = [_normalise(_from_direct(j)) for j in jobs]
        return _keep_relevant(normalised, config)


# ---------------------------------------------------------------------------
# Relevance filter — the actor/endpoint does not filter by keyword, so an
# off-target role (e.g. a software job) can come back. Keep only rows that
# match a configured keyword, mirroring the other sources.
# ---------------------------------------------------------------------------

def _keep_relevant(rows: list[dict], config: "Config") -> list[dict]:
    relevant = filter_relevant(rows, config)
    dropped = len(rows) - len(relevant)
    if dropped:
        print(f"  [naukri] dropped {dropped} off-keyword listing(s).")
    return relevant


# ---------------------------------------------------------------------------
# Normalisation (steering rule 10)
# ---------------------------------------------------------------------------

def _abs_url(path: str | None) -> str | None:
    """Turn a relative Naukri jdURL into an absolute, clickable link."""
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"https://www.naukri.com{path if path.startswith('/') else '/' + path}"


def _location_from_placeholders(placeholders: list | None) -> str | None:
    for ph in placeholders or []:
        if isinstance(ph, dict) and ph.get("type") == "location":
            return ph.get("label")
    return None


def _from_apify(item: dict) -> dict:
    """Map the Apify Naukri actor's nested shape onto the flat shape.

    The actor returns top-level jdURL/jobId/title/companyName plus a nested
    jobDetails object carrying placeholders (location) and the posted-date
    label. Fields are read defensively so a shape change degrades to None
    rather than raising (rules 6/10)."""
    details = item.get("jobDetails") or {}
    return {
        "jobId": item.get("jobId") or details.get("jobId"),
        "title": item.get("title") or details.get("title"),
        "company": item.get("companyName") or details.get("companyName"),
        "location": _location_from_placeholders(details.get("placeholders")),
        "url": _abs_url(item.get("jdURL") or details.get("jdURL")),
        "description": item.get("jobDescription") or details.get("jobDescription"),
        "postedAt": details.get("footerPlaceholderLabel"),
        "raw": item,
    }


def _from_direct(job: dict) -> dict:
    """Map Naukri's internal-endpoint shape onto the Apify-like flat shape."""
    return {
        "jobId": job.get("jobId"),
        "title": job.get("title"),
        "company": job.get("companyName"),
        "location": _location_from_placeholders(job.get("placeholders")),
        "url": _abs_url(job.get("jdURL")),
        "description": job.get("jobDescription"),
        "postedAt": job.get("footerPlaceholderLabel"),
        "raw": job,
    }


def _normalise(item: dict) -> dict:
    return {
        "source":      "naukri",
        "external_id": item.get("jobId") or item.get("id") or None,
        "title":       item.get("title") or None,
        "company":     item.get("company") or item.get("companyName") or None,
        "location":    item.get("location") or None,
        "url":         item.get("url") or None,
        "description": item.get("description") or None,
        "posted_at":   item.get("postedAt") or None,
        "raw":         item.get("raw") or item,
    }
