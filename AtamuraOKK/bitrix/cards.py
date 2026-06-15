"""Human-facing Bitrix24 CRM card links.

AtamuraOKK is the single Bitrix gateway, so it owns the portal origin and the
URL shape; consumers (the sales-companion cabinet) just render the link. This
is navigation only — read-only, never a write to Bitrix.
"""

from __future__ import annotations

from AtamuraOKK.settings import settings

# voximplant.statistic CRM_ENTITY_TYPE -> CRM card path segment.
_ENTITY_SLUGS = {
    "LEAD": "lead",
    "DEAL": "deal",
    "CONTACT": "contact",
    "COMPANY": "company",
}


def crm_card_url(
    entity_type: str | None,
    entity_id: int | None,
    *,
    origin: str | None = None,
) -> str | None:
    """Bitrix24 CRM card URL for a CRM entity, or None when not derivable.

    Returns None if the portal origin is unconfigured, the entity is unset, or
    the type is not a known CRM entity — callers then simply omit the link.
    """
    base = settings.bitrix_portal_origin if origin is None else origin
    if not base or not entity_type or not entity_id:
        return None
    slug = _ENTITY_SLUGS.get(entity_type.upper())
    if slug is None:
        return None
    return f"{base}/crm/{slug}/details/{entity_id}/"
