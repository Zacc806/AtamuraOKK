"""Shared-bearer-token auth for the companion read API.

Fail closed: if ``companion_api_token`` is unset the API returns 503 rather than
serving call-quality data unauthenticated. Compared in constant time.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from AtamuraOKK.settings import settings


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
