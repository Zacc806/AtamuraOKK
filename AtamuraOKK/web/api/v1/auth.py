"""Auth for the companion read API — two layers.

1. **Service layer** (``require_companion_token``): the shared bearer the
   sales-companion BFF (nginx) injects server-side. Proves the request came
   through the companion seam at all. Fail closed: if ``companion_api_token``
   is unset the API returns 503 rather than serving call-quality data
   unauthenticated. Compared in constant time.
2. **User layer** (``get_companion_identity``): the personal access key the
   browser sends as ``X-Companion-User-Key``. Two sources, checked in order:
   the **static head key** (``companion_head_key`` setting — the РОП's fixed
   code, compared in constant time, no DB row needed), then a
   ``companion_users`` row (SHA-256 lookup) carrying the role — ``manager`` is
   scoped to their own Bitrix user id, ``head`` sees everything. A head row may
   additionally carry a ``department_id`` (Bitrix department id) — an office
   РОП scoped to that one department's managers and team rollup; the static key
   stays the global head. Manager keys are issued by a head from the cabinet
   (``POST /users``) or with ``python -m AtamuraOKK.companion_users``;
   department-scoped head keys are minted by the *global* head (cabinet or
   CLI) — the global head itself stays env/CLI-only.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from AtamuraOKK.db.dependencies import get_db_session
from AtamuraOKK.db.models.companion_user import CompanionUser
from AtamuraOKK.db.models.department import Department
from AtamuraOKK.db.models.enums import CompanionRole
from AtamuraOKK.db.models.manager import Manager
from AtamuraOKK.settings import settings


def hash_key(key: str) -> str:
    """SHA-256 hex of a personal access key (what ``companion_users`` stores)."""
    return hashlib.sha256(key.encode()).hexdigest()


async def require_companion_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Reject any request lacking a valid ``Authorization: Bearer <token>``."""
    expected = settings.companion_api_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Companion API token is not configured.",
        )

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


#: ``CompanionIdentity.user_id`` of the static-key РОП session (no DB row).
STATIC_HEAD_USER_ID = 0


@dataclass(frozen=True)
class CompanionIdentity:
    """Who is behind the cabinet session, resolved from the personal key.

    ``department_id`` is the **Bitrix** department id a HEAD is scoped to
    (an office РОП); ``None`` means the global head. Managers are scoped by
    ``bitrix_user_id`` and never carry a department here.
    """

    user_id: int
    role: CompanionRole
    bitrix_user_id: int | None
    name: str | None
    department_id: int | None = None

    @property
    def is_global_head(self) -> bool:
        """The unscoped head of sales — sees every department."""
        return self.role is CompanionRole.HEAD and self.department_id is None


async def get_companion_identity(
    x_companion_user_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> CompanionIdentity:
    """Resolve ``X-Companion-User-Key`` to an active cabinet user (else 401)."""
    if not x_companion_user_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Personal access key required (X-Companion-User-Key).",
        )
    head_key = settings.companion_head_key
    if head_key and secrets.compare_digest(x_companion_user_key, head_key):
        return CompanionIdentity(
            user_id=STATIC_HEAD_USER_ID,
            role=CompanionRole.HEAD,
            bitrix_user_id=None,
            name="РОП",
        )
    user = await session.scalar(
        select(CompanionUser).where(
            CompanionUser.key_sha256 == hash_key(x_companion_user_key),
            CompanionUser.active.is_(True),
        ),
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked personal access key.",
        )
    return CompanionIdentity(
        user_id=user.id,
        role=CompanionRole(user.role),
        bitrix_user_id=user.bitrix_user_id,
        name=user.name,
        department_id=(
            user.department_id if CompanionRole(user.role) is CompanionRole.HEAD
            else None
        ),
    )


async def _manager_department_bitrix_id(
    session: AsyncSession,
    manager_bitrix_user_id: int,
) -> int | None:
    """The Bitrix department id of a manager, or None if unknown/unenriched."""
    return await session.scalar(
        select(Department.bitrix_id)
        .join(Manager, Manager.department_id == Department.id)
        .where(Manager.bitrix_user_id == manager_bitrix_user_id),
    )


async def ensure_can_view_manager(
    session: AsyncSession,
    identity: CompanionIdentity,
    manager_bitrix_user_id: int | None,
) -> None:
    """403 unless the identity may see this manager's data.

    MANAGER: only themselves. Global HEAD: everyone. Department-scoped HEAD:
    only managers whose ``managers`` row maps to their Bitrix department —
    unknown/unenriched managers (and unattributed items) stay global-head-only.
    """
    if identity.role is CompanionRole.HEAD:
        if identity.department_id is None:
            return
        if manager_bitrix_user_id is not None:
            dept = await _manager_department_bitrix_id(
                session,
                manager_bitrix_user_id,
            )
            if dept == identity.department_id:
                return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A department head can only view their own department.",
        )
    if (
        manager_bitrix_user_id is None
        or manager_bitrix_user_id != identity.bitrix_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managers can only view their own data.",
        )


def ensure_head(
    identity: CompanionIdentity,
    department_bitrix_id: int | None = None,
) -> None:
    """403 unless a head; a scoped head only within their own department."""
    if identity.role is not CompanionRole.HEAD:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the head of sales can view team-wide data.",
        )
    if (
        identity.department_id is not None
        and department_bitrix_id is not None
        and department_bitrix_id != identity.department_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A department head can only view their own department.",
        )


def ensure_access_admin(identity: CompanionIdentity) -> None:
    """403 unless a head (any scope) — the entry gate to ``/users``.

    What a scoped head may touch inside is checked per-row by the endpoints;
    minting head keys stays behind ``ensure_global_head``.
    """
    if identity.role is not CompanionRole.HEAD:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a head of sales can manage cabinet access.",
        )


def ensure_global_head(identity: CompanionIdentity) -> None:
    """403 unless the global (unscoped) head — guards access management."""
    if not identity.is_global_head:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the global head of sales can manage cabinet access.",
        )
