"""Download a call recording from Bitrix to local storage.

Two paths (both seen on this portal): a native Voximplant ``CALL_RECORD_URL``
(token in the URL), or a Bitrix Drive file resolved via ``disk.file.get`` ->
``DOWNLOAD_URL`` (needs the ``disk`` webhook scope). A relative DOWNLOAD_URL is
joined to the portal origin (audit finding).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import httpx

from AtamuraOKK.bitrix import BitrixClient
from AtamuraOKK.settings import settings


class RecordingUnavailableError(RuntimeError):
    """No usable recording URL/file id for a call."""


def portal_origin() -> str:
    """``scheme://host`` of the configured Bitrix webhook (for relative URLs)."""
    parts = urlsplit(settings.bitrix_base)
    return f"{parts.scheme}://{parts.netloc}"


async def resolve_download_url(bx: BitrixClient, file_id: str) -> str | None:
    """Resolve a Bitrix Drive file id to a direct download URL."""
    info = await bx.call("disk.file.get", {"id": file_id})
    if not info:
        return None
    url = info.get("DOWNLOAD_URL")
    if isinstance(url, str) and url.startswith("/"):
        return portal_origin() + url
    return url if isinstance(url, str) else None


def _ext_from_url(url: str, default: str = ".mp3") -> str:
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return Path(name).suffix or default


async def download_recording(
    bx: BitrixClient,
    http: httpx.AsyncClient,
    *,
    record_url: str | None,
    record_file_id: str | None,
    dest_dir: Path,
    stem: str,
) -> Path:
    """Download a recording to ``dest_dir/<stem><ext>`` and return its path.

    :raises RecordingUnavailableError: if neither a URL nor a file id resolves.
    :raises httpx.HTTPError: on a failed download.
    """
    url = record_url
    if not url and record_file_id:
        url = await resolve_download_url(bx, record_file_id)
    if not url:
        raise RecordingUnavailableError("no record url / resolvable file id")

    dest_dir.mkdir(parents=True, exist_ok=True)
    response = await http.get(url)
    response.raise_for_status()
    dest = dest_dir / f"{stem}{_ext_from_url(url)}"
    dest.write_bytes(response.content)
    return dest
