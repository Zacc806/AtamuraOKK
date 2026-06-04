"""Daily pipeline run-summary: throughput, backlog, failures, cost estimate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from AtamuraOKK.db.session import session_scope
from AtamuraOKK.settings import settings


@dataclass
class DailySummary:
    """Aggregated pipeline activity for one day."""

    day: str
    status_counts: dict[str, int] = field(default_factory=dict)
    ingested: int = 0
    transcribed: int = 0
    scored: int = 0
    pending_kk: int = 0
    backlog_download: int = 0
    backlog_transcribe: int = 0
    backlog_score: int = 0
    failed_total: int = 0
    failed_by_stage: dict[str, int] = field(default_factory=dict)
    dead_letter: int = 0
    audio_minutes: float = 0.0
    cost_transcribe: float = 0.0
    cost_score: float = 0.0

    @property
    def cost_total(self) -> float:
        """Estimated total USD cost for the day."""
        return round(self.cost_transcribe + self.cost_score, 2)


async def build_summary(day: date_cls | None = None) -> DailySummary:
    """Compute the run summary for ``day`` (default: today in report tz)."""
    tz = settings.report_timezone
    if day is None:
        day = datetime.now(ZoneInfo(tz)).date()
    s = DailySummary(day=day.isoformat())
    p = {"day": day, "tz": tz, "max": settings.max_retries}

    async with session_scope() as session:

        async def scalar(sql: str) -> Any:
            return await session.scalar(text(sql), p)

        # Lifecycle funnel (all calls).
        for st, n in (
            await session.execute(text("SELECT status, COUNT(*) FROM calls GROUP BY 1"))
        ).all():
            s.status_counts[st] = n

        s.ingested = await scalar(
            "SELECT COUNT(*) FROM calls "
            "WHERE (created_at AT TIME ZONE :tz)::date = :day",
        )
        s.transcribed = await scalar(
            "SELECT COUNT(*) FROM transcripts "
            "WHERE (created_at AT TIME ZONE :tz)::date = :day",
        )
        s.scored = await scalar(
            "SELECT COUNT(*) FROM scores "
            "WHERE (created_at AT TIME ZONE :tz)::date = :day",
        )
        s.pending_kk = await scalar(
            "SELECT COUNT(*) FROM calls WHERE status='PENDING_KK'",
        )

        s.backlog_download = await scalar(
            "SELECT COUNT(*) FROM calls WHERE analyzable AND status='NEW'",
        )
        s.backlog_transcribe = await scalar(
            "SELECT COUNT(*) FROM calls WHERE analyzable AND status='DOWNLOADED'",
        )
        s.backlog_score = await scalar(
            "SELECT COUNT(*) FROM calls WHERE analyzable AND status='TRANSCRIBED'",
        )

        s.failed_total = await scalar(
            "SELECT COUNT(*) FROM calls WHERE status='FAILED'",
        )
        s.dead_letter = await scalar(
            "SELECT COUNT(*) FROM calls WHERE status='FAILED' AND attempts >= :max",
        )
        for stage, n in (
            await session.execute(
                text(
                    "SELECT split_part(error, ':', 1) AS stage, COUNT(*) "
                    "FROM calls WHERE status='FAILED' AND error IS NOT NULL "
                    "GROUP BY 1",
                ),
            )
        ).all():
            s.failed_by_stage[stage] = n

        # Audio minutes transcribed today (stereo billed as 2 channels).
        billed_seconds = await scalar(
            "SELECT COALESCE(SUM(ca.duration_sec * "
            '  CASE WHEN t.segments @> \'[{"speaker":"agent"}]\'::jsonb '
            "       THEN 2 ELSE 1 END), 0) "
            "FROM transcripts t JOIN calls ca ON ca.id = t.call_id "
            "WHERE (t.created_at AT TIME ZONE :tz)::date = :day",
        )
        s.audio_minutes = round(float(billed_seconds or 0) / 60.0, 1)
        s.cost_transcribe = round(s.audio_minutes * settings.cost_transcribe_per_min, 2)

        # Scoring cost estimate from transcript length (~4 chars/token + overhead).
        est_input_tokens = await scalar(
            "SELECT COALESCE(SUM((LENGTH(t.full_text) + 2500) / 4.0), 0) "
            "FROM scores sc JOIN transcripts t ON t.call_id = sc.call_id "
            "WHERE (sc.created_at AT TIME ZONE :tz)::date = :day",
        )
        est_output_tokens = s.scored * 1500  # structured JSON, ~18 criteria
        s.cost_score = round(
            float(est_input_tokens or 0) / 1000 * settings.cost_score_input_per_1k
            + est_output_tokens / 1000 * settings.cost_score_output_per_1k,
            2,
        )

    return s


def render_summary(s: DailySummary) -> str:
    """Render the summary as a compact text block (for console/Telegram)."""
    funnel = ", ".join(f"{k}={v}" for k, v in sorted(s.status_counts.items()))
    failures = ", ".join(f"{k}={v}" for k, v in s.failed_by_stage.items()) or "—"
    return (
        f"📊 Atamura QA — сводка за {s.day}\n"
        f"Обработано: ингест {s.ingested}, транскрибировано {s.transcribed}, "
        f"оценено {s.scored} (KK отложено: {s.pending_kk})\n"
        f"Очередь: загрузка {s.backlog_download}, транскрипция {s.backlog_transcribe}, "
        f"оценка {s.backlog_score}\n"
        f"Ошибки: всего FAILED {s.failed_total} ({failures}); "
        f"dead-letter {s.dead_letter}\n"
        f"Аудио: {s.audio_minutes} мин; "
        f"оценка стоимости ~${s.cost_total} "
        f"(транскрипция ${s.cost_transcribe} + оценка ${s.cost_score})\n"
        f"Статусы: {funnel}"
    )
