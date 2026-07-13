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
    COMPLETED=N). ``notes`` are completed call-activity rows (COMPLETED=Y). ``due``
    / ``overdue`` are the envelope totals returned for the two on-time counts.
    """

    def __init__(
        self,
        *,
        deals: list[dict[str, Any]] | None = None,
        task_owners: list[int] | None = None,
        notes: list[dict[str, Any]] | None = None,
        due: int = 0,
        overdue: int = 0,
    ) -> None:
        self._deals = deals or []
        self._task_owners = task_owners or []
        self._notes = notes or []
        self.due = due
        self.overdue = overdue
        self.seen: list[str] = []

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
        """Open deals, open-task owners and completed call-activity note rows."""
        self.seen.append(method)
        flt = (params or {}).get("filter") or {}
        if method == "crm.deal.list":
            for d in self._deals:
                yield d
            return
        if method == "crm.activity.list":
            if flt.get("COMPLETED") == "N":  # open-task owner lookup
                for owner in self._task_owners:
                    yield {"ID": "1", "OWNER_ID": owner}
                return
            for row in self._notes:  # completed call activities (notes)
                yield row
            return
        raise AssertionError(f"unexpected list {method}")


# --- statuses (stale-card proxy) --------------------------------------------


def test_statuses_splits_stale_from_maintained() -> None:
    """Open deals untouched longer than the stale window are not «maintained»."""
    deals = [
        {"ID": "1", "LAST_ACTIVITY_TIME": _iso_days_ago(1)},  # fresh
        {"ID": "2", "LAST_ACTIVITY_TIME": _iso_days_ago(2)},  # fresh
        {"ID": "3", "LAST_ACTIVITY_TIME": _iso_days_ago(60)},  # stale
        {"ID": "4", "LAST_ACTIVITY_TIME": None},  # no activity → stale
    ]
    crit = hygiene._statuses(deals)
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (2, 4)
    assert crit.pct == 50.0


def test_statuses_no_open_deals_is_not_available() -> None:
    """No open deals → nothing to measure, criterion is «нет данных»."""
    crit = hygiene._statuses([])
    assert crit.status == "not_available"
    assert crit.pct is None


def test_statuses_bitrix_down_is_not_available() -> None:
    """A failed deal pull degrades the criterion, not the whole view."""
    assert hygiene._statuses(None).status == "not_available"


# --- anketa (config-gated completeness) -------------------------------------


def test_anketa_unconfigured_is_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no field list configured the criterion stays «нет данных», not 0%."""
    monkeypatch.setattr(settings, "companion_anketa_fields", [])
    crit = hygiene._anketa([{"ID": "1"}])
    assert crit.status == "not_available"
    assert crit.note is not None


def test_anketa_counts_fully_filled_deals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deal is «filled» only when ALL configured fields are non-empty."""
    monkeypatch.setattr(settings, "companion_anketa_fields", ["UF_A", "UF_B"])
    deals = [
        {"ID": "1", "UF_A": "Алматы", "UF_B": "3 комн."},  # both → filled
        {"ID": "2", "UF_A": "Астана", "UF_B": ""},  # one empty → not
        {"ID": "3", "UF_A": "0", "UF_B": ["x"]},  # "0" and list count as filled
        {"ID": "4", "UF_A": None, "UF_B": "x"},  # None → not
    ]
    crit = hygiene._anketa(deals)
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (2, 4)
    assert crit.pct == 50.0


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


def test_tasks_set_no_task_requiring_stage_is_not_available() -> None:
    """Open deals only on «нет задач» stages → nothing to measure."""
    deals = [
        {"ID": "1", "STAGE_ID": "C24:NEW"},
        {"ID": "2", "STAGE_ID": "C24:UC_LS7DKY"},  # Недозвон 2
    ]
    assert hygiene._tasks_set(deals, {1, 2}).status == "not_available"


def test_tasks_set_bitrix_down_is_not_available() -> None:
    """A missing owners pull degrades the criterion to «нет данных»."""
    down = hygiene._tasks_set([{"ID": "1", "STAGE_ID": "C24:UC_OPEENZ"}], None)
    assert down.status == "not_available"


# --- tasks_on_time (due-but-not-overdue) ------------------------------------


async def test_tasks_on_time_splits_overdue() -> None:
    """Of activities already due in the period, the overdue share is the failure."""
    bx = FakeBitrix(due=10, overdue=3)
    crit = await hygiene._tasks_on_time(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (7, 10)
    assert crit.pct == 70.0


async def test_tasks_on_time_future_period_has_nothing_due() -> None:
    """A future period has no deadlines yet reached → «нет данных»."""
    bx = FakeBitrix(due=0, overdue=0)
    crit = await hygiene._tasks_on_time(bx, 5, _FUT_START, _FUT_END)  # type: ignore[arg-type]
    assert crit.status == "not_available"


# --- notes (примечание по шаблону) ------------------------------------------


async def test_notes_counts_any_nonempty_without_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no marker, any non-empty call note counts toward the criterion."""
    monkeypatch.setattr(settings, "companion_note_template_marker", "")
    bx = FakeBitrix(
        notes=[
            {"ID": "1", "DESCRIPTION": "Договорились о встрече"},
            {"ID": "2", "DESCRIPTION": "   "},  # blank → no note
            {"ID": "3", "DESCRIPTION": "перезвонить"},
            {"ID": "4"},  # missing → no note
        ],
    )
    crit = await hygiene._notes(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert crit.status == "live"
    assert (crit.numerator, crit.denominator) == (2, 4)


async def test_notes_requires_marker_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a marker set, only notes containing it count as «по шаблону»."""
    monkeypatch.setattr(settings, "companion_note_template_marker", "Итог")
    bx = FakeBitrix(
        notes=[
            {"ID": "1", "DESCRIPTION": "Итог: клиент думает"},  # has marker
            {"ID": "2", "DESCRIPTION": "перезвонить"},  # no marker
        ],
    )
    crit = await hygiene._notes(bx, 5, _PAST_START, _PAST_END)  # type: ignore[arg-type]
    assert (crit.numerator, crit.denominator) == (1, 2)


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
            {
                "ID": "1",
                "STAGE_ID": "C24:UC_OPEENZ",
                "LAST_ACTIVITY_TIME": _iso_days_ago(1),
            },
            {
                "ID": "2",
                "STAGE_ID": "C24:PREPAYMENT_INVOIC",
                "LAST_ACTIVITY_TIME": _iso_days_ago(99),
            },
        ],
        task_owners=[1, 2],  # both deals have an open task → tasks_set 100%
        notes=[{"ID": "1", "DESCRIPTION": "ok"}],  # 1/1 → notes 100%
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

    async def _fake_get(session: Any, uid: int, period: str | None) -> Any:
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
