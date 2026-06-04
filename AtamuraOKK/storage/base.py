"""Provider-agnostic object-storage interface.

Ingestion depends only on :class:`ObjectStorage`, so MinIO (dev) and AWS S3 /
Backblaze B2 (prod) are interchangeable without touching the pipeline.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStorage(Protocol):
    """Minimal blob store: put/exists/get + a presigned URL for dashboards."""

    async def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist (idempotent)."""
        ...

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        """Store ``data`` at ``key``; return the key."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether an object exists at ``key``."""
        ...

    async def download(self, key: str) -> bytes:
        """Fetch the object bytes at ``key``."""
        ...

    def presigned_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        """A time-limited URL to fetch ``key`` (for the call drill-down)."""
        ...
