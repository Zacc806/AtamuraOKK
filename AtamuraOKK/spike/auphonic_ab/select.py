"""Pick 50 May first-touch client calls and write a manifest (read-only).

Selection: ``is_first_call`` AND ``duration_sec > 90`` AND ``started_at`` in May
(report tz) AND audio present. To exercise both the mixed kk/ru angle and the
"does cleanup rescue bad audio" question, the sample is bucketed:

  * up to ``FAILED_TARGET`` calls whose prod transcription FAILED,
  * the remainder split evenly between Kazakh- and Russian-detected calls
    (status TRANSCRIBED/SCORED — known-good audio for a clean baseline).

Deterministic: every bucket is ordered by ``id`` so re-running picks the same
calls. Touches no pipeline-owned state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import ColumnElement, Select, and_, select

from AtamuraOKK.db.models.call import Call
from AtamuraOKK.db.models.enums import CallStatus
from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings
from AtamuraOKK.spike.auphonic_ab import config


@dataclass(slots=True)
class CallRef:
    """One selected call (the bits the runner and export need)."""

    id: int
    bitrix_call_id: str
    audio_object_key: str
    duration_sec: int
    direction: str
    prior_status: str
    prior_language: str | None


def _base_filter() -> ColumnElement[bool]:
    tz = ZoneInfo(settings.report_timezone)
    start = datetime(*config.MONTH_START, tzinfo=tz)
    end = datetime(*config.MONTH_END, tzinfo=tz)
    return and_(
        Call.is_first_call.is_(True),
        Call.duration_sec > 90,
        Call.started_at >= start,
        Call.started_at < end,
        Call.audio_object_key.is_not(None),
    )


def _bucket_query(extra: ColumnElement[bool], limit: int) -> Select[tuple[Call]]:
    return (
        select(Call).where(and_(_base_filter(), extra)).order_by(Call.id).limit(limit)
    )


def _ref(call: Call) -> CallRef:
    return CallRef(
        id=call.id,
        bitrix_call_id=call.bitrix_call_id,
        audio_object_key=call.audio_object_key or "",
        duration_sec=call.duration_sec,
        direction=str(call.direction),
        prior_status=str(call.status),
        prior_language=call.language,
    )


async def select_calls() -> list[CallRef]:
    """Build the bucketed sample and return it (also persisted to the manifest)."""
    good = Call.status.in_([CallStatus.TRANSCRIBED, CallStatus.SCORED])
    async with session_scope() as s:

        async def bucket(extra: ColumnElement[bool], limit: int) -> list[CallRef]:
            result = await s.execute(_bucket_query(extra, limit))
            return [_ref(c) for c in result.scalars().all()]

        failed = await bucket(Call.status == CallStatus.FAILED, config.FAILED_TARGET)
        remaining = config.SAMPLE_SIZE - len(failed)
        per_lang = remaining // 2
        kk = await bucket(and_(good, Call.language == "kk"), per_lang)
        ru = await bucket(and_(good, Call.language == "ru"), remaining - len(kk))

    # De-dup defensively and cap at SAMPLE_SIZE, preserving bucket order.
    seen: set[int] = set()
    chosen: list[CallRef] = []
    for ref in (*failed, *kk, *ru):
        if ref.id not in seen:
            seen.add(ref.id)
            chosen.append(ref)
    chosen = chosen[: config.SAMPLE_SIZE]

    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    config.MANIFEST.write_text(
        json.dumps([asdict(c) for c in chosen], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Selected {n} calls (failed={f}, kk={k}, ru={r}) -> {p}",
        n=len(chosen),
        f=len(failed),
        k=len(kk),
        r=len(ru),
        p=config.MANIFEST,
    )
    return chosen


def load_manifest() -> list[CallRef]:
    """Read back a previously-selected manifest."""
    raw = json.loads(config.MANIFEST.read_text(encoding="utf-8"))
    return [CallRef(**item) for item in raw]
