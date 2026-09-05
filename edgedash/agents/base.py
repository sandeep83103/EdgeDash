"""Base contract that every EdgeDash agent must satisfy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from edgedash.config import Config


Status = Literal["ok", "failed"]


@dataclass
class AgentResult:
    agent: str
    status: Status
    records_touched: int
    notes: str


@runtime_checkable
class Agent(Protocol):
    """Every agent exposes a name and a run() method.

    run() receives explicit stop_conditions from the Orchestrator (rule 29)
    and must RESPECT them rather than using its own internal limits. The
    Orchestrator owns runtime and cost bounds; the agent honours them.

    stop_conditions is a dict of limit-name -> int, e.g.
        {"max_items": 25, "max_seconds": 120}
    An agent reads the keys relevant to it and ignores the rest.
    """

    name: str

    def run(
        self,
        config: "Config",
        db_path: "Path",
        stop_conditions: dict[str, int],
    ) -> AgentResult:
        ...
