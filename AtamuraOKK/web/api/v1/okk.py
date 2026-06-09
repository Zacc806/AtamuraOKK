"""Pure helpers for the companion contract: ОКК 1–5 + reporting-period windows.

The companion's Положение logic consumes ОКК as a **1–5 bonus modifier**, while
the pipeline stores a 0–100 percent. This module owns that mapping (and the
month-window parsing) so the value the companion sees is defined in exactly one
place. The bands align with the rubric zones (risk / borderline / normal /
strong) used everywhere else in the report layer.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from AtamuraOKK.settings import settings


def okk_5(percent: float | None) -> int | None:
    """Map a 0–100 QA percent to the 1–5 ОКК modifier (None if no calls)."""
    if percent is None:
        return None
    if percent >= 90:
        return 5
    if percent >= 85:  # strong
        return 4
    if percent >= 80:  # normal
        return 3
    if percent >= 75:  # borderline
        return 2
    return 1  # risk


def zone_for(percent: float | None) -> str | None:
    """Zone band for an aggregate percent (mirrors reporting._zone_for)."""
    if percent is None:
        return None
    if percent >= 85:
        return "strong"
    if percent >= 80:
        return "normal"
    if percent >= 75:
        return "borderline"
    return "risk"


class PeriodError(ValueError):
    """The ``period`` query param was not a valid ``YYYY-MM``."""


def parse_period(period: str | None) -> tuple[datetime, datetime, str]:
    """Resolve a ``YYYY-MM`` (or None = current month) to a [start, end) window.

    The window is expressed in the report timezone so it lines up with the
    twice-daily reports and Metabase dashboards.
    """
    tz = ZoneInfo(settings.report_timezone)
    if period is None:
        now = datetime.now(tz=tz)
        year, month = now.year, now.month
    else:
        try:
            year_s, month_s = period.split("-", 1)
            year, month = int(year_s), int(month_s)
            if not 1 <= month <= 12:
                raise ValueError
        except ValueError as exc:
            raise PeriodError(
                f"period must be 'YYYY-MM' (got {period!r})",
            ) from exc

    start = datetime(year, month, 1, tzinfo=tz)
    # Exclusive upper bound: midnight of the first day of the next month.
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    return start, end, f"{year:04d}-{month:02d}"
