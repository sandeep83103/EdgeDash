"""MockFetcher — returns realistic fake listings without any network call.

The first four listings carry STABLE IDs so that a second run of the
cycle proves deduplication: upsert_listings will return 0 new rows for
those four on every subsequent run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.storage import make_listing_id, upsert_listings


# ---------------------------------------------------------------------------
# Helpers (defined before module-level data that uses them)
# ---------------------------------------------------------------------------

def _days_ago(n: int) -> str:
    """Return an ISO-8601 UTC timestamp for n days in the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.isoformat()


def _build_variable_listings(timestamp: str) -> list[dict]:
    """Stamp a run-time token into the variable listing URLs."""
    return [
        {**t, "url": t["url"].format(ts=timestamp)}
        for t in _VARIABLE_TEMPLATES
    ]


# ---------------------------------------------------------------------------
# Stable listings — same source+url every run, so their IDs never change.
# Four of the twelve listings live here. On run 2+ they all dedup to 0 new.
# ---------------------------------------------------------------------------

_STABLE: list[dict] = [
    {
        "title": "Lead Electrical Design Engineer",
        "company": "Wood",
        "location": "Aberdeen (Remote)",
        "url": "https://careers.woodplc.com/job/led-001",
        "source": "mock",
        "description": (
            "Lead electrical detail design for offshore oil & gas projects. "
            "SLD, load list, cable sizing, hazardous area classification (ATEX). "
            "Prepare material requisitions and technical bid evaluations."
        ),
        "posted_at": _days_ago(1),
    },
    {
        "title": "Senior Electrical Engineer — Substation Design",
        "company": "Petrofac",
        "location": "Sharjah (Remote)",
        "url": "https://careers.petrofac.com/job/see-042",
        "source": "mock",
        "description": (
            "Own HV/MV substation design and switchgear specification. "
            "Equipment sizing, earthing design, short circuit calculation. "
            "Vendor document review and IEC standards compliance required."
        ),
        "posted_at": _days_ago(2),
    },
    {
        "title": "Electrical Department Lead",
        "company": "Technip Energies",
        "location": "Remote",
        "url": "https://careers.technipenergies.com/job/edl-007",
        "source": "mock",
        "description": (
            "Lead the electrical department on FEED and detail design phases. "
            "Power layouts, lighting layouts, electrical heat tracing. "
            "Manage procurement support and purchase requisitions."
        ),
        "posted_at": _days_ago(3),
    },
    {
        "title": "Lead Electrical Engineer — EPC",
        "company": "Saipem",
        "location": "Milan (Remote)",
        "url": "https://careers.saipem.com/job/lee-011",
        "source": "mock",
        "description": (
            "Detail design lead for onshore EPC projects. "
            "SLD, load flow, protection coordination, DIALux lighting design. "
            "ETAP modelling and EPLAN experience preferred."
        ),
        "posted_at": _days_ago(1),
    },
]

# ---------------------------------------------------------------------------
# Variable listings — URL carries a run-time timestamp so they look new each
# run (simulating fresh postings from a real job board).
# ---------------------------------------------------------------------------

_VARIABLE_TEMPLATES: list[dict] = [
    {
        "title": "Lead Electrical Engineer — Renewables",
        "company": "Worley",
        "location": "Remote",
        "url": "https://worley.jobs/led-renew-{ts}",
        "source": "mock",
        "description": (
            "Lead electrical design for solar and wind projects. "
            "Power layouts, earthing design, equipment sizing. "
            "Experience with IEC standards and grid connection required."
        ),
        "posted_at": _days_ago(0),
    },
    {
        "title": "Electrical Design Engineer — Detail Design",
        "company": "Fluor",
        "location": "Remote",
        "url": "https://fluor.jobs/ede-detail-{ts}",
        "source": "mock",
        "description": (
            "Detail design for petrochemical facilities. "
            "SLD, cable routing, load schedule, hazardous area layouts. "
            "Prepare material take-off and bill of materials."
        ),
        "posted_at": _days_ago(0),
    },
    {
        "title": "Principal Electrical Engineer",
        "company": "KBR",
        "location": "Houston (Remote)",
        "url": "https://kbr.careers/pee-{ts}",
        "source": "mock",
        "description": (
            "Principal-level lead on LNG EPC projects. "
            "Substation design, protection coordination, transformer sizing. "
            "Technical bid evaluation and vendor drawing review."
        ),
        "posted_at": _days_ago(1),
    },
    {
        "title": "Electrical Department Head",
        "company": "McDermott",
        "location": "Remote",
        "url": "https://mcdermott.jobs/edh-{ts}",
        "source": "mock",
        "description": (
            "Head the electrical discipline across multiple FEED studies. "
            "Lead SLD reviews, load list ownership, electrical heat tracing. "
            "Manage procurement support and purchase requisitions."
        ),
        "posted_at": _days_ago(2),
    },
    {
        "title": "Senior Electrical Engineer — Hazardous Areas",
        "company": "Bechtel",
        "location": "Remote",
        "url": "https://bechtel.jobs/see-hazarea-{ts}",
        "source": "mock",
        "description": (
            "Specialist in hazardous area classification for oil & gas. "
            "ATEX, IECEx, zone classification, equipment sizing. "
            "DIALux lighting design and earthing grid experience a plus."
        ),
        "posted_at": _days_ago(0),
    },
    {
        "title": "Lead Electrical Engineer — Substations",
        "company": "SNC-Lavalin",
        "location": "Remote",
        "url": "https://snclavalin.jobs/lee-sub-{ts}",
        "source": "mock",
        "description": (
            "Lead HV substation design for utility clients. "
            "Switchgear design, short circuit calculation, power distribution. "
            "IEEE standards and ETAP modelling required."
        ),
        "posted_at": _days_ago(1),
    },
    {
        "title": "Electrical Engineering Team Lead",
        "company": "Jacobs",
        "location": "Remote",
        "url": "https://jacobs.jobs/eetl-{ts}",
        "source": "mock",
        "description": (
            "Lead a team of electrical engineers on infrastructure EPC. "
            "Power layouts, lighting calculations, cable sizing. "
            "EPLAN Electric P8 and AutoCAD Electrical experience preferred."
        ),
        "posted_at": _days_ago(2),
    },
    {
        "title": "Lead Electrical Design Engineer — Offshore",
        "company": "Aker Solutions",
        "location": "Oslo (Remote)",
        "url": "https://akersolutions.jobs/led-offshore-{ts}",
        "source": "mock",
        "description": (
            "Lead offshore electrical detail design. "
            "SLD, load flow, hazardous area, electric heat tracing. "
            "Material requisitions and vendor document review ownership."
        ),
        "posted_at": _days_ago(3),
    },
]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class MockFetcher:
    name: str = "MockFetcher"

    def run(
        self,
        config: Config,
        db_path: Path,
        stop_conditions: dict[str, int],
    ) -> AgentResult:
        # Respect the Orchestrator's max_listings cap (rule 29). The mock
        # always prepares 12; if the cap is lower, honour it.
        max_listings = stop_conditions.get("max_listings")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

        stable = [dict(row) for row in _STABLE]
        variable = _build_variable_listings(timestamp)
        all_listings = stable + variable  # always exactly 12

        capped = False
        if max_listings is not None and len(all_listings) > max_listings:
            all_listings = all_listings[:max_listings]
            capped = True

        # Pre-assign IDs so storage dedup is based on the correct hash.
        for listing in all_listings:
            listing.setdefault(
                "id", make_listing_id(listing["source"], listing["url"])
            )

        new_count = upsert_listings(db_path, all_listings)
        duplicate_count = len(all_listings) - new_count

        notes = (
            f"Prepared {len(all_listings)} listings — "
            f"{new_count} new, {duplicate_count} already in DB."
        )
        if capped:
            notes += f" (capped at max_listings={max_listings})"
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=new_count,
            notes=notes,
        )
