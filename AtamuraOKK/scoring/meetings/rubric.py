"""OKK rubric: versioned checklist from ``scoring/meetings/rubrics/<version>.json``.

The rubric is the single source of truth for the LLM prompt. The production ОП
rubric is ``okk_meeting_v1`` (20 criteria, max 50), transcribed from the OKK
"Чек лист встречи ОП" Excel checklist. Other rubric files serve as engine test
fixtures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RUBRICS_DIR = Path(__file__).resolve().parent / "rubrics"

# auto_check rules understood by the rubric (criteria not scored by the LLM).
_AUTO_DURATION_MAX = "duration <= 300"
_AUTO_DEFAULT_FULL = "default_full"


@dataclass(slots=True)
class Criterion:
    """One checklist criterion."""

    id: int
    block: str
    name: str
    max_score: int
    check: str
    auto_check: str | None = None  # _AUTO_DURATION_MAX | _AUTO_DEFAULT_FULL | None


@dataclass(slots=True)
class Rubric:
    """A versioned OKK checklist."""

    id: str
    version: str
    source: str
    context: str
    max_total_score: int
    criteria: list[Criterion]
    blocks: dict[str, list[int]]
    red_flags: list[str]
    raw: dict[str, Any]  # the original JSON, for the rubric_versions snapshot
    # Name of the conditional "objections" block (full marks if none arose).
    objection_block: str | None = None
    # Block names whose criteria describe the WHOLE conversation (soft skills) or
    # are conditional (objections). When a long transcript is chunked, these are
    # merged across chunks by MIN (worst chunk wins) instead of MAX, so a clean
    # chunk cannot mask bad behaviour seen in another. Stage-bound criteria
    # (greeting, closing) stay MAX — they legitimately appear in one chunk only.
    min_merge_blocks: list[str] = field(default_factory=list)

    @property
    def ai_criteria(self) -> list[Criterion]:
        """Criteria scored by the LLM (no ``auto_check`` rule)."""
        return [c for c in self.criteria if c.auto_check is None]

    @property
    def by_id(self) -> dict[int, Criterion]:
        """Criteria indexed by id."""
        return {c.id: c for c in self.criteria}

    def auto_scores(self, *, duration_sec: int) -> dict[int, int]:
        """Resolve the auto_check criteria to fixed scores.

        :param duration_sec: call duration, for the ``duration <= 300`` rule.
        :returns: {criterion_id: awarded_points} for auto criteria only.
        """
        out: dict[int, int] = {}
        for c in self.criteria:
            if c.auto_check == _AUTO_DURATION_MAX:
                out[c.id] = c.max_score if duration_sec <= 300 else 0
            elif c.auto_check == _AUTO_DEFAULT_FULL:
                out[c.id] = c.max_score
        return out

    def to_definition(self) -> dict[str, Any]:
        """Frozen JSON snapshot for the ``rubric_versions`` table."""
        return self.raw


def _parse(data: dict[str, Any]) -> Rubric:
    criteria = [
        Criterion(
            id=int(c["id"]),
            block=str(c["block"]),
            name=str(c["name"]),
            max_score=int(c["max_score"]),
            check=str(c["check"]),
            auto_check=c.get("auto_check"),
        )
        for c in data["criteria"]
    ]
    rubric = Rubric(
        id=str(data.get("id") or data["version"]),
        version=str(data["version"]),
        source=str(data.get("source", "")),
        context=str(data.get("context", "")),
        max_total_score=int(data["max_total_score"]),
        criteria=criteria,
        blocks={k: [int(i) for i in v] for k, v in data.get("blocks", {}).items()},
        red_flags=[str(f) for f in data.get("red_flags", [])],
        raw=data,
        objection_block=data.get("objection_block"),
        min_merge_blocks=[str(b) for b in data.get("min_merge_blocks", [])],
    )
    _validate(rubric)
    return rubric


def _validate(rubric: Rubric) -> None:
    total = sum(c.max_score for c in rubric.criteria)
    if total != rubric.max_total_score:
        msg = (
            f"Rubric {rubric.id}: criteria max_score sum {total} != "
            f"max_total_score {rubric.max_total_score}"
        )
        raise ValueError(msg)
    ids = [c.id for c in rubric.criteria]
    if len(ids) != len(set(ids)):
        msg = f"Rubric {rubric.id}: duplicate criterion ids"
        raise ValueError(msg)


def load_rubric(version: str) -> Rubric:
    """Load and validate a rubric by version id from the rubrics directory.

    :param version: rubric id, e.g. ``"tm_call_v2"`` (matches the file stem).
    :returns: the parsed, validated rubric.
    """
    path = _RUBRICS_DIR / f"{version}.json"
    if not path.exists():
        msg = f"Rubric file not found: {path}"
        raise FileNotFoundError(msg)
    return _parse(json.loads(path.read_text(encoding="utf-8")))
