"""Client qualification based on whether a deal reached 'Лид квалифицирован'.

The operator's rule: a client is *qualified* once a manager moves their card into
the Kanban column **Лид квалифицирован**. That column is a **deal stage** (present
in more than one pipeline), and calls link to **contacts** — so we resolve
Contact → deals → deal stage history and check whether any deal **ever entered**
a qualified stage (faithful to "the card was placed there", and correctly
excludes deals dropped to Отказ before qualifying).

Swappable via the :class:`QualificationChecker` protocol.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings

# Bitrix entityTypeId for deals (crm.stagehistory.list).
_DEAL_ENTITY_TYPE_ID = 2


@runtime_checkable
class QualificationChecker(Protocol):
    """Decides whether each client key is qualified."""

    async def qualified(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, bool | None]:
        """Map each client key to True/False/None (unknown)."""
        ...


class NullQualificationChecker:
    """Always returns unknown — the safe default before a rule is configured."""

    async def qualified(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, bool | None]:
        """Return None (unknown) for every client."""
        return dict.fromkeys(client_keys, None)


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
    """Qualified iff a client's deal ever entered the qualified stage."""

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
    ) -> dict[str, bool | None]:
        """Resolve each client to qualified/not/unknown via deal stage history."""
        stage_ids = await self._ensure_stage_ids(bx)
        if not stage_ids:
            logger.warning("No qualified stages found; qualification is unknown.")
            return dict.fromkeys(client_keys, None)

        result: dict[str, bool | None] = {}
        for key in client_keys:
            entity_type, _, entity_id = key.partition(":")
            try:
                result[key] = await self._check(entity_type, entity_id, stage_ids, bx)
            except BitrixError as exc:
                logger.warning("Qualification failed for {k}: {e}", k=key, e=exc)
                result[key] = None
        return result

    async def _check(
        self,
        entity_type: str,
        entity_id: str,
        stage_ids: set[str],
        bx: BitrixClient,
    ) -> bool | None:
        if entity_type == "CONTACT":
            deal_ids = await self._deal_ids(bx, {"CONTACT_ID": entity_id})
        elif entity_type == "COMPANY":
            deal_ids = await self._deal_ids(bx, {"COMPANY_ID": entity_id})
        elif entity_type == "DEAL":
            deal_ids = [entity_id]
        else:
            return None  # LEAD / PHONE-only: not resolvable to deal stages
        if not deal_ids:
            return False
        return await self._any_deal_qualified(bx, deal_ids, stage_ids)

    async def _deal_ids(
        self,
        bx: BitrixClient,
        filter_: dict[str, Any],
    ) -> list[str]:
        deals = await bx.call(
            "crm.deal.list",
            {"filter": filter_, "select": ["ID"]},
        )
        return [str(d["ID"]) for d in (deals or [])]

    async def _any_deal_qualified(
        self,
        bx: BitrixClient,
        deal_ids: list[str],
        stage_ids: set[str],
    ) -> bool:
        """True if any deal has a stage-history entry in a qualified stage."""
        env = await bx.call(
            "crm.stagehistory.list",
            {
                "entityTypeId": _DEAL_ENTITY_TYPE_ID,
                "filter": {"OWNER_ID": deal_ids, "STAGE_ID": sorted(stage_ids)},
                "select": ["STAGE_ID"],
            },
        )
        items = env.get("items", []) if isinstance(env, dict) else (env or [])
        return len(items) > 0


def default_checker() -> QualificationChecker:
    """The checker the service uses (stage-history rule)."""
    return ContactDealStageQualificationChecker()
