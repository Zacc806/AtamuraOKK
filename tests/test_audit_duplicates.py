"""«Дубль…» close reasons: settled against the CRM, not the call.

The duplicate route never touches the LLM and never needs a transcript, so these
tests stub no judge — they assert the opposite: that the judge is never reached, that
a deal with no call still gets a verdict, and that only a *missing* duplicate
contradicts the close (a wrong subtype or a dupe in another funnel does not).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.audit import duplicates, service
from AtamuraOKK.db.models.audit_verdict import AuditVerdict
from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.db.models.transcript import Transcript
from AtamuraOKK.settings import settings

pytestmark = pytest.mark.anyio

_FIELD = settings.companion_closed_reason_field
_TM = settings.companion_tm_category_id
_SAME = settings.audit_dup_same_project_reason_id  # «Дубль по этому проекту»
_OTHER = settings.audit_dup_other_project_reason_id  # «Дубль по другим проектам»
_CLOSE_A = "2026-07-01T10:00:00+05:00"
_CLOSE_B = "2026-07-02T11:00:00+05:00"
_PHONE = "+77015550101"


class _FakeBitrix:
    """Serves the closed-lost scan plus the three dedupe reads the check makes."""

    def __init__(
        self,
        closed: list[dict[str, Any]],
        *,
        phones: dict[int, list[str]] | None = None,
        dup_contacts: dict[str, list[int]] | None = None,
        dup_leads: dict[str, list[int]] | None = None,
        deals_of: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.closed = closed
        self.phones = phones or {}
        self.dup_contacts = dup_contacts or {}
        self.dup_leads = dup_leads or {}
        self.deals_of = deals_of or {}

    async def list(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        filter_ = (params or {}).get("filter") or {}
        if method == "crm.contact.list":
            for cid in filter_["ID"]:
                yield {
                    "ID": str(cid),
                    "PHONE": [{"VALUE": p} for p in self.phones.get(int(cid), [])],
                }
            return
        assert method == "crm.deal.list"
        if "CONTACT_ID" in filter_:  # the duplicate lookup
            for cid in filter_["CONTACT_ID"]:
                for d in self.deals_of.get(int(cid), []):
                    yield d
            return
        for d in self.closed:  # the closed-lost scan
            yield d

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method == "crm.deal.fields":
            return {
                _FIELD: {
                    "items": [
                        {"ID": _SAME, "VALUE": "Дубль по этому проекту"},
                        {"ID": _OTHER, "VALUE": "Дубль по другим проектам"},
                        {"ID": "101", "VALUE": "Локация не подходит"},
                    ],
                },
            }
        if method == "user.get":  # manager enrichment (ensure_managers)
            return [{"ID": "555", "NAME": "Асель", "UF_DEPARTMENT": [250]}]
        assert method == "crm.duplicate.findbycomm"
        params = params or {}
        table = (
            self.dup_contacts if params["entity_type"] == "CONTACT" else self.dup_leads
        )
        found = sorted({i for v in params["values"] for i in table.get(v, [])})
        if not found:
            return []  # Bitrix returns a bare [] when nothing matches
        return {params["entity_type"]: found}


def _closed(
    deal_id: str, *, title: str, contact: str, reason: str, closedate: str = _CLOSE_A
) -> dict[str, Any]:
    return {
        "ID": deal_id,
        "TITLE": title,
        "ASSIGNED_BY_ID": "555",
        "CONTACT_ID": contact,
        "CLOSEDATE": closedate,
        _FIELD: reason,
    }


def _other(deal_id: str, *, title: str, category: int = _TM) -> dict[str, Any]:
    return {"ID": deal_id, "TITLE": title, "CATEGORY_ID": str(category)}


def _no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any call to the judge is a test failure — the дубль route must not reach it."""

    async def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the LLM judge must not be called for a «Дубль…» reason")

    monkeypatch.setattr(service, "build_judge_client", object)
    monkeypatch.setattr(service, "judge_one", boom)


async def _verdict(session: AsyncSession) -> AuditVerdict:
    return (await session.execute(select(AuditVerdict))).scalar_one()


async def _seed_transcript(session: AsyncSession, contact_id: int) -> None:
    """A transcribed call for CONTACT:{id}, so the deal reaches the judge route."""
    call = Call(
        bitrix_call_id=f"dup-{contact_id}",
        client_key=f"CONTACT:{contact_id}",
        started_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC),
        status=CallStatus.SCORED,
    )
    session.add(call)
    await session.flush()
    session.add(Transcript(call_id=call.id, full_text="[AGENT]\nздравствуйте"))
    await session.flush()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Лиды FB | Aqsai Resort New", "aqsai"),
        ("Лиды FB | Keruen - ", "keruen"),
        ("Крыша, новая заявка от колл центра ЖК Keruen", "keruen"),
        ("Сайт atamura.group: start-amaia · визит 17:00", "amaia"),
        ("Лиды FB | Атмосфера (восстановлено)", "atmosfera"),
        ("+7 708 021 2381 - Входящий звонок", None),
        ("Гость - Instagram лиды", None),
        (None, None),
    ],
)
def test_project_of(title: str | None, expected: str | None) -> None:
    """The project is read off the title — Latin or Cyrillic, or not at all."""
    assert duplicates.project_of(title) == expected


async def test_no_duplicate_contradicts(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed as «дубль» but the number is on nothing else → contradicted, no LLM."""
    dbsession.add(Manager(bitrix_user_id=555, name="Асель", enriched=True))
    _no_judge(monkeypatch)
    deal = _closed("7001", title="Лиды FB | Keruen", contact="5001", reason=_SAME)
    bx = _FakeBitrix(
        [deal],
        phones={5001: [_PHONE]},
        dup_contacts={_PHONE: [5001]},
        deals_of={5001: [deal]},  # only the audited deal itself
    )

    stats = await service.run_audit(dbsession, bx)

    assert stats.checked == 1
    assert stats.judged == 0
    row = await _verdict(dbsession)
    assert row.verdict == "contradicted"
    assert row.model is None  # deterministic — no LLM was used
    assert row.details["check"] == duplicates.CHECK_ID
    assert row.details["duplicate_deal_ids"] == []
    assert "дубля нет" in (row.justification or "")
    assert stats.cursor == _CLOSE_A


async def test_dup_audited_without_any_transcript(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A «дубль» deal we hold no call for is still checked (the judge route skips it).

    This is the coverage the change buys: a lead closed on sight never got a call,
    which is exactly the case worth checking.
    """
    _no_judge(monkeypatch)
    deal = _closed("7002", title="Лиды FB | Amaia", contact="5002", reason=_SAME)
    bx = _FakeBitrix(
        [deal],
        phones={5002: [_PHONE]},
        dup_contacts={_PHONE: [5002]},
        deals_of={5002: [deal, _other("7100", title="Лиды FB | Amaia")]},
    )

    stats = await service.run_audit(dbsession, bx)

    assert stats.no_transcript == 0  # not skipped for lack of a call
    assert stats.checked == 1
    row = await _verdict(dbsession)
    assert row.verdict == "supported"
    assert row.call_ids == []


async def test_same_project_subtype_ok(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Дубль по этому проекту» with a twin on the same ЖК → supported, subtype ok."""
    _no_judge(monkeypatch)
    deal = _closed("7003", title="Крыша ЖК Keruen", contact="5003", reason=_SAME)
    bx = _FakeBitrix(
        [deal],
        phones={5003: [_PHONE]},
        dup_contacts={_PHONE: [5003]},
        deals_of={5003: [deal, _other("7101", title="Лиды FB | Keruen - ")]},
    )

    await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "supported"
    assert row.details["subtype_ok"] is True
    assert row.details["duplicate_projects"] == ["keruen"]
    assert row.details["tm_duplicate_deal_ids"] == [7101]


async def test_wrong_subtype_is_recorded_but_not_contradicted(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real dupe on ANOTHER ЖК, closed as «по этому проекту» — hygiene, not a nudge.

    The lead was rightly closed (the duplicate exists), so the verdict stays
    ``supported`` and «Отказы не по делу» leaves it alone; only ``details`` records
    that the manager picked the wrong дубль subtype.
    """
    _no_judge(monkeypatch)
    deal = _closed(
        "7004", title="Лиды FB | Aqsai Resort New", contact="5004", reason=_SAME
    )
    bx = _FakeBitrix(
        [deal],
        phones={5004: [_PHONE]},
        dup_contacts={_PHONE: [5004]},
        deals_of={5004: [deal, _other("7102", title="Лиды FB | Amaia")]},
    )

    await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "supported"  # NOT contradicted — the дубль is real
    assert row.details["subtype_ok"] is False
    assert row.details["expected_reason_kind"] == "other"
    assert "следовало выбрать «Дубль по другим проектам»" in (row.justification or "")


async def test_unknown_project_leaves_subtype_unresolved(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ЖК in the title → the subtype is left unresolved rather than guessed."""
    _no_judge(monkeypatch)
    deal = _closed(
        "7005", title="+7 708 021 2381 - Входящий звонок", contact="5005", reason=_OTHER
    )
    bx = _FakeBitrix(
        [deal],
        phones={5005: [_PHONE]},
        dup_contacts={_PHONE: [5005]},
        deals_of={5005: [deal, _other("7103", title="Гость - Instagram лиды")]},
    )

    await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "supported"
    assert row.details["subtype_ok"] is None


async def test_duplicate_outside_tm_funnel_is_supported(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The twin lives in the sales funnel: a real duplicate, so no accusation."""
    _no_judge(monkeypatch)
    deal = _closed("7006", title="Лиды FB | Keruen", contact="5006", reason=_SAME)
    bx = _FakeBitrix(
        [deal],
        phones={5006: [_PHONE]},
        dup_contacts={_PHONE: [5006]},
        deals_of={
            5006: [deal, _other("7104", title="ЖК Keruen", category=2)],
        },
    )

    await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "supported"
    assert row.details["tm_duplicate_deal_ids"] == []
    assert "вне воронки ТМ" in (row.justification or "")


async def test_lead_only_duplicate_is_not_accused(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No other deal, but a LEAD carries the number → a real dupe record, not a lie."""
    _no_judge(monkeypatch)
    deal = _closed("7007", title="Лиды FB | Keruen", contact="5007", reason=_SAME)
    bx = _FakeBitrix(
        [deal],
        phones={5007: [_PHONE]},
        dup_contacts={_PHONE: [5007]},
        dup_leads={_PHONE: [900]},
        deals_of={5007: [deal]},
    )

    await service.run_audit(dbsession, bx)

    row = await _verdict(dbsession)
    assert row.verdict == "supported"
    assert row.details["lead_ids"] == [900]


async def test_deal_without_contact_is_not_determinable(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No contact → nothing to look up; never accuse on missing data."""
    _no_judge(monkeypatch)
    deal = _closed("7008", title="Лиды FB | Keruen", contact="0", reason=_SAME)

    await service.run_audit(dbsession, _FakeBitrix([deal]))

    row = await _verdict(dbsession)
    assert row.verdict == "not_determinable"


async def test_judge_off_still_runs_the_dup_check(
    dbsession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the LLM off, «дубль» deals still audit; other deals are left pending.

    The cursor must not advance past the unjudged deal, so turning the judge back on
    picks it up — the whole point of not marking it done.
    """
    _no_judge(monkeypatch)
    monkeypatch.setattr(settings, "audit_llm_judge_enabled", False)
    await _seed_transcript(dbsession, 5010)  # the non-дубль deal HAS a call to judge
    dup = _closed(
        "7009",
        title="Лиды FB | Keruen",
        contact="5009",
        reason=_SAME,
        closedate=_CLOSE_A,
    )
    other = _closed(
        "7010",
        title="Лиды FB | Keruen",
        contact="5010",
        reason="101",
        closedate=_CLOSE_B,
    )
    bx = _FakeBitrix(
        [dup, other],
        phones={5009: [_PHONE]},
        dup_contacts={_PHONE: [5009]},
        deals_of={5009: [dup, _other("7105", title="Лиды FB | Keruen")]},
    )

    stats = await service.run_audit(dbsession, bx)

    assert stats.checked == 1
    assert stats.judged == 0
    assert stats.judge_off == 1
    assert stats.cursor == _CLOSE_A  # advanced past the дубль, stopped at the unjudged
    row = await _verdict(dbsession)
    assert row.bitrix_deal_id == 7009
