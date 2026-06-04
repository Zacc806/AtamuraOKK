"""Sales-script config: the ideal call flow a manager should follow.

Versioned like rubrics (``scoring/scripts/<version>.json``). The scorer measures
how much the manager deviated from it (a second dimension alongside the rubric).
The real scripts are provided by Pavel; until ``score_script_version`` is set the
script dimension is simply skipped (:func:`load_script` returns ``None``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


@dataclass(slots=True)
class ScriptStep:
    """One step the manager is expected to perform, in order."""

    id: int
    name: str
    expected: str  # what the manager should say/do at this step


@dataclass(slots=True)
class Script:
    """A versioned sales script (ordered steps)."""

    id: str
    version: str
    source: str
    steps: list[ScriptStep]
    raw: dict[str, Any]

    def render(self) -> str:
        """Render the script steps for injection into the prompt."""
        return "\n".join(f"{s.id}. {s.name}: {s.expected}" for s in self.steps)


def _parse(data: dict[str, Any]) -> Script:
    steps = [
        ScriptStep(
            id=int(s["id"]),
            name=str(s["name"]),
            expected=str(s.get("expected", "")),
        )
        for s in data.get("steps", [])
    ]
    return Script(
        id=str(data.get("id") or data["version"]),
        version=str(data["version"]),
        source=str(data.get("source", "")),
        steps=steps,
        raw=data,
    )


def load_script(version: str) -> Script | None:
    """Load a sales script by version id, or None if unset/absent.

    :param version: script id (file stem); empty string means "no script".
    :returns: the parsed script, or None to skip the script dimension.
    """
    if not version:
        return None
    path = _SCRIPTS_DIR / f"{version}.json"
    if not path.exists():
        return None
    return _parse(json.loads(path.read_text(encoding="utf-8")))
