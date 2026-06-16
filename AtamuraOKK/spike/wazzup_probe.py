"""Phase-0 probe: discover how the Wazzup API exposes calls + recordings.

Read-only. The Wazzup v3 API (``api.wazzup24.com``) is messaging-centric and the
public docs are access-gated, so the endpoint/payload that carries *calls* (and a
downloadable recording URL) is unconfirmed. This script authenticates with the
configured key(s) and empirically maps the surface: which auth header works, what
channels the keys see, the current webhook config, and which candidate GET
endpoints exist — dumping enough raw JSON to fix the field mapping the real
``AtamuraOKK/wazzup/`` ingestion will use.

Keys are read straight from the environment (``ATAMURAOKK_WAZZUP_<number>``) so
the probe runs before any settings/wiring exists. Findings go to
``<spike_dir>/wazzup/`` (gitignored) and stdout.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from loguru import logger

from AtamuraOKK.settings import settings

WAZZUP_BASE = "https://api.wazzup24.com/v3"
# ATAMURAOKK_WAZZUP_<digits> -> the per-number API key.
_KEY_ENV_RE = re.compile(r"^ATAMURAOKK_WAZZUP_(\d+)$")
# Candidate read endpoints to probe (path, description). We don't know which of
# these exist on v3 — the probe records the status of each so we learn the real
# surface (channels are documented; calls/messages history is the open question).
_CANDIDATE_GETS: tuple[tuple[str, str], ...] = (
    ("/channels", "list connected channels (numbers the key sees)"),
    ("/webhooks", "current webhook subscriptions (push vs poll signal)"),
    ("/users", "synced users"),
    ("/calls", "calls history (guess)"),
    ("/call", "calls history (guess, singular)"),
    ("/messages", "messages history (guess)"),
    ("/messages/history", "messages history (guess, nested)"),
    ("/chats", "chats list (guess)"),
    ("/contacts", "contacts (push-sync entity; read?)"),
    ("/deals", "deals (push-sync entity; read?)"),
)
# How many characters of each response body to keep in the findings dump.
_SNIPPET = 1500


def _discover_keys() -> dict[str, str]:
    """Map number -> API key from ATAMURAOKK_WAZZUP_<number> env vars."""
    keys: dict[str, str] = {}
    for name, value in os.environ.items():
        m = _KEY_ENV_RE.match(name)
        if m and value:
            keys[m.group(1)] = value
    return keys


def _redact(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "set"


async def _probe_key(number: str, key: str) -> dict[str, Any]:
    """Probe the API with one key: confirm auth, then hit candidate GETs."""
    result: dict[str, Any] = {"number": number, "key": _redact(key), "endpoints": {}}
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        base_url=WAZZUP_BASE, headers=headers, timeout=30.0, follow_redirects=True
    ) as http:
        for path, desc in _CANDIDATE_GETS:
            entry: dict[str, Any] = {"description": desc}
            try:
                resp = await http.get(path)
                entry["status"] = resp.status_code
                body = resp.text
                entry["body_snippet"] = body[:_SNIPPET]
                # Keep parsed top-level keys so the field names are visible at a glance.
                try:
                    parsed = resp.json()
                    if isinstance(parsed, dict):
                        entry["json_keys"] = sorted(parsed.keys())
                    elif isinstance(parsed, list) and parsed:
                        first = parsed[0]
                        entry["list_len"] = len(parsed)
                        if isinstance(first, dict):
                            entry["item_keys"] = sorted(first.keys())
                except ValueError:
                    entry["json_keys"] = "non-JSON body"
            except httpx.HTTPError as exc:
                entry["error"] = str(exc)
            result["endpoints"][path] = entry
            logger.info(
                "  {num} GET {path} -> {st}",
                num=number,
                path=path,
                st=entry.get("status", entry.get("error", "?")),
            )
    return result


async def run_probe() -> dict[str, Any]:
    """Probe every configured Wazzup key and persist findings."""
    keys = _discover_keys()
    if not keys:
        raise RuntimeError(
            "No Wazzup keys found. Set ATAMURAOKK_WAZZUP_<number>=<key> in .env.",
        )
    logger.info("Probing {n} Wazzup key(s): {nums}", n=len(keys), nums=list(keys))
    findings: dict[str, Any] = {"base": WAZZUP_BASE, "keys": list(keys), "probes": []}
    for number, key in keys.items():
        findings["probes"].append(await _probe_key(number, key))

    out_dir = settings.spike_dir / "wazzup"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "probe.json"
    out.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote probe findings to {path}", path=out)
    return findings
