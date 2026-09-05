"""Source protocol, registry, and registration decorator.

Every job-board integration is a class that satisfies Source.
The Fetcher agent iterates SOURCES and never contains board-specific logic.

To add a new source:
    1. Create edgedash/sources/mysource.py
    2. Decorate the class with @register
    3. Import the module in edgedash/sources/__init__.py (so the decorator runs)
    Nothing else needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from edgedash.config import Config

# Normalised field names every Source must return (steering rule 10).
REQUIRED_KEYS: frozenset[str] = frozenset({
    "source",
    "external_id",
    "title",
    "company",
    "location",
    "url",
    "description",
    "posted_at",
    "raw",
})


class Source(ABC):
    """Base class for all job-board sources."""

    name: str  # short identifier, e.g. "arbeitnow"

    @abstractmethod
    def fetch(self, config: "Config") -> list[dict]:
        """Return a list of normalised job dicts (see steering rule 10).

        Missing values must be None, never empty string or "N/A".
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SOURCES: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    """Class decorator that adds a Source to the global registry."""
    if not hasattr(cls, "name") or not cls.name:
        raise TypeError(f"Source class {cls.__name__} must define a non-empty `name`.")
    SOURCES[cls.name] = cls
    return cls
