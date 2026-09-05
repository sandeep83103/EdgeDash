"""ApifySource — runs the reapx/indeed-job-search-scraper actor on Apify.

Actor: reapx/indeed-job-search-scraper (ID: 1YVeg0kdsuX5wcgZm)
Docs:  https://apify.com/reapx/indeed-job-search-scraper

Input fields:  startUrls (list[str]), maxItems (int), maxSeconds (int)
Output fields: title, company, location, url, description,
               postedAt, jobId, salary, scrapedAt, companyUrl, ...

Requires APIFY_TOKEN in environment (loaded from .env by edgedash.config).
Missing token → empty list + log line. Never raises, never crashes the cycle.
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

_ACTOR_ID = "1YVeg0kdsuX5wcgZm"          # reapx/indeed-job-search-scraper
_RUN_SYNC_URL = (
    f"https://api.apify.com/v2/acts/{_ACTOR_ID}/run-sync-get-dataset-items"
)
_MAX_ITEMS_CAP = 100     # hard ceiling — protects free-tier credits
_MAX_SECONDS = 120       # abort the actor run after this many seconds


@register
class ApifySource(Source):
    name = "apify"

    def fetch(self, config: "Config") -> list[dict]:
        token = os.environ.get("APIFY_TOKEN", "").strip()
        if not token:
            logger.info("apify: no APIFY_TOKEN in environment — skipping.")
            print("  [apify] no APIFY_TOKEN — skipping source.")
            return []

        search_term = f"{config.target_role} {config.target_city}"
        params = {"token": token, "format": "json"}
        body_params = {
            "startUrls": [search_term],
            "maxItems": min(_MAX_ITEMS_CAP, _MAX_ITEMS_CAP),  # always capped
            "maxSeconds": _MAX_SECONDS,
            "includeEmpty": False,
        }

        raw_items = _call_actor(params, body_params)
        print(
            f"  [apify] {len(raw_items)} raw results for "
            f'"{search_term}".'
        )
        normalised = [_normalise(item) for item in raw_items]
        # The actor searches on the role term and does not filter by keyword,
        # so it can return off-target roles. Keep only keyword-relevant rows.
        relevant = filter_relevant(normalised, config)
        dropped = len(normalised) - len(relevant)
        if dropped:
            print(f"  [apify] dropped {dropped} off-keyword listing(s).")
        return relevant


# ---------------------------------------------------------------------------
# HTTP call (POST with JSON body — extends get_json with body support)
# ---------------------------------------------------------------------------

def _call_actor(
    params: dict, body: dict, override_url: str | None = None
) -> list[dict]:
    """POST to a run-sync endpoint; return the dataset items list.

    override_url lets another source (e.g. Naukri) reuse this shared,
    retry-and-backoff-wrapped caller against a different actor, so all
    network calls still go through one place (rule 11)."""
    import requests  # already a project dependency

    from edgedash.sources.http import (
        _BACKOFF_BASE,
        _DEFAULT_TIMEOUT,
        _MAX_RETRIES,
        _USER_AGENT,
    )
    import time

    headers = {"User-Agent": _USER_AGENT, "Content-Type": "application/json"}
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            time.sleep(_BACKOFF_BASE ** attempt)
        try:
            resp = requests.post(
                override_url or _RUN_SYNC_URL,
                params=params,
                json=body,
                headers=headers,
                timeout=_MAX_SECONDS + 10,   # HTTP timeout > actor timeout
            )
            resp.raise_for_status()
            data = resp.json()
            # The endpoint returns a list directly.
            if isinstance(data, list):
                return data
            raise SourceError(
                f"apify: unexpected response shape — expected list, "
                f"got {type(data).__name__}"
            )
        except requests.exceptions.HTTPError as exc:
            raise SourceError(
                f"apify: HTTP {exc.response.status_code}: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            last_exc = exc

    raise SourceError(
        f"apify: failed after {_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Normalisation — maps actor output onto our schema (steering rule 10)
# ---------------------------------------------------------------------------

def _normalise(item: dict) -> dict:
    return {
        "source":      "apify",
        "external_id": item.get("jobId") or None,
        "title":       item.get("title") or None,
        "company":     item.get("company") or None,
        "location":    item.get("location") or None,
        "url":         item.get("url") or None,
        "description": item.get("description") or None,
        "posted_at":   item.get("postedAt") or None,
        "raw":         item,
    }
