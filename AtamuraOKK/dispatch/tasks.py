"""Broker tasks: one task per call per stage.

Each task is the thin broker wrapper around a stage's per-call unit of work
(``download_one`` / ``transcribe_one`` / ``score_one``). The dispatcher claims a
row (flipping it into an in-flight status) *before* enqueuing, and each unit of
work re-checks that claim, so a task is idempotent: a duplicate delivery simply
finds the row already advanced and returns ``"skipped"``.

``ctx`` is the arq job context (a plain dict). Stage workers preload expensive,
reusable resources (the whisper model, the scorer, the rubric) into ``ctx`` in
their ``on_startup`` hook so tasks don't rebuild them per call.
"""

from __future__ import annotations

from typing import Any

from AtamuraOKK.ingestion.download import download_one
from AtamuraOKK.scoring.worker import score_one
from AtamuraOKK.transcription.worker import transcribe_one


def queue_for(stage_name: str) -> str:
    """Queue name a given stage's tasks are routed to / consumed from (arq)."""
    return f"queue:{stage_name}"


async def download_task(ctx: dict[str, Any], call_id: int) -> str:
    """Download one claimed call."""
    return await download_one(call_id)


async def transcribe_task(ctx: dict[str, Any], call_id: int) -> str:
    """Transcribe one claimed call, reusing the preloaded model from ctx."""
    return await transcribe_one(
        call_id,
        transcriber=ctx.get("transcriber"),
        storage=ctx.get("storage"),
    )


async def score_task(ctx: dict[str, Any], call_id: int) -> str:
    """Score one claimed call, reusing the preloaded scorer/rubric from ctx."""
    return await score_one(
        call_id,
        scorer=ctx.get("scorer"),
        rubric=ctx.get("rubric"),
    )


# Stage name -> task coroutine. The dispatcher enqueues by task name.
STAGE_TASKS = {
    "download": download_task,
    "transcribe": transcribe_task,
    "score": score_task,
}
