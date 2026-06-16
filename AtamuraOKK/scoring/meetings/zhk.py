"""ЖК (residential-complex) knowledge base for the manipulation detector (ТЗ 2.1).

Per-ЖК ground-truth facts (этажность, лифт, отделка, сроки сдачи, банки) live as
one JSON file per complex in ``scoring/zhk/``. The manipulation detector compares
a manager's claims against these facts; without the data the detector is inert,
so this is a config seam the business fills (kept current weekly per the ТЗ).

Files whose name starts with ``_`` are templates/examples and are NOT loaded in
production (so ``_example.json`` documents the shape without polluting the KB).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ZHK_DIR = Path(__file__).resolve().parent / "zhk"


@dataclass(slots=True)
class ZhkFacts:
    """Ground-truth facts for one ЖК (the manipulation detector's reference)."""

    name: str
    aliases: list[str] = field(default_factory=list)  # spellings managers may use
    floors: int | None = None  # этажность
    has_elevator: bool | None = None  # наличие лифта
    finishing: str = ""  # отделка (e.g. "черновая" | "чистовая")
    handover: str = ""  # срок сдачи (e.g. "4 кв. 2026")
    banks: list[str] = field(default_factory=list)  # ипотечные банки
    notes: str = ""

    def render(self) -> str:
        """One-line fact sheet for the detector prompt."""
        lift = (
            "лифт есть"
            if self.has_elevator
            else "лифта НЕТ"
            if self.has_elevator is False
            else "лифт: н/д"
        )
        floors = f"{self.floors} эт." if self.floors is not None else "этажность: н/д"
        banks = ", ".join(self.banks) or "н/д"
        return (
            f"ЖК {self.name} (варианты: {', '.join(self.aliases) or '—'}): "
            f"{floors}; {lift}; отделка: {self.finishing or 'н/д'}; "
            f"сдача: {self.handover or 'н/д'}; банки: {banks}."
            + (f" Прим.: {self.notes}" if self.notes else "")
        )


def _parse(data: dict[str, Any]) -> ZhkFacts:
    return ZhkFacts(
        name=str(data["name"]),
        aliases=[str(a) for a in data.get("aliases", [])],
        floors=int(data["floors"]) if data.get("floors") is not None else None,
        has_elevator=(
            bool(data["has_elevator"]) if data.get("has_elevator") is not None else None
        ),
        finishing=str(data.get("finishing", "")),
        handover=str(data.get("handover", "")),
        banks=[str(b) for b in data.get("banks", [])],
        notes=str(data.get("notes", "")),
    )


def load_zhk_facts(directory: Path | None = None) -> list[ZhkFacts]:
    """Load every production ЖК fact file (``*.json`` not starting with ``_``).

    :param directory: override the default ``scoring/zhk`` dir (for tests).
    :returns: parsed facts; empty list when the KB has not been populated yet.
    """
    root = directory or _ZHK_DIR
    if not root.exists():
        return []
    return [
        _parse(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(root.glob("*.json"))
        if not path.name.startswith("_")
    ]
