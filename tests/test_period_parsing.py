"""``okk.parse_period`` — month / day / week-range granularity.

The companion analytics filter (день / неделя / месяц) leans entirely on this
helper: every analytics block derives its [start, end) window from it, so the
day and inclusive-range formats must produce exact half-open windows in the
report timezone, stay unique per granularity (cache keys), and reject garbage
as a 422-worthy ``PeriodError``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import okk

_TZ = ZoneInfo(settings.report_timezone)


def test_month_window_unchanged() -> None:
    """A ``YYYY-MM`` spec still resolves to the whole-month window verbatim."""
    start, end, label = okk.parse_period("2026-05")
    assert start == datetime(2026, 5, 1, tzinfo=_TZ)
    assert end == datetime(2026, 6, 1, tzinfo=_TZ)
    assert label == "2026-05"


def test_month_december_rolls_year() -> None:
    """December's exclusive upper bound rolls into the next year."""
    start, end, _ = okk.parse_period("2026-12")
    assert start == datetime(2026, 12, 1, tzinfo=_TZ)
    assert end == datetime(2027, 1, 1, tzinfo=_TZ)


def test_single_day_is_one_day_window() -> None:
    """A ``YYYY-MM-DD`` spec is a half-open 24h window labelled by that day."""
    start, end, label = okk.parse_period("2026-06-15")
    assert start == datetime(2026, 6, 15, tzinfo=_TZ)
    assert end == datetime(2026, 6, 16, tzinfo=_TZ)
    assert end - start == timedelta(days=1)
    assert label == "2026-06-15"


def test_inclusive_day_range_covers_whole_week() -> None:
    """A ``from..to`` range is inclusive of both days (a Mon..Sun week = 7d)."""
    # Mon 2026-06-08 .. Sun 2026-06-14 → exclusive end is Mon 2026-06-15.
    start, end, label = okk.parse_period("2026-06-08..2026-06-14")
    assert start == datetime(2026, 6, 8, tzinfo=_TZ)
    assert end == datetime(2026, 6, 15, tzinfo=_TZ)
    assert end - start == timedelta(days=7)
    assert label == "2026-06-08..2026-06-14"


def test_labels_unique_per_granularity() -> None:
    """Month/day/week labels differ, so they never collide in the cache key."""
    month = okk.parse_period("2026-06")[2]
    day = okk.parse_period("2026-06-15")[2]
    week = okk.parse_period("2026-06-08..2026-06-14")[2]
    assert len({month, day, week}) == 3


@pytest.mark.parametrize(
    "bad",
    [
        "2026-13",  # month out of range
        "2026-06-32",  # day out of range
        "2026-06-14..2026-06-08",  # reversed range (end <= start)
        "not-a-date",
        "2026/06/15",
    ],
)
def test_invalid_specs_raise(bad: str) -> None:
    """Malformed specs raise ``PeriodError`` (surfaced by the API as 422)."""
    with pytest.raises(okk.PeriodError):
        okk.parse_period(bad)
