"""Client category (A/B/C/X) resolution from the deal field «Квалификация клиента».

The manager tags each client's *deal* with a lead category per the qualification
регламент. Calls link to a CRM entity (``CONTACT:123`` / ``DEAL:45`` / ...); for a
contact/company we resolve its deals and read the tag off the most recent deal that
carries one — mirroring ``qualification.py``'s Contact→deals resolution. The field
is an enumeration whose raw value is an *enum ID* (not the letter), so a configured
enum-id → letter map turns ``1008`` into ``"B"``. Anything not resolvable to a deal
(lead / phone-only) → None.

Swappable via the :class:`CategoryChecker` protocol.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.settings import settings


@runtime_checkable
class CategoryChecker(Protocol):
    """Maps each client key to its lead category letter (or None)."""

    async def categorize(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, str | None]:
        """Map each client key to a category letter (A/B/C/X) or None."""
        ...


class NullCategoryChecker:
    """Always returns None — the safe default before a field is configured."""

    async def categorize(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, str | None]:
        """Return None for every client."""
        return dict.fromkeys(client_keys, None)


class BitrixDealCategoryChecker:
    """Reads the category from the deal enumeration field, latest deal wins."""

    def __init__(
        self,
        *,
        field: str | None = None,
        value_map: dict[str, str] | None = None,
    ) -> None:
        self._field = field or settings.client_category_field
        self._value_map = value_map or settings.client_category_value_map

    async def categorize(
        self,
        client_keys: set[str],
        bx: BitrixClient,
    ) -> dict[str, str | None]:
        """Resolve each client key to a category letter (or None)."""
        result: dict[str, str | None] = {}
        for key in client_keys:
            entity_type, _, entity_id = key.partition(":")
            try:
                result[key] = await self._category(entity_type, entity_id, bx)
            except BitrixError as exc:
                logger.warning("Category lookup failed for {k}: {e}", k=key, e=exc)
                result[key] = None
        return result

    async def _category(
        self,
        entity_type: str,
        entity_id: str,
        bx: BitrixClient,
    ) -> str | None:
        if not self._field:
            return None
        if entity_type == "CONTACT":
            filter_: dict[str, Any] = {"CONTACT_ID": entity_id}
        elif entity_type == "COMPANY":
            filter_ = {"COMPANY_ID": entity_id}
        elif entity_type == "DEAL":
            filter_ = {"ID": entity_id}
        else:
            # LEAD / PHONE-only: no deal to read the tag off.
            return None
        return await self._latest_category(bx, filter_)

    async def _latest_category(
        self,
        bx: BitrixClient,
        filter_: dict[str, Any],
    ) -> str | None:
        """Mapped category of the most recent deal that carries one (or None).

        Deals are paged newest-first; the first deal whose field maps to a letter
        is the latest tag, so we can stop there. Deals without the tag are skipped.
        """
        async for deal in bx.list(
            "crm.deal.list",
            {
                "filter": filter_,
                "select": ["ID", self._field],
                "order": {"ID": "DESC"},
            },
        ):
            raw = str(deal.get(self._field) or "").strip()
            letter = self._value_map.get(raw)
            if letter is not None:
                return letter
        return None


def default_category_checker() -> CategoryChecker:
    """The checker the service uses: deal-field-based if configured, else null."""
    if settings.client_category_field:
        return BitrixDealCategoryChecker()
    return NullCategoryChecker()


async def discover_category_fields() -> None:
    """Print Contact/Lead/Deal enumeration UF fields + their enum-id → value pairs.

    Read-only helper to fill ``client_category_field`` / ``..._value_map`` from the
    live portal: list/enumeration fields store enum IDs (not the letters), so the
    operator needs to see each field's ``items`` to build the value map. The A/B/C/X
    tag «Квалификация клиента» lives on the *deal*.
    """
    async with BitrixClient() as bx:
        for entity, method in (
            ("CONTACT", "crm.contact.fields"),
            ("LEAD", "crm.lead.fields"),
            ("DEAL", "crm.deal.fields"),
        ):
            logger.info("=== {entity} ({method}) ===", entity=entity, method=method)
            fields = await bx.call(method)
            if not isinstance(fields, dict):
                logger.info("  (no fields returned)")
                continue
            for fid, meta in sorted(fields.items()):
                if not (fid.startswith("UF_CRM_") and isinstance(meta, dict)):
                    continue
                if meta.get("type") != "enumeration":
                    continue
                label = str(meta.get("formLabel") or meta.get("title") or "")
                hinted = any(h in label.casefold() for h in ("категори", "квалификац"))
                mark = "  <-- candidate" if hinted else ""
                logger.info("{fid}  «{label}»{mark}", fid=fid, label=label, mark=mark)
                for item in meta.get("items") or []:
                    logger.info(
                        "    {id} = {value}",
                        id=item.get("ID"),
                        value=item.get("VALUE"),
                    )
