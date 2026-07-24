"""«Отказы не по делу» queue: which contradicted verdicts reach Мой день.

«Автодозвон» is audited and stored like any other «недозвон» reason, but withheld
from the queue: that dial was the robot's, so a client who answered is not a call
the manager failed to make. The queue only shows leads a human never reached.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.settings import settings
from AtamuraOKK.web.api.v1.day import _audit_failed_items

pytestmark = pytest.mark.anyio

_UID = 90210
_AVTODOZVON = "1956"
_CHRONIC = "1192"
_STOPPED = "3774"


async def _manager(dbsession: AsyncSession) -> Manager:
    mgr = Manager(bitrix_user_id=_UID, name="ТМ тест")
    dbsession.add(mgr)
    await dbsession.flush()
    return mgr


def _verdict(
    mgr_id: int,
    deal_id: int,
    reason_id: str | None,
    reason: str,
    verdict: str = "contradicted",
) -> AuditVerdict:
    return AuditVerdict(
        bitrix_deal_id=deal_id,
        deal_title=reason,
        manager_id=mgr_id,
        close_reason=reason,
        reason_id=reason_id,
        verdict=verdict,
    )


async def test_avtodozvon_is_withheld_from_the_queue(dbsession: AsyncSession) -> None:
    """The robot's dial is not a call the manager failed to make."""
    mgr = await _manager(dbsession)
    dbsession.add(_verdict(mgr.id, 8001, _AVTODOZVON, "Автодозвон"))
    dbsession.add(_verdict(mgr.id, 8002, _CHRONIC, "Хронический недозвон"))
    await dbsession.flush()

    items = await _audit_failed_items(dbsession, _UID, 20)

    assert [i.deal_id for i in items] == [8002]


async def test_avtodozvon_verdict_is_still_stored(dbsession: AsyncSession) -> None:
    """Hidden from the queue is not the same as un-audited — the row survives."""
    mgr = await _manager(dbsession)
    dbsession.add(_verdict(mgr.id, 8003, _AVTODOZVON, "Автодозвон"))
    await dbsession.flush()

    row = await dbsession.scalar(
        select(AuditVerdict).where(AuditVerdict.bitrix_deal_id == 8003),
    )

    assert row is not None
    assert row.verdict == "contradicted"


async def test_other_nedozvon_reasons_still_show(dbsession: AsyncSession) -> None:
    """Only «Автодозвон» is withheld — the rest of the family is untouched."""
    mgr = await _manager(dbsession)
    dbsession.add(_verdict(mgr.id, 8004, _CHRONIC, "Хронический недозвон"))
    dbsession.add(_verdict(mgr.id, 8005, _STOPPED, "Перестал выходить на связь"))
    await dbsession.flush()

    items = await _audit_failed_items(dbsession, _UID, 20)

    assert {i.deal_id for i in items} == {8004, 8005}


async def test_unspecified_reason_still_shows(dbsession: AsyncSession) -> None:
    """A NULL reason_id is «Не указана» — never a hidden enum, so it stays visible."""
    mgr = await _manager(dbsession)
    dbsession.add(_verdict(mgr.id, 8006, None, "Не указана"))
    await dbsession.flush()

    items = await _audit_failed_items(dbsession, _UID, 20)

    assert [i.deal_id for i in items] == [8006]


async def test_supported_verdicts_never_reach_the_queue(
    dbsession: AsyncSession,
) -> None:
    """The hidden-reason filter must not widen the queue past «contradicted»."""
    mgr = await _manager(dbsession)
    dbsession.add(
        _verdict(mgr.id, 8007, _CHRONIC, "Хронический недозвон", verdict="supported"),
    )
    await dbsession.flush()

    items = await _audit_failed_items(dbsession, _UID, 20)

    assert items == []


async def test_empty_hidden_list_shows_everything(
    dbsession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the setting is the escape hatch — автодозвон comes back."""
    monkeypatch.setattr(settings, "companion_day_audit_hidden_reason_ids", [])
    mgr = await _manager(dbsession)
    dbsession.add(_verdict(mgr.id, 8008, _AVTODOZVON, "Автодозвон"))
    await dbsession.flush()

    items = await _audit_failed_items(dbsession, _UID, 20)

    assert [i.deal_id for i in items] == [8008]
