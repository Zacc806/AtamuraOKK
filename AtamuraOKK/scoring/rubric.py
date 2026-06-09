"""Load the versioned QA rubric and expose scoring helpers.

The rubric is a JSON file in the repo (``rubrics/<version>.json``) so the ОКК can
tune criteria/weights without code changes. Only ``source == "call"`` criteria are
scored in the conversational version; the final percent is over their max (91).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_RUBRIC_DIR = Path(__file__).parent / "rubrics"
DEFAULT_VERSION = "tm-call-v2"


@dataclass(frozen=True)
class Criterion:
    """One checklist item."""

    id: int
    text: str
    max: int
    source: str  # "call" (scored from transcript) | "crm" (excluded for now)
    block_id: str
    block_name: str


@dataclass
class Rubric:
    """A loaded rubric version."""

    version: str
    name: str
    zones: dict[str, int]
    raw: dict[str, Any]

    @property
    def criteria(self) -> list[Criterion]:
        """All criteria across all blocks."""
        out: list[Criterion] = []
        for block in self.raw["blocks"]:
            for c in block["criteria"]:
                out.append(
                    Criterion(
                        id=int(c["id"]),
                        text=c["text"],
                        max=int(c["max"]),
                        source=c.get("source", "call"),
                        block_id=block["id"],
                        block_name=block["name"],
                    ),
                )
        return out

    @property
    def scored_criteria(self) -> list[Criterion]:
        """Criteria scored from the transcript (conversational subset)."""
        return [c for c in self.criteria if c.source == "call"]

    @property
    def max_conversational(self) -> int:
        """Total points available from transcript-scored criteria (91)."""
        return sum(c.max for c in self.scored_criteria)

    def block_name(self, block_id: str) -> str:
        """Human name for a block id."""
        return next(
            (b["name"] for b in self.raw["blocks"] if b["id"] == block_id),
            block_id,
        )

    def zone_for(self, percent: float) -> str:
        """Map a 0-100 percent to a manager zone."""
        if percent >= self.zones["strong"]:
            return "strong"
        if percent >= self.zones["normal"]:
            return "normal"
        if percent >= self.zones["borderline"]:
            return "borderline"
        return "risk"


@lru_cache(maxsize=8)
def load_rubric(version: str = DEFAULT_VERSION) -> Rubric:
    """Load a rubric JSON by version (cached)."""
    path = _RUBRIC_DIR / f"{version.replace('-', '_')}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Rubric(
        version=raw["version"],
        name=raw["name"],
        zones=raw["zones"],
        raw=raw,
    )
