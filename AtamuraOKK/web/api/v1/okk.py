"""Pure helpers for the companion contract: ОКК 1–5 + reporting-period windows.

The companion's Положение logic consumes ОКК as a **1–5 bonus modifier**, while
the pipeline stores a 0–100 percent. This module owns that mapping (and the
month-window parsing) so the value the companion sees is defined in exactly one
place. The bands align with the rubric zones (risk / borderline / normal /
strong) used everywhere else in the report layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from AtamuraOKK.scoring.rubric import load_rubric
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
    """Zone band for an aggregate percent.

    Delegates to the active call rubric's zones so the bands stay aligned with
    per-call scoring and the report layer when the rubric JSON is retuned.
    """
    if percent is None:
        return None
    return load_rubric().zone_for(percent)


class PeriodError(ValueError):
    """The ``period`` query param was not a recognised period spec."""


_PERIOD_FORMATS = "'YYYY-MM', 'YYYY-MM-DD' or 'YYYY-MM-DD..YYYY-MM-DD'"


def _next_month(start: datetime) -> datetime:
    """Midnight of the first day of the month after ``start``."""
    if start.month == 12:
        return datetime(start.year + 1, 1, 1, tzinfo=start.tzinfo)
    return datetime(start.year, start.month + 1, 1, tzinfo=start.tzinfo)


def _day_start(spec: str, tz: ZoneInfo) -> datetime:
    """Midnight at the start of a ``YYYY-MM-DD`` day in the report timezone."""
    year_s, month_s, day_s = spec.split("-")
    return datetime(int(year_s), int(month_s), int(day_s), tzinfo=tz)


def parse_period(period: str | None) -> tuple[datetime, datetime, str]:
    """Resolve a period spec to a [start, end) window in the report timezone.

    Accepts, in order of granularity:

    - ``None`` — the current month;
    - ``YYYY-MM`` — that whole month;
    - ``YYYY-MM-DD`` — that single day;
    - ``YYYY-MM-DD..YYYY-MM-DD`` — an inclusive day range (e.g. a week,
      Monday..Sunday). The upper day is made exclusive internally.

    The window is expressed in the report timezone so it lines up with the
    twice-daily reports and Metabase dashboards. The returned label is the
    canonical period string and is unique per granularity, so callers may use
    it as a cache key.
    """
    tz = ZoneInfo(settings.report_timezone)
    if period is None:
        now = datetime.now(tz=tz)
        start = datetime(now.year, now.month, 1, tzinfo=tz)
        return start, _next_month(start), f"{now.year:04d}-{now.month:02d}"

    # Guard the datetime constructors: an out-of-range field (e.g. month 13 or
    # ?period=9999999999-01) raises ValueError/OverflowError, which must surface
    # as a 422, not a 500.
    try:
        if ".." in period:  # inclusive day range — e.g. a week
            from_spec, to_spec = period.split("..", 1)
            start = _day_start(from_spec, tz)
            end = _day_start(to_spec, tz) + timedelta(days=1)
            if end <= start:
                raise ValueError
            return start, end, period
        if period.count("-") == 2:  # single day
            start = _day_start(period, tz)
            return start, start + timedelta(days=1), period
        # whole month
        year_s, month_s = period.split("-", 1)
        year, month = int(year_s), int(month_s)
        if not 1 <= month <= 12:
            raise ValueError
        start = datetime(year, month, 1, tzinfo=tz)
        return start, _next_month(start), f"{year:04d}-{month:02d}"
    except (ValueError, OverflowError) as exc:
        raise PeriodError(
            f"period must be {_PERIOD_FORMATS} (got {period!r})",
        ) from exc
