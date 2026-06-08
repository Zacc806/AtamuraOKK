"""Standalone config for the ОП-meeting scoring automation.

Independent of the call-scoring ``AtamuraOKK.settings`` so this automation does
not depend on (or modify) the other programmer's scoring. Reads the same ``.env``
via ``ATAMURAOKK_`` env vars. Field names mirror what the engine expects, so the
engine modules just import ``config as settings``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root (…/AtamuraOKK), derived locally so this automation never imports the
# call-scoring ``AtamuraOKK.settings`` module.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class MeetingScoringConfig(BaseSettings):
    """Configuration for the ОП-meeting scorer (Anthropic Claude)."""

    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        ),
    )
    anthropic_model: str = "claude-sonnet-4-6"

    # Scoring engine knobs.
    score_pass_threshold: int = 75
    score_max_retries: int = 5
    score_retry_base_delay: float = 1.0
    score_max_transcript_chars: int = 24000
    score_script_version: str = ""  # sales-script id for deviation dim; empty = off

    # Meeting chunking (long transcripts).
    score_meeting_rubric_version: str = "okk_meeting_v1"
    score_meeting_chunk_chars: int = 12000
    score_meeting_overlap_lines: int = 1

    # --- Meeting-recording ingestion (Bitrix Disk → transcribe → score) ---
    # Inbound webhook (shared neutral infra; same value the call pipeline uses).
    bitrix_webhook: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_BITRIX_WEBHOOK",
            "BITRIX_WEBHOOK",
        ),
    )
    # Bitrix Disk folder holding ОП meeting recordings ("Встречи ОП"). The МОПs'
    # personal disks were consolidated here; it is a mixed dump, so non-A/V files
    # (scans, photos, docs) are filtered out by extension.
    meetings_disk_folder_id: int = 804938
    # How deep to walk the folder tree under the root.
    meetings_walk_max_depth: int = 6
    # Extensions (lowercase, with dot) treated as a meeting recording. WhatsApp
    # voice notes arrive as ".mp4" audio, hence mp4/mov are included.
    meetings_audio_exts: tuple[str, ...] = (
        ".mp3",
        ".m4a",
        ".wav",
        ".ogg",
        ".opus",
        ".amr",
        ".aac",
        ".wma",
        ".flac",
        ".mp4",
        ".mov",
        ".m4v",
        ".3gp",
        ".webm",
    )
    # Skip recordings whose audio is shorter than this (seconds) — not a meeting.
    meetings_min_duration_sec: int = 60
    # Self-contained working dir: downloaded audio + the SQLite state live here
    # (no Postgres — keeps this automation parallel to the call pipeline).
    meetings_work_dir: Path = _REPO_ROOT / ".meetings"
    # SQLite state file; relative paths resolve under ``meetings_work_dir``.
    meetings_db_path: str = "meetings.db"
    # How many recordings each stage processes per invocation.
    meetings_batch_limit: int = 50
    # Give up on a recording after this many failed download/transcribe attempts.
    meetings_max_attempts: int = 4

    # Transcription engine for meetings: "whisper" (local faster-whisper, no API
    # quota — the default) or "openai" (gpt-4o-transcribe).
    meetings_transcribe_engine: str = "whisper"
    meetings_whisper_model: str = "large-v3"
    meetings_whisper_device: str = "cpu"
    meetings_whisper_compute_type: str = "int8"
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_OPENAI_API_KEY",
            "OPENAI_API_KEY",
        ),
    )
    openai_transcribe_model: str = "gpt-4o-transcribe"

    # Manipulation detector (ТЗ 2.1) — off until scoring/meetings/zhk/ is filled.
    manipulation_check_enabled: bool = False
    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
        ),
    )
    telegram_alert_chat_id: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ATAMURAOKK_",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


config = MeetingScoringConfig()
