"""Generate a half-day QA report (optionally running the pipeline first)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

from AtamuraOKK.reporting.aggregate import aggregate_window, compute_window
from AtamuraOKK.reporting.render import render_docx, render_markdown
from AtamuraOKK.reporting.writer import ReportWriter
from AtamuraOKK.settings import settings


@dataclass
class ReportResult:
    """Paths to the generated report files."""

    markdown_path: Path
    docx_path: Path
    n_scored: int


async def _run_pipeline() -> None:
    """Process newly-finished calls so the report reflects the latest data."""
    from AtamuraOKK.ingestion.download import download_pending  # noqa: PLC0415
    from AtamuraOKK.ingestion.service import (  # noqa: PLC0415
        refresh_qualification,
        run_ingestion,
    )
    from AtamuraOKK.ops.retry import requeue_failed  # noqa: PLC0415
    from AtamuraOKK.scoring.worker import score_pending  # noqa: PLC0415
    from AtamuraOKK.transcription.worker import transcribe_pending  # noqa: PLC0415

    logger.info("Report pipeline pre-pass: requeue -> ingest -> ... -> score")
    await requeue_failed()  # auto-recover transient failures from prior runs
    await run_ingestion()
    await refresh_qualification()
    await download_pending()
    await transcribe_pending()
    await score_pending()


async def generate_report(
    half: str,
    *,
    day: date_cls | None = None,
    run_pipeline: bool = False,
) -> ReportResult:
    """Generate the report for ``half`` ("morning"/"afternoon") of ``day``."""
    if run_pipeline:
        await _run_pipeline()

    if day is None:
        day = datetime.now(ZoneInfo(settings.report_timezone)).date()

    start, end, label = compute_window(day, half)
    data = await aggregate_window(start, end, half, label)
    narrative = await ReportWriter().write(data)

    out_dir = settings.report_dir
    stem = f"{day.isoformat()}_{half}"
    md_path = out_dir / f"{stem}.md"
    docx_path = out_dir / f"{stem}.docx"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(data, narrative), encoding="utf-8")
    render_docx(data, narrative, docx_path)

    logger.info(
        "Report ({half}) for {label}: {n} calls -> {md}",
        half=half,
        label=label,
        n=data.n_scored,
        md=md_path,
    )
    return ReportResult(md_path, docx_path, data.n_scored)
