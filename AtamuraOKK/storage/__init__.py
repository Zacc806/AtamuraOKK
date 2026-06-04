"""Object storage for call recordings (S3-compatible)."""

from AtamuraOKK.storage.base import ObjectStorage
from AtamuraOKK.storage.s3 import S3Storage, get_storage

__all__ = ["ObjectStorage", "S3Storage", "get_storage"]
