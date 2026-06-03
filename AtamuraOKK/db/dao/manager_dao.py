"""Data access for managers and departments."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.manager import Manager


class ManagerDAO:
    """Read/write access to the ``managers`` table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)) -> None:
        self.session = session

    async def id_map(self) -> dict[int, int]:
        """Map ``bitrix_user_id`` -> ``managers.id`` for fast call linking."""
        result = await self.session.execute(
            select(Manager.bitrix_user_id, Manager.id),
        )
        return {row[0]: row[1] for row in result.all()}

    async def upsert(
        self,
        *,
        bitrix_user_id: int,
        name: str = "",
        email: str | None = None,
        department_id: int | None = None,
    ) -> None:
        """Insert or update a manager keyed on ``bitrix_user_id``."""
        stmt = pg_insert(Manager).values(
            bitrix_user_id=bitrix_user_id,
            name=name,
            email=email,
            department_id=department_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["bitrix_user_id"],
            set_={
                "name": stmt.excluded.name,
                "email": stmt.excluded.email,
                "department_id": stmt.excluded.department_id,
            },
        )
        await self.session.execute(stmt)


class DepartmentDAO:
    """Read/write access to the ``departments`` table."""

    def __init__(self, session: AsyncSession = Depends(get_db_session)) -> None:
        self.session = session

    async def id_map(self) -> dict[int, int]:
        """Map ``bitrix_dept_id`` -> ``departments.id``."""
        result = await self.session.execute(
            select(Department.bitrix_dept_id, Department.id),
        )
        return {row[0]: row[1] for row in result.all()}

    async def upsert(
        self,
        *,
        bitrix_dept_id: int,
        name: str,
        head_bitrix_user_id: int | None = None,
    ) -> None:
        """Insert or update a department keyed on ``bitrix_dept_id``."""
        stmt = pg_insert(Department).values(
            bitrix_dept_id=bitrix_dept_id,
            name=name,
            head_bitrix_user_id=head_bitrix_user_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["bitrix_dept_id"],
            set_={
                "name": stmt.excluded.name,
                "head_bitrix_user_id": stmt.excluded.head_bitrix_user_id,
            },
        )
        await self.session.execute(stmt)
