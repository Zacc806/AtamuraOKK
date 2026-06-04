"""Map PORTAL_USER_ID -> Manager (and Department), via ``user.get``.

Degrades gracefully: without the ``user`` scope we still create Manager rows
keyed by ``bitrix_user_id`` (``enriched=False``) so calls can be attributed;
name/email/department are backfilled automatically once the scope is granted
(the next run re-fetches un-enriched managers).
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.manager import Manager


async def _fetch_profiles(
    bx: BitrixClient,
    user_ids: set[int],
) -> dict[int, dict[str, Any]]:
    """Fetch user profiles; returns {} (and logs once) if the scope is missing."""
    profiles: dict[int, dict[str, Any]] = {}
    for uid in user_ids:
        try:
            rows = await bx.call("user.get", {"ID": uid})
        except BitrixError as exc:
            if "INSUFFICIENT_SCOPE" in exc.code:
                logger.warning(
                    "user.get blocked (no 'user' scope); managers stay "
                    "un-enriched until it's added.",
                )
                return {}
            raise
        if rows:
            profiles[uid] = rows[0]
    return profiles


async def _ensure_department(
    session: AsyncSession,
    bitrix_dept_id: int,
) -> int | None:
    """Get-or-create a Department by its Bitrix id; return local id."""
    existing = await session.scalar(
        select(Department).where(Department.bitrix_id == bitrix_dept_id),
    )
    if existing:
        return existing.id
    dept = Department(bitrix_id=bitrix_dept_id, name=f"Department {bitrix_dept_id}")
    session.add(dept)
    await session.flush()
    return dept.id


def _first_department_id(profile: dict[str, Any]) -> int | None:
    """First UF_DEPARTMENT id from a user profile, if any."""
    depts = profile.get("UF_DEPARTMENT") or []
    if isinstance(depts, list) and depts:
        try:
            return int(depts[0])
        except (TypeError, ValueError):
            return None
    return None


async def ensure_managers(
    session: AsyncSession,
    user_ids: set[int],
    bx: BitrixClient,
) -> dict[int, int]:
    """Ensure a Manager row per ``bitrix_user_id``; return {user_id: manager_id}."""
    if not user_ids:
        return {}

    existing_rows = (
        await session.scalars(
            select(Manager).where(Manager.bitrix_user_id.in_(user_ids)),
        )
    ).all()
    by_uid = {m.bitrix_user_id: m for m in existing_rows}

    # Create rows for unseen users (un-enriched placeholders).
    for uid in user_ids - set(by_uid):
        manager = Manager(bitrix_user_id=uid)
        session.add(manager)
        by_uid[uid] = manager
    await session.flush()

    # Enrich anything not yet enriched (covers new rows + backfill after scope).
    to_enrich = {uid for uid, m in by_uid.items() if not m.enriched}
    profiles = await _fetch_profiles(bx, to_enrich) if to_enrich else {}
    for uid, profile in profiles.items():
        manager = by_uid[uid]
        manager.name = profile.get("NAME")
        manager.last_name = profile.get("LAST_NAME")
        manager.email = profile.get("EMAIL")
        manager.active = str(profile.get("ACTIVE", "Y")) in {"Y", "True", "1", "true"}
        dept_id = _first_department_id(profile)
        if dept_id is not None:
            manager.department_id = await _ensure_department(session, dept_id)
        manager.enriched = True

    await session.flush()
    return {uid: m.id for uid, m in by_uid.items()}
