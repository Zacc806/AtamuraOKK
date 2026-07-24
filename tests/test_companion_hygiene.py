"""Companion /hygiene — the five «ОКК · Гигиена CRM» discipline criteria.

The hygiene view reads straight through to Bitrix (open deals + activities). These
tests fake the Bitrix client and verify each criterion's counting (status staleness
split, anketa completeness, tasks-set intersection, on-time-due split, note
detection), the gating of the config-driven criteria, the overall mean over only
the live criteria, and the endpoint's role scoping (a manager sees only their own).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1 import day, hygiene
from AtamuraOKK.web.api.v1.auth import hash_key
from AtamuraOKK.web.api.v1.schemas import ManagerRef

pytestmark = pytest.mark.anyio

_TZ = ZoneInfo(settings.report_timezone)
_PAGE = 50

# A window safely in the past (now > end) so the on-time helper has its full due
# window and a future one (now < start) so nothing has come due yet.
_PAST_START = datetime(2020, 1, 1, tzinfo=_TZ)
_PAST_END = datetime(2020, 2, 1, tzinfo=_TZ)
_FUT_START = datetime(2099, 1, 1, tzinfo=_TZ)
_FUT_END = datetime(2099, 2, 1, tzinfo=_TZ)


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    hygiene._cache.clear()


def _iso_days_ago(days: int) -> str:
    return (datetime.now(tz=_TZ) - timedelta(days=days)).isoformat()


class FakeBitrix:
    """Replays the reads the hygiene criteria make.

    ``deals`` are the open-deal rows (``list`` crm.deal.list). ``task_owners`` are
    the deal ids that have an open activity (``list`` crm.activity.list,
    COMPLETED=N). ``calls`` are completed call-activity rows (COMPLETED=Y), each
    pointing at the deal it was made on (``OWNER_ID``); ``comments`` maps a deal id
    to its timeline comments (``batch`` crm.timeline.comment.list) — together they
    drive the notes criterion. ``due`` / ``overdue`` are the envelope totals
    returned for the two on-time counts.
    """

    def __init__(
        self,
        *,
        deals: list[dict[str, Any]] | None = None,
        task_owners: list[int] | None = None,
        calls: list[dict[str, Any]] | None = None,
        comments: dict[int, list[dict[str, Any]]] | None = None,
        overdue_rows: list[dict[str, Any]] | None = None,
        due: int = 0,
        overdue: int = 0,
    ) -> None:
        self._deals = deals or []
        self._task_owners = task_owners or []
        self._calls = calls or []
        self._comments = comments or {}
        self._overdue_rows = overdue_rows or []
        self.due = due
        self.overdue = overdue
        self.seen: list[str] = []
        self.deal_filters: list[dict[str, Any]] = []

    async def call_raw(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The two crm.activity.list count envelopes (due / overdue)."""
        self.seen.append(method)
        flt = (params or {}).get("filter") or {}
        if method == "crm.activity.list":
            total = self.overdue if flt.get("COMPLETED") == "N" else self.due
            return {"result": [], "total": total}
        raise AssertionError(f"unexpected call_raw {method}")

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open deals, open-task owners, overdue rows and the calls behind the notes."""
        self.seen.append(method)
        flt = (params or {}).get("filter") or {}
        if method == "crm.deal.list":
            self.deal_filters.append(flt)
            wanted = flt.get("ID")  # _deal_titles narrows to a set of ids
            ids = {int(i) for i in wanted} if isinstance(wanted, list) else None
            for d in self._deals:
                if ids is None or int(d["ID"]) in ids:
                    yield d
            return
        if method == "crm.activity.list":
            if (
                ">=DEADLINE" in flt and flt.get("COMPLETED") == "N"
            ):  # overdue drill-down
                for row in self._overdue_rows:
                    yield row
                return
            if flt.get("COMPLETED") == "N":  # open-task owner lookup
                for owner in self._task_owners:
                    yield {"ID": "1", "OWNER_ID": owner}
                return
            for row in self._calls:  # completed calls, each on its deal
                yield row
            return
        raise AssertionError(f"unexpected list {method}")

    async def batch(
        self,
        commands: dict[str, tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Timeline comments, one command per deal id (keyed by that id)."""
        self.seen.append("batch")
        out: dict[str, Any] = {}
        for key, (method, _params) in commands.items():
            if method != "crm.timeline.comment.list":
                raise AssertionError(f"unexpected batch command {method}")
            out[key] = self._comments.get(int(key), [])
        return out


# --- statuses (stale-card proxy) --------------------------------------------


def test_statuses_splits_stale_from_maintained() -> None:
    """Open deals untouched longer than the stale window are not «maintained»."""
    deals = [
        {"ID": "1", "TITLE": "Свежая", "LAST_ACTIVITY_TIME": _iso_days_ago(1)},  # fresh
        {"ID": "2", "TITLE": "Тоже", "LAST_ACTIVITY_TIME": _iso_days_ago(2)},  # fresh
        {
            "ID": "3",
            "TITLE": "Зависла",
            "LAST_ACTIVITY_TIME": _iso_days_ago(60),
        },  # stale
        {"ID": "4", "TITLE": "Пустая", "LAST_ACTIVITY_TIME": None},  # none → stale
    ]
    crit = hygiene._statuses(deals, _FUT_END)
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (2, 4)
    assert crit.pct == 50.0
    # the two stale cards are listed for the manager to open and move
    assert {i.entity_id for i in crit.failed_items} == {3, 4}
    assert not crit.failed_truncated
    by_id = {i.entity_id: i for i in crit.failed_items}
    assert by_id[3].title == "Зависла" and "дн." in (by_id[3].detail or "")
    assert by_id[4].detail == "ни одной активности"


def test_statuses_failed_list_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """More stale cards than the cap → list is truncated but the pct stays exact."""
    monkeypatch.setattr(settings, "companion_hygiene_failed_max_items", 2)
    deals = [
        {"ID": str(i), "LAST_ACTIVITY_TIME": _iso_days_ago(60)} for i in range(1, 6)
    ]
    crit = hygiene._statuses(deals, _FUT_END)
    assert (crit.numerator, crit.denominator) == (0, 5)  # counts unaffected by the cap
    assert len(crit.failed_items) == 2
    assert crit.failed_truncated


def test_statuses_no_open_deals_is_not_available() -> None:
    """No open deals → nothing to measure, criterion is «нет данных»."""
    crit = hygiene._statuses([], _FUT_END)
    assert crit.status == "not_available"
    assert crit.pct is None


def test_statuses_bitrix_down_is_not_available() -> None:
    """A failed deal pull degrades the criterion, not the whole view."""
    assert hygiene._statuses(None, _FUT_END).status == "not_available"


def test_statuses_closed_deal_is_never_stale() -> None:
    """A card carried to won/lost was not left hanging, however old it now is."""
    deals = [
        {"ID": "1", "CLOSED": "Y", "LAST_ACTIVITY_TIME": _iso_days_ago(300)},
        {"ID": "2", "CLOSED": "N", "LAST_ACTIVITY_TIME": _iso_days_ago(300)},
    ]
    crit = hygiene._statuses(deals, _FUT_END)
    assert (crit.numerator, crit.denominator) == (1, 2)
    assert {i.entity_id for i in crit.failed_items} == {2}


def test_statuses_staleness_is_measured_at_period_end() -> None:
    """For a past period the cutoff anchors to its end, not to today.

    The card was touched the day before the period closed — fresh as of then, and
    it must stay fresh however many months have since passed.
    """
    end = datetime.now(tz=_TZ) - timedelta(days=200)
    deals = [{"ID": "1", "LAST_ACTIVITY_TIME": (end - timedelta(days=1)).isoformat()}]
    crit = hygiene._statuses(deals, end)
    assert (crit.numerator, crit.denominator) == (1, 1)
    assert crit.failed_items == []


# --- anketa (config-gated completeness) -------------------------------------


def test_anketa_unconfigured_is_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no field list configured the criterion stays «нет данных», not 0%."""
    monkeypatch.setattr(settings, "companion_anketa_fields", [])
    crit = hygiene._anketa([{"ID": "1", "STAGE_ID": "C24:PREPAYMENT_INVOIC"}])
    assert crit.status == "not_available"
    assert crit.note is not None


def test_anketa_counts_fully_filled_deals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deal is «filled» only when ALL configured fields are non-empty."""
    monkeypatch.setattr(settings, "companion_anketa_fields", ["UF_A", "UF_B"])
    qual = "C24:PREPAYMENT_INVOIC"
    deals = [
        {"ID": "1", "STAGE_ID": qual, "UF_A": "Алматы", "UF_B": "3 комн."},  # filled
        {"ID": "2", "STAGE_ID": qual, "UF_A": "Астана", "UF_B": ""},  # one empty → not
        {"ID": "3", "STAGE_ID": qual, "UF_A": "0", "UF_B": ["x"]},  # "0"/list → filled
        {"ID": "4", "STAGE_ID": qual, "UF_A": None, "UF_B": "x"},  # None → not
    ]
    crit = hygiene._anketa(deals)
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (2, 4)
    assert crit.pct == 50.0
    # the incomplete cards are listed with how many fields are still empty
    by_id = {i.entity_id: i for i in crit.failed_items}
    assert set(by_id) == {2, 4}
    assert "1 из 2" in (by_id[2].detail or "")


def test_anketa_counts_only_qualified_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Base = deals that reached qualification; leads still in dialling owe no анкета.

    Without this gate the pre-qualification cards (the bulk of an open pipeline, where
    Bitrix measures the анкета 0–8% filled) sit in the denominator and cap even a
    perfect manager near 20%.
    """
    monkeypatch.setattr(settings, "companion_anketa_fields", ["UF_A"])
    deals = [
        {"ID": "1", "STAGE_ID": "C24:PREPAYMENT_INVOIC", "UF_A": "x"},  # квал → filled
        {"ID": "2", "STAGE_ID": "C24:EXECUTING", "UF_A": ""},  # записан → empty
        {"ID": "3", "STAGE_ID": "C24:UC_9OBT14", "UF_A": "x"},  # не дошёл → filled
        {"ID": "4", "STAGE_ID": "C24:NEW", "UF_A": ""},  # новая заявка → excluded
        {"ID": "5", "STAGE_ID": "C24:UC_VL3EHH", "UF_A": ""},  # недозвон 1 → excluded
        {"ID": "6", "STAGE_ID": "C24:PREPARATION", "UF_A": ""},  # в работе → excluded
    ]
    crit = hygiene._anketa(deals)
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (2, 3)  # deals 4–6 out of the base


def test_anketa_without_qualified_deals_is_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline of nothing but fresh leads reports «нет данных», not a fake 0%."""
    monkeypatch.setattr(settings, "companion_anketa_fields", ["UF_A"])
    crit = hygiene._anketa([{"ID": "1", "STAGE_ID": "C24:NEW", "UF_A": ""}])
    assert crit.status == "not_available"
    assert crit.pct is None


def test_anketa_counts_won_deals_whatever_stage_they_now_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A won deal passed qualification, so it owes an анкета; a lost one is unknown."""
    monkeypatch.setattr(settings, "companion_anketa_fields", ["UF_A"])
    deals = [
        {"ID": "1", "STAGE_ID": "C24:WON", "STAGE_SEMANTIC_ID": "S", "UF_A": "да"},
        {"ID": "2", "STAGE_ID": "C24:WON", "STAGE_SEMANTIC_ID": "S", "UF_A": ""},
        # lost at an unknown depth — current stage no longer says how far it got
        {"ID": "3", "STAGE_ID": "C24:LOSE", "STAGE_SEMANTIC_ID": "F", "UF_A": ""},
    ]
    crit = hygiene._anketa(deals)
    assert (crit.numerator, crit.denominator) == (1, 2)
    assert [i.entity_id for i in crit.failed_items] == [2]


# --- tasks_set (open deals carrying an open activity) ------------------------


def test_tasks_set_counts_only_task_requiring_stages() -> None:
    """Base = deals on task-requiring stages; «нет задач» stages are excluded."""
    deals = [
        {"ID": "1", "STAGE_ID": "C24:UC_OPEENZ"},  # Попросил перезвонить — has task
        {"ID": "2", "STAGE_ID": "C24:PREPAYMENT_INVOIC"},  # Квалифицирован — no task
        {"ID": "3", "STAGE_ID": "C24:FINAL_INVOICE"},  # Подтверждён визит — has task
        {"ID": "4", "STAGE_ID": "C24:NEW"},  # Новая заявка — нет задач → excluded
        {"ID": "5", "STAGE_ID": "C24:UC_VL3EHH"},  # Недозвон 1 — нет задач → excluded
    ]
    crit = hygiene._tasks_set(deals, {1, 3, 99})  # 99 isn't an open deal
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (2, 3)  # deals 4 & 5 out of base
    # deal 2 requires a task but has none → it is the one listed to fix
    assert [i.entity_id for i in crit.failed_items] == [2]
    assert crit.failed_items[0].detail == "нет запланированного дела"


def test_tasks_set_no_task_requiring_stage_is_not_available() -> None:
    """Open deals only on «нет задач» stages → nothing to measure."""
    deals = [
        {"ID": "1", "STAGE_ID": "C24:NEW"},
        {"ID": "2", "STAGE_ID": "C24:UC_LS7DKY"},  # Недозвон 2
    ]
    assert hygiene._tasks_set(deals, {1, 2}).status == "not_available"


def test_tasks_set_excludes_closed_deals() -> None:
    """A closed card owes no follow-up task, so it leaves the base entirely."""
    deals = [
        {"ID": "1", "STAGE_ID": "C24:UC_OPEENZ", "CLOSED": "N"},
        {"ID": "2", "STAGE_ID": "C24:UC_OPEENZ", "CLOSED": "Y"},
    ]
    crit = hygiene._tasks_set(deals, set())
    assert (crit.numerator, crit.denominator) == (0, 1)
    assert [i.entity_id for i in crit.failed_items] == [1]


def test_tasks_set_bitrix_down_is_not_available() -> None:
    """A missing owners pull degrades the criterion to «нет данных»."""
    down = hygiene._tasks_set([{"ID": "1", "STAGE_ID": "C24:UC_OPEENZ"}], None)
    assert down.status == "not_available"


# --- tasks_on_time (due-but-not-overdue) ------------------------------------


async def test_tasks_on_time_splits_overdue() -> None:
    """Of activities already due in the period, the overdue share is the failure."""
    bx = FakeBitrix(
        due=10,
        overdue=3,
        overdue_rows=[
            {
                "ID": "77",
                "SUBJECT": "Перезвонить",
                "OWNER_ID": "501",
                "OWNER_TYPE_ID": 2,
                "DEADLINE": "2020-01-10T09:00:00+05:00",
            }
        ],
    )
    crit = await hygiene._tasks_on_time(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (7, 10)
    assert crit.pct == 70.0
    # the overdue activities are listed (linked to their deal) for the manager to close
    assert [i.entity_id for i in crit.failed_items] == [501]
    assert crit.failed_items[0].title == "Перезвонить"
    assert "2020-01-10" in (crit.failed_items[0].detail or "")
    assert crit.failed_truncated  # 3 overdue, only 1 row sampled here


async def test_tasks_on_time_future_period_has_nothing_due() -> None:
    """A future period has no deadlines yet reached → «нет данных»."""
    bx = FakeBitrix(due=0, overdue=0)
    crit = await hygiene._tasks_on_time(bx, 5, _FUT_START, _FUT_END)  # type: ignore[arg-type]
    assert crit.status == "not_available"


# --- notes (примечание по шаблону) ------------------------------------------


def _call(deal_id: int, ident: int) -> dict[str, Any]:
    return {"ID": str(ident), "OWNER_ID": str(deal_id)}


def _comment(
    author: int,
    text: str,
    when: str = "2020-01-15T10:00:00+05:00",
) -> dict[str, Any]:
    return {"ID": "1", "AUTHOR_ID": str(author), "COMMENT": text, "CREATED": when}


async def test_notes_counts_manager_note_on_the_called_deal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A called deal counts as noted when the manager wrote a comment on the card."""
    monkeypatch.setattr(settings, "companion_note_template_marker", "")
    bx = FakeBitrix(
        calls=[_call(1, 10), _call(2, 11), _call(3, 12), _call(4, 13)],
        comments={
            1: [_comment(5, "Договорились о встрече")],  # the manager's own note
            2: [],  # card left bare
            3: [_comment(9, "Автосообщение WhatsApp")],  # not his — integration
            4: [_comment(5, "Перезвонить", when="2019-12-01T10:00:00+05:00")],  # old
        },
    )
    crit = await hygiene._notes(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (1, 4)
    # the three cards missing the manager's note are listed to go back and fill
    assert {i.entity_id for i in crit.failed_items} == {2, 3, 4}
    assert all(i.detail == "нет примечания в карточке" for i in crit.failed_items)


async def test_notes_collapses_repeat_calls_to_one_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three Недозвон attempts on one lead are one card owing one note, not three."""
    monkeypatch.setattr(settings, "companion_note_template_marker", "")
    bx = FakeBitrix(
        calls=[_call(1, 10), _call(1, 11), _call(1, 12), _call(2, 13)],
        comments={1: [_comment(5, "Не берёт трубку, пишу в WhatsApp")], 2: []},
    )
    crit = await hygiene._notes(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert (crit.numerator, crit.denominator) == (1, 2)


async def test_notes_ignores_markup_only_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image-only BB-code comment is integration traffic, not a written note."""
    monkeypatch.setattr(settings, "companion_note_template_marker", "")
    bx = FakeBitrix(
        calls=[_call(1, 10), _call(2, 11)],
        comments={
            1: [_comment(5, "[img]https://static.wazzup24.com/w.png[/img]&nbsp; ")],
            2: [_comment(5, "[b]Клиент[/b] думает до пятницы")],
        },
    )
    crit = await hygiene._notes(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert (crit.numerator, crit.denominator) == (1, 2)


async def test_notes_requires_marker_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a marker set, only notes containing it count as «по шаблону»."""
    monkeypatch.setattr(settings, "companion_note_template_marker", "Итог")
    bx = FakeBitrix(
        calls=[_call(1, 10), _call(2, 11)],
        comments={
            1: [_comment(5, "Итог: клиент думает")],  # has marker
            2: [_comment(5, "перезвонить")],  # no marker
        },
    )
    crit = await hygiene._notes(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert (crit.numerator, crit.denominator) == (1, 2)


async def test_notes_unavailable_without_calls() -> None:
    """No calls on deals in the period → «нет данных», not a fake 0%."""
    crit = await hygiene._notes(FakeBitrix(), 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert crit.status == "not_available"
    assert crit.pct is None


async def test_notes_caps_the_base_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager who called more deals than the cap is measured on the latest ones."""
    monkeypatch.setattr(settings, "companion_note_template_marker", "")
    monkeypatch.setattr(settings, "companion_hygiene_notes_max_deals", 2)
    bx = FakeBitrix(
        calls=[_call(1, 10), _call(2, 11), _call(3, 12)],
        comments={1: [_comment(5, "есть")], 2: [], 3: [_comment(5, "есть")]},
    )
    crit = await hygiene._notes(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert crit.denominator == 2  # deal 3 was never read
    assert crit.note is not None and "последние" in crit.note


# --- get_hygiene aggregation -------------------------------------------------


class _FakeClientCtx:
    def __init__(self, fake: FakeBitrix) -> None:
        self._fake = fake

    async def __aenter__(self) -> FakeBitrix:
        return self._fake

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_get_hygiene_overall_is_mean_of_live_criteria(
    dbsession: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The view averages only live criteria; anketa stays unavailable (no fields)."""
    monkeypatch.setattr(settings, "companion_anketa_fields", [])
    fake = FakeBitrix(
        deals=[
            # staleness is judged as of the period's end (2020-02-01), so these
            # timestamps sit inside/outside the window relative to *that*, not today
            {
                "ID": "1",
                "STAGE_ID": "C24:UC_OPEENZ",
                "LAST_ACTIVITY_TIME": "2020-01-31T10:00:00+05:00",
            },
            {
                "ID": "2",
                "STAGE_ID": "C24:PREPAYMENT_INVOIC",
                "LAST_ACTIVITY_TIME": "2019-06-01T10:00:00+05:00",
            },
        ],
        task_owners=[1, 2],  # both deals have an open task → tasks_set 100%
        calls=[{"ID": "1", "OWNER_ID": "1"}],  # one called deal…
        comments={1: [_comment(5, "Договорились")]},  # …with the note → notes 100%
        due=4,
        overdue=1,  # tasks_on_time 75%
    )
    monkeypatch.setattr(hygiene, "BitrixClient", lambda: _FakeClientCtx(fake))

    async def _ref(_session: Any, uid: int) -> ManagerRef:
        return ManagerRef(bitrix_user_id=uid)

    monkeypatch.setattr(day, "manager_ref", _ref)

    view = await hygiene.get_hygiene(dbsession, 5, "2020-01")
    by_key = {c.key: c for c in view.criteria}
    assert by_key["statuses"].pct == 50.0
    assert by_key["anketa"].status == "not_available"
    assert by_key["tasks_set"].pct == 100.0
    assert by_key["tasks_on_time"].pct == 75.0
    assert by_key["notes"].pct == 100.0
    # mean of the four live criteria (50 + 100 + 75 + 100) / 4
    assert view.overall_pct == round((50 + 100 + 75 + 100) / 4, 1)


async def test_get_hygiene_flags_a_truncated_deal_pull(
    dbsession: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hitting the scan cap must be said out loud, not read as «посчитали всё»."""
    monkeypatch.setattr(settings, "companion_hygiene_deals_max_scan", 2)
    monkeypatch.setattr(settings, "companion_anketa_fields", ["UF_A"])
    fake = FakeBitrix(
        deals=[
            {"ID": "1", "STAGE_ID": "C24:UC_OPEENZ", "LAST_ACTIVITY_TIME": None},
            {"ID": "2", "STAGE_ID": "C24:UC_OPEENZ", "LAST_ACTIVITY_TIME": None},
        ],
        task_owners=[1, 2],
    )
    monkeypatch.setattr(hygiene, "BitrixClient", lambda: _FakeClientCtx(fake))

    async def _ref(_session: Any, uid: int) -> ManagerRef:
        return ManagerRef(bitrix_user_id=uid)

    monkeypatch.setattr(day, "manager_ref", _ref)

    view = await hygiene.get_hygiene(dbsession, 5, "2020-01")
    by_key = {c.key: c for c in view.criteria}
    assert "лимита выборки" in (by_key["statuses"].note or "")
    assert "лимита выборки" in (by_key["tasks_set"].note or "")
    # the period-windowed criteria read their own Bitrix calls — not truncated
    assert "лимита выборки" not in (by_key["notes"].note or "")


async def test_get_hygiene_scopes_the_deal_pull_to_the_period(
    dbsession: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card criteria read deals created in the period — not every open deal.

    Regression: the pull used to filter on ``CLOSED: "N"`` alone, so statuses /
    anketa / tasks_set returned byte-identical blocks for a week and for a month.
    """
    fake = FakeBitrix(deals=[], due=0, overdue=0)
    monkeypatch.setattr(hygiene, "BitrixClient", lambda: _FakeClientCtx(fake))

    async def _ref(_session: Any, uid: int) -> ManagerRef:
        return ManagerRef(bitrix_user_id=uid)

    monkeypatch.setattr(day, "manager_ref", _ref)

    await hygiene.get_hygiene(dbsession, 5, "2020-01-06..2020-01-12")
    week = fake.deal_filters[0]
    assert week[">=DATE_CREATE"] == "2020-01-06T00:00:00+05:00"
    assert week["<DATE_CREATE"] == "2020-01-13T00:00:00+05:00"  # upper day exclusive
    assert "CLOSED" not in week  # closed cards stay in the base of a past period

    fake.deal_filters.clear()
    await hygiene.get_hygiene(dbsession, 5, "2020-01")
    month = fake.deal_filters[0]
    assert month[">=DATE_CREATE"] == "2020-01-01T00:00:00+05:00"
    assert month["<DATE_CREATE"] == "2020-02-01T00:00:00+05:00"
    assert month[">=DATE_CREATE"] != week[">=DATE_CREATE"]


# --- endpoint scoping --------------------------------------------------------

_TOKEN = "test-companion-token"
_MANAGER_KEY = "mgr-hygiene-key"


@pytest.fixture
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "companion_api_token", _TOKEN)


async def _seed_manager_user(session: Any, bitrix_user_id: int) -> None:
    session.add(Manager(bitrix_user_id=bitrix_user_id, name="Олжас", last_name="М."))
    session.add(
        CompanionUser(
            key_sha256=hash_key(_MANAGER_KEY),
            role=CompanionRole.MANAGER,
            bitrix_user_id=bitrix_user_id,
            name="Олжас М.",
        ),
    )
    await session.flush()


async def test_hygiene_endpoint_scopes_to_manager(
    client: AsyncClient,
    dbsession: Any,
    _token: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager reads their own hygiene (200) but not another's (403)."""
    await _seed_manager_user(dbsession, bitrix_user_id=701)
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Companion-User-Key": _MANAGER_KEY,
    }

    async def _fake_get(
        session: Any, uid: int, period: str | None, refresh: bool = False
    ) -> Any:
        from AtamuraOKK.web.api.v1.schemas import HygieneView

        return HygieneView(manager=ManagerRef(bitrix_user_id=uid), period="2026-03")

    monkeypatch.setattr(hygiene, "get_hygiene", _fake_get)

    own = await client.get("/api/v1/managers/701/hygiene", headers=headers)
    assert own.status_code == 200
    assert own.json()["manager"]["bitrix_user_id"] == 701

    other = await client.get("/api/v1/managers/999/hygiene", headers=headers)
    assert other.status_code == 403


async def test_hygiene_requires_user_key(client: AsyncClient, _token: None) -> None:
    """The service bearer alone (no personal key) is rejected."""
    resp = await client.get(
        "/api/v1/managers/1/hygiene",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 401
