"""Client qualification based on whether a deal reached 'Лид квалифицирован'.

The operator's rule: a client is *qualified* once a manager moves their card into
the Kanban column **Лид квалифицирован**. That column is a **deal stage** (present
in more than one pipeline), and calls link to **contacts** — so we resolve
Contact → deals → deal stage history. The scope rule needs not just *whether* a
deal entered a qualified stage but **when** (the earliest entry's CREATED_TIME):
calls before that moment are the sales conversation, calls after it are
logistics. Stage history correctly excludes deals dropped to Отказ before
qualifying.

Swappable via the :class:`QualificationChecker` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings

# Bitrix entityTypeId for deals (crm.stagehistory.list).
_DEAL_ENTITY_TYPE_ID = 2


@dataclass(frozen=True)
class Qualification:
    """Whether (and when) a client entered the qualified column.

    ``qualified`` is True/False/None (unknown — e.g. a phone-only client that
    cannot be resolved to deals). ``at`` is the earliest qualified-stage entry;
    None whenever ``qualified`` is not True.
    """

    qualified: bool | None
    at: datetime | None = None


UNKNOWN_QUALIFICATION = Qualification(qualified=None)


@runtime_checkable
class QualificationChecker(Protocol):
    """Decides whether/when each client key qualified."""

    async def qualified(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, Qualification]:
        """Map each client key to its :class:`Qualification`."""
        ...


class NullQualificationChecker:
    """Always returns unknown — the safe default before a rule is configured."""

    async def qualified(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, Qualification]:
        """Return unknown for every client."""
        return dict.fromkeys(client_keys, UNKNOWN_QUALIFICATION)


async def discover_qualified_stage_ids(bx: BitrixClient) -> set[str]:
    """All deal-pipeline STATUS_IDs whose column name is the qualified name.

    Discovered by name so new pipelines that add a 'Лид квалифицирован' column are
    picked up automatically. Overridable via ``qualified_deal_stage_ids``.
    """
    if settings.qualified_deal_stage_ids:
        return set(settings.qualified_deal_stage_ids)

    target = settings.qualified_stage_name.strip().casefold()
    stage_ids: set[str] = set()
    async for status in bx.list("crm.status.list"):
        entity = str(status.get("ENTITY_ID") or "")
        name = str(status.get("NAME") or "").strip().casefold()
        if entity.startswith("DEAL_STAGE") and name == target:
            stage_ids.add(str(status["STATUS_ID"]))
    return stage_ids


class ContactDealStageQualificationChecker:
    """Qualified iff a client's deal ever entered the qualified stage.

    Resolves the *earliest* entry time so the scope rule can split a client's
    calls into before/after qualification.
    """

    def __init__(self, qualified_stage_ids: set[str] | None = None) -> None:
        self._stage_ids = qualified_stage_ids

    async def _ensure_stage_ids(self, bx: BitrixClient) -> set[str]:
        if self._stage_ids is None:
            self._stage_ids = await discover_qualified_stage_ids(bx)
            logger.info(
                "Qualified deal stages ({name}): {ids}",
                name=settings.qualified_stage_name,
                ids=sorted(self._stage_ids) or "NONE FOUND",
            )
        return self._stage_ids

    async def qualified(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, Qualification]:
        """Resolve each client to qualified/not/unknown (+ moment) via stage history."""
        stage_ids = await self._ensure_stage_ids(bx)
        if not stage_ids:
            logger.warning("No qualified stages found; qualification is unknown.")
            return dict.fromkeys(client_keys, UNKNOWN_QUALIFICATION)

        result: dict[str, Qualification] = {}
        for key in client_keys:
            entity_type, _, entity_id = key.partition(":")
            try:
                result[key] = await self._check(entity_type, entity_id, stage_ids, bx)
            except BitrixError as exc:
                logger.warning("Qualification failed for {k}: {e}", k=key, e=exc)
                result[key] = UNKNOWN_QUALIFICATION
        return result

    async def _check(
        self,
        entity_type: str,
        entity_id: str,
        stage_ids: set[str],
        bx: BitrixClient,
    ) -> Qualification:
        if entity_type == "CONTACT":
            deal_ids = await self._deal_ids(bx, {"CONTACT_ID": entity_id})
        elif entity_type == "COMPANY":
            deal_ids = await self._deal_ids(bx, {"COMPANY_ID": entity_id})
        elif entity_type == "DEAL":
            deal_ids = [entity_id]
        else:
            # LEAD / PHONE-only: not resolvable to deal stages.
            return UNKNOWN_QUALIFICATION
        if not deal_ids:
            return Qualification(qualified=False)
        at = await self._earliest_qualified_at(bx, deal_ids, stage_ids)
        return Qualification(qualified=at is not None, at=at)

    async def _deal_ids(
        self,
        bx: BitrixClient,
        filter_: dict[str, Any],
    ) -> list[str]:
        # Page through every deal: a client with >50 deals would otherwise have
        # later deals dropped, and the qualifying deal could be among them.
        return [
            str(d["ID"])
            async for d in bx.list(
                "crm.deal.list",
                {"filter": filter_, "select": ["ID"]},
            )
        ]

    async def _earliest_qualified_at(
        self,
        bx: BitrixClient,
        deal_ids: list[str],
        stage_ids: set[str],
    ) -> datetime | None:
        """Earliest qualified-stage entry across the deals, or None if never.

        Pages through *every* history page (a qualifying entry on page 2+ —
        many deals / stage transitions — must not be missed, and the earliest
        one can sit anywhere). The filter restricts rows to the qualified
        stages, so per-client volume is tiny.
        """
        earliest: datetime | None = None
        cursor: int | None = 0
        while cursor is not None:
            env = await bx.call_raw(
                "crm.stagehistory.list",
                {
                    "entityTypeId": _DEAL_ENTITY_TYPE_ID,
                    "filter": {"OWNER_ID": deal_ids, "STAGE_ID": sorted(stage_ids)},
                    "select": ["STAGE_ID", "CREATED_TIME"],
                    "start": cursor,
                },
            )
            result = env.get("result")
            items = (
                result.get("items", []) if isinstance(result, dict) else (result or [])
            )
            for item in items:
                raw = item.get("CREATED_TIME")
                if not raw:
                    continue
                try:
                    at = datetime.fromisoformat(str(raw))
                except ValueError:
                    continue
                if earliest is None or at < earliest:
                    earliest = at
            nxt = env.get("next")
            cursor = int(nxt) if nxt is not None else None
        return earliest


def default_checker() -> QualificationChecker:
    """The checker the service uses (stage-history rule)."""
    return ContactDealStageQualificationChecker()
