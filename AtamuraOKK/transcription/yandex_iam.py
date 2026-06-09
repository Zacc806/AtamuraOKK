"""Mint and cache Yandex IAM tokens from a service-account authorized key.

The authorized-key JSON (``id``, ``service_account_id``, ``private_key``) is
exchanged for a short-lived IAM token: we sign a PS256 JWT with the SA's private
key and POST it to the IAM token endpoint. The IAM token carries the SA's full
role set with no API-key scope restriction — which is why this path is used when
scoped API keys fail authorization.

Tokens are valid ~12 h; we cache one and refresh a few minutes before expiry.
The exchange is synchronous (httpx) and thread-safe, matching the blocking gRPC
recognize call the provider runs via ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import jwt

from AtamuraOKK.settings import PROJECT_ROOT, settings

# Refresh this many seconds before the token's stated expiry.
_REFRESH_MARGIN = 300
# JWT lifetime for the exchange request (max allowed is 1 h).
_JWT_TTL = 3600


class IamTokenProvider:
    """Caches an IAM token minted from a service-account authorized key."""

    def __init__(
        self,
        key_file: str | None = None,
        *,
        iam_endpoint: str | None = None,
    ) -> None:
        self._key_file = key_file or settings.yandex_sa_key_file
        self._iam_endpoint = iam_endpoint or settings.yandex_iam_endpoint
        self._key: dict[str, Any] | None = None
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def _load_key(self) -> dict[str, Any]:
        if self._key is not None:
            return self._key
        path = Path(self._key_file).expanduser()
        if not path.is_absolute():
            # Resolve relative paths against the repo root, not the CWD, so the
            # workers find the key regardless of where they're launched.
            path = PROJECT_ROOT / path
        if not self._key_file or not path.is_file():
            raise RuntimeError(
                f"Yandex SA key file not found: {self._key_file!r} "
                "(ATAMURAOKK_YANDEX_SA_KEY_FILE).",
            )
        key = json.loads(path.read_text(encoding="utf-8"))
        for field in ("id", "service_account_id", "private_key"):
            if not key.get(field):
                raise RuntimeError(f"Authorized key JSON missing {field!r}.")
        self._key = key
        return key

    def _signed_jwt(self) -> str:
        key = self._load_key()
        now = int(time.time())
        payload = {
            "aud": self._iam_endpoint,
            "iss": key["service_account_id"],
            "iat": now,
            "exp": now + _JWT_TTL,
        }
        return jwt.encode(
            payload,
            key["private_key"],
            algorithm="PS256",
            headers={"kid": key["id"]},
        )

    def _refresh(self) -> None:
        resp = httpx.post(
            self._iam_endpoint,
            json={"jwt": self._signed_jwt()},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["iamToken"]
        # Refresh on our own clock rather than parsing the RFC3339 expiresAt.
        self._expires_at = time.time() + 11 * 3600

    def token(self) -> str:
        """Return a valid IAM token, refreshing if missing or near expiry."""
        with self._lock:
            if not self._token or time.time() >= self._expires_at - _REFRESH_MARGIN:
                self._refresh()
            return self._token
