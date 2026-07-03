"""Load the versioned QA rubric and expose scoring helpers.

The rubric is a JSON file in the repo (``rubrics/<version>.json``) so the ОКК can
tune criteria without code changes. The active version (``tm-call-v4``) is a
**binary** checklist: every element is ДА=1 / НЕТ=0 / Н.П. (excluded). The call
score is flat — ДА ÷ applicable × 100 across all applicable elements (each element
weighs the same; blocks only group elements and handle the Н.П. rules) — see
``worker._assemble``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_RUBRIC_DIR = Path(__file__).parent / "rubrics"
DEFAULT_VERSION = "tm-call-v4"


@dataclass(frozen=True)
class Criterion:
    """One checklist item (binary: ДА=1 / НЕТ=0 / Н.П.)."""

    id: int
    text: str
    max: int
    source: str  # "call" (scored from transcript) | "crm" (excluded for now)
    block_id: str
    block_name: str
    # Prompt-rendering hints from the rubric sheet.
    yes_rule: str = ""  # when to score ДА
    no_rule: str = ""  # when to score НЕТ
    na_rule: str | None = None  # when the element is Н.П. (None -> never)
    where: str = ""  # where in the call to look

    @property
    def na_allowed(self) -> bool:
        """Whether Н.П. is a legitimate verdict for this element."""
        return bool(self.na_rule)


@dataclass(frozen=True)
class Block:
    """A rubric block (scored as one equal-weight percentage)."""

    id: str
    name: str
    criteria: list[Criterion]
    # The objections block is Н.П. as a whole when no objection occurred.
    na_if_no_objections: bool = False


@dataclass
class Rubric:
    """A loaded rubric version."""

    version: str
    name: str
    zones: dict[str, int]
    raw: dict[str, Any]

    @property
    def block_list(self) -> list[Block]:
        """Blocks in sheet order, each carrying its scored criteria."""
        out: list[Block] = []
        for block in self.raw["blocks"]:
            crits = [
                Criterion(
                    id=int(c["id"]),
                    text=c["text"],
                    max=int(c.get("max", 1)),
                    source=c.get("source", "call"),
                    block_id=block["id"],
                    block_name=block["name"],
                    yes_rule=c.get("yes", ""),
                    no_rule=c.get("no", ""),
                    na_rule=c.get("na") or None,
                    where=c.get("where", ""),
                )
                for c in block["criteria"]
                if c.get("source", "call") == "call"
            ]
            out.append(
                Block(
                    id=block["id"],
                    name=block["name"],
                    criteria=crits,
                    na_if_no_objections=bool(block.get("na_if_no_objections")),
                ),
            )
        return out

    @property
    def criteria(self) -> list[Criterion]:
        """All criteria across all blocks."""
        return [c for b in self.block_list for c in b.criteria]

    @property
    def scored_criteria(self) -> list[Criterion]:
        """Criteria scored from the transcript (all, in the binary model)."""
        return self.criteria

    @property
    def max_conversational(self) -> int:
        """Total per-element points (element count in the binary model)."""
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
