"""Track transcription-backlog progress: done/total, rate, ETA, breakdown.

Reads the running worker's log (the ``Transcribed X/Y`` lines it emits) plus a
live DB status snapshot. Use ``--watch`` for a refreshing view.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from AtamuraOKK.db.session import session_scope

_TOTAL_RE = re.compile(r"Transcribing (\d+) calls")
_DONE_RE = re.compile(
    r"^([\d-]+ [\d:.]+) .*Transcribed (\d+)/(\d+): .* -> (\w+)",
)


@dataclass
class Progress:
    """Parsed state of one transcription run."""

    total: int = 0
    done: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    @property
    def rate_per_min(self) -> float:
        """Calls finished per minute, from the log timestamps."""
        if not self.first_ts or not self.last_ts or self.done < 2:
            return 0.0
        elapsed = (self.last_ts - self.first_ts).total_seconds()
        return (self.done / elapsed * 60.0) if elapsed > 0 else 0.0

    @property
    def eta_minutes(self) -> float:
        """Estimated minutes remaining at the current rate."""
        rate = self.rate_per_min
        return (self.total - self.done) / rate if rate > 0 else 0.0


def parse_log(log_path: Path) -> Progress:
    """Parse a transcription run log into a :class:`Progress`."""
    p = Progress()
    if not log_path.exists():
        return p
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if m := _TOTAL_RE.search(line):
            p.total = int(m.group(1))
        if m := _DONE_RE.match(line):
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
            p.first_ts = p.first_ts or ts
            p.last_ts = ts
            p.done = int(m.group(2))
            p.total = p.total or int(m.group(3))
            status = m.group(4)
            p.by_status[status] = p.by_status.get(status, 0) + 1
    return p


async def db_snapshot() -> dict[str, int]:
    """Current pipeline status counts (live, authoritative)."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT status, COUNT(*) FROM calls "
                    "WHERE status IN ('DOWNLOADED','TRANSCRIBED','PENDING_KK','FAILED',"
                    "'SCORED') GROUP BY 1",
                ),
            )
        ).all()
    return {str(st): int(n) for st, n in rows}


def _bar(done: int, total: int, width: int = 30) -> str:
    filled = int(width * done / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def render(p: Progress, snap: dict[str, int]) -> str:
    """Render a compact progress block."""
    pct = (100.0 * p.done / p.total) if p.total else 0.0
    eta = p.eta_minutes
    eta_str = f"{eta / 60:.1f}h" if eta >= 60 else f"{eta:.0f}m"
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(p.by_status.items())) or "—"
    snap_str = ", ".join(f"{k}={v}" for k, v in sorted(snap.items()))
    return (
        f"Transcription {_bar(p.done, p.total)} {p.done}/{p.total} ({pct:.1f}%)\n"
        f"  this run: {breakdown}\n"
        f"  rate: {p.rate_per_min:.1f} calls/min · ETA ~{eta_str}\n"
        f"  pipeline now: {snap_str}"
    )
