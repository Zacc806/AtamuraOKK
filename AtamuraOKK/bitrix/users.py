"""Sync Bitrix users/departments into managers/departments.

Needs the ``user`` webhook scope. While that scope is pending, falls back to the
operator-provided ``data/tm_managers.json`` so calls can still be linked by
``PORTAL_USER_ID`` to a manager name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from AtamuraOKK.bitrix import BitrixClient, BitrixError
from AtamuraOKK.db.dao.manager_dao import DepartmentDAO, ManagerDAO

_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "data" / "tm_managers.json"


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _sync_departments(bx: BitrixClient, departments: DepartmentDAO) -> None:
    async for dept in bx.list("department.get"):
        dept_id = _to_int(dept.get("ID"))
        if dept_id is None:
            continue
        await departments.upsert(
            bitrix_dept_id=dept_id,
            name=str(dept.get("NAME", "")),
            head_bitrix_user_id=_to_int(dept.get("UF_HEAD")),
        )


async def _load_fallback(managers: ManagerDAO, path: Path) -> int:
    if not path.exists():
        logger.warning("user scope missing and no fallback at {p}", p=path)
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    for entry in data.get("managers", []):
        uid = _to_int(entry.get("id"))
        if uid is None:
            continue
        await managers.upsert(bitrix_user_id=uid, name=str(entry.get("name", "")))
        count += 1
    logger.info("Loaded {n} managers from fallback {p}", n=count, p=path)
    return count


async def sync_users(
    bx: BitrixClient,
    managers: ManagerDAO,
    departments: DepartmentDAO,
    *,
    fallback_path: Path = _FALLBACK_PATH,
) -> int:
    """Upsert managers (and departments) from Bitrix, or from the fallback file.

    :returns: number of managers upserted.
    """
    try:
        await _sync_departments(bx, departments)
        dept_map = await departments.id_map()
        count = 0
        async for user in bx.list("user.get"):
            uid = _to_int(user.get("ID"))
            if uid is None:
                continue
            name = " ".join(
                part for part in (user.get("NAME"), user.get("LAST_NAME")) if part
            ).strip()
            dept_id: int | None = None
            for raw_dept in user.get("UF_DEPARTMENT") or []:
                bitrix_dept = _to_int(raw_dept)
                if bitrix_dept is not None and bitrix_dept in dept_map:
                    dept_id = dept_map[bitrix_dept]
                    break
            await managers.upsert(
                bitrix_user_id=uid,
                name=name,
                email=user.get("EMAIL"),
                department_id=dept_id,
            )
            count += 1
    except BitrixError as exc:
        logger.warning("user.get failed ({e}); using fallback manager list", e=exc)
        return await _load_fallback(managers, fallback_path)
    else:
        logger.info("Synced {n} managers from Bitrix", n=count)
        return count
