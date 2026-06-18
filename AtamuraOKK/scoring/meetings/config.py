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
REPO_ROOT = Path(__file__).resolve().parents[3]


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
    # Which LLM scores meetings: "anthropic" (Claude, the default) or "openai".
    # OpenAI is the fallback when the Anthropic key is out of credit.
    meetings_scoring_engine: str = "anthropic"
    openai_scoring_model: str = "gpt-4o"

    # Post-STT LLM correction of ЖК names & addresses (shared with the call
    # pipeline; both read the same ATAMURAOKK_GLOSSARY_* vars from one .env).
    # Off by default — enable once a sample validates the prompt/glossary.
    glossary_correct_enabled: bool = False
    glossary_correct_model: str = "claude-haiku-4-5"

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
    # Max chunks of one meeting scored concurrently (rate-limit guard).
    score_meeting_chunk_concurrency: int = 3

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
    meetings_work_dir: Path = REPO_ROOT / ".meetings"
    # SQLite state file; relative paths resolve under ``meetings_work_dir``.
    meetings_db_path: str = "meetings.db"
    # CSV export of scored meetings (`report` command); relative → work dir.
    meetings_report_path: str = "meetings_report.csv"
    # How many recordings each stage processes per invocation.
    meetings_batch_limit: int = 50
    # ``meetings.source`` value stamped on rows pushed to Postgres — set per
    # deployment when other departments start dropping recordings ("op" = ОП).
    meetings_source: str = "op"
    # Give up on a recording after this many failed download/transcribe attempts.
    meetings_max_attempts: int = 4

    # --- Scheduler worker (python -m AtamuraOKK.scoring.meetings.worker) ---
    # How often the full pipeline pass runs, in hours.
    meetings_worker_interval_hours: float = 3.0
    # How often FAILED recordings are re-queued, in hours.
    meetings_worker_retry_interval_hours: float = 6.0
    # Run one pipeline pass immediately on startup instead of waiting a full cycle.
    meetings_worker_run_on_start: bool = True
    # Timezone for the scheduler (matches the ОП reporting tz).
    meetings_worker_timezone: str = "Asia/Qyzylorda"

    # Transcription engine for meetings: "yandex" (SpeechKit v3 async — native
    # ru + kk, the default) or "openai" (gpt-4o-transcribe).
    meetings_transcribe_engine: str = "yandex"
    # --- Yandex SpeechKit (meetings_transcribe_engine="yandex") ---
    # Shared neutral infra with the call pipeline: same ``ATAMURAOKK_YANDEX_*``
    # env vars and service-account authorized key, read here independently.
    yandex_sa_key_file: str = ""
    yandex_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_YANDEX_SECRET_KEY",
            "ATAMURAOKK_YANDEX_API_KEY",
        ),
    )
    yandex_iam_endpoint: str = "https://iam.api.yandexcloud.kz/iam/v1/tokens"
    yandex_stt_endpoint: str = "stt.api.cloud.yandex.net:443"
    yandex_operation_endpoint: str = "operation.api.yandexcloud.kz:443"
    yandex_stt_model: str = "general"
    # Apply text normalization (numbers, punctuation, capitalization) to finals.
    yandex_stt_normalize: bool = True
    # Languages SpeechKit may recognize (WHITELIST). RU + KK covers the team.
    yandex_stt_languages: tuple[str, ...] = ("ru-RU", "kk-KZ")
    # Meetings run hours long, so polling gets its own (laxer) knobs instead of
    # the call pipeline's tighter defaults (status checks are quota'd at 5/s).
    meetings_stt_poll_interval: float = 5.0
    meetings_stt_timeout: float = 3600.0
    # How many recordings to transcribe concurrently. Yandex async STT is
    # network-bound (upload + poll), so parallelism drains the backlog at no CPU
    # cost; each in-flight operation polls every ``meetings_stt_poll_interval``
    # seconds — keep concurrency/interval under the 5/s status-check quota
    # (shared with the call pipeline's transcribe workers).
    meetings_transcribe_concurrency: int = 8
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
