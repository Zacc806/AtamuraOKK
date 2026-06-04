"""Strip Whisper hallucinations from a transcript before scoring.

Whisper invents text on silence/quiet audio ("Спасибо за просмотр",
"подписывайтесь", duplicated farewells). Left in, these cause false scoring.
The blacklist lives in ``whisper_blacklist.json`` and is easily extended.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

_BLACKLIST_PATH = Path(__file__).resolve().parent / "whisper_blacklist.json"
_TAG = re.compile(r"^\[[^\]]+\]\s*")
_WS = re.compile(r"\s+")


@functools.lru_cache(maxsize=1)
def _blacklist() -> tuple[re.Pattern[str], ...]:
    data = json.loads(_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return tuple(
        re.compile(re.escape(p), re.IGNORECASE) for p in data.get("phrases", [])
    )


def clean_transcript(
    text: str,
    *,
    blacklist: tuple[re.Pattern[str], ...] | None = None,
) -> str:
    r"""Remove blacklisted phrases and collapse consecutive duplicate lines.

    :param text: speaker-tagged transcript ("[agent] ... \n [customer] ...").
    :param blacklist: compiled phrase patterns (defaults to the JSON blacklist).
    :returns: the cleaned transcript.
    """
    patterns = _blacklist() if blacklist is None else blacklist
    out: list[str] = []
    prev: str | None = None
    for raw_line in text.split("\n"):
        line = raw_line
        for pat in patterns:
            line = pat.sub("", line)
        line = _WS.sub(" ", line).strip()
        body = _TAG.sub("", line).strip()
        if not body:  # line was only a hallucination / speaker tag
            continue
        if line == prev:  # duplicated farewell/greeting
            continue
        out.append(line)
        prev = line
    return "\n".join(out)
