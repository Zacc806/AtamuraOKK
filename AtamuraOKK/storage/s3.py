"""S3-compatible object storage via boto3 (MinIO in dev, S3/B2 in prod).

boto3 is synchronous; calls are wrapped with ``asyncio.to_thread`` so the async
ingestion worker isn't blocked. At ~200 recordings/day this is more than enough.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from botocore.config import Config
from botocore.exceptions import ClientError
from loguru import logger

from AtamuraOKK.settings import settings

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class S3Storage:
    """ObjectStorage backed by an S3-compatible endpoint."""

    def __init__(
        self,
        *,
        bucket: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self.bucket = bucket or settings.s3_bucket
        self.endpoint_url = endpoint_url or settings.s3_endpoint_url
        self._client: S3Client | None = None

    def _get_client(self) -> S3Client:
        if self._client is None:
            import boto3  # noqa: PLC0415

            addressing = "path" if settings.s3_use_path_style else "auto"
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                config=Config(s3={"addressing_style": addressing}),
            )
        return self._client

    async def ensure_bucket(self) -> None:
        """Create the bucket if missing (idempotent)."""

        def _ensure() -> None:
            client = self._get_client()
            try:
                client.head_bucket(Bucket=self.bucket)
            except ClientError:
                logger.info("Creating bucket {bucket}", bucket=self.bucket)
                client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(_ensure)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        """Store ``data`` at ``key``; return the key."""

        def _put() -> None:
            extra: dict[str, Any] = {}
            if content_type:
                extra["ContentType"] = content_type
            self._get_client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                **extra,
            )

        await asyncio.to_thread(_put)
        return key

    async def exists(self, key: str) -> bool:
        """Whether an object exists at ``key``."""

        def _head() -> bool:
            try:
                self._get_client().head_object(Bucket=self.bucket, Key=key)
            except ClientError:
                return False
            else:
                return True

        return await asyncio.to_thread(_head)

    async def download(self, key: str) -> bytes:
        """Fetch the object bytes at ``key``."""

        def _get() -> bytes:
            resp = self._get_client().get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()

        return await asyncio.to_thread(_get)

    def presigned_url(self, key: str, *, expires_seconds: int = 3600) -> str:
        """A time-limited URL to fetch ``key``."""
        return self._get_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


@lru_cache(maxsize=1)
def get_storage() -> S3Storage:
    """Process-wide default storage instance."""
    return S3Storage()
