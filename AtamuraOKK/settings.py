import enum
from pathlib import Path
from tempfile import gettempdir

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from yarl import URL

TEMP_DIR = Path(gettempdir())


class LogLevel(enum.StrEnum):
    """Possible log levels."""

    NOTSET = "NOTSET"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


class Settings(BaseSettings):
    """
    Application settings.

    These parameters can be configured
    with environment variables.
    """

    host: str = "127.0.0.1"
    port: int = 8000
    # quantity of workers for uvicorn
    workers_count: int = 1
    # Enable uvicorn reloading
    reload: bool = False

    # Current environment
    environment: str = "dev"

    log_level: LogLevel = LogLevel.INFO
    # Variables for the database
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "AtamuraOKK"
    db_pass: str = "AtamuraOKK"  # noqa: S105
    db_base: str = "AtamuraOKK"
    db_echo: bool = False

    # --- Bitrix24 ---
    # Inbound-webhook base URL, e.g.
    # https://<portal>.bitrix24.kz/rest/<user_id>/<token>/
    # Accept both the prefixed and bare spellings in .env.
    bitrix_webhook: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_BITRIX_WEBHOOK",
            "BITRIX_WEBHOOK",
        ),
    )
    # Seconds to wait/retry on Bitrix QUERY_LIMIT_EXCEEDED throttling.
    bitrix_max_retries: int = 5
    bitrix_retry_base_delay: float = 1.0

    # --- Phase 0 spike ---
    # Where the transcription-eval spike writes calls metadata, audio, and
    # transcripts.
    spike_dir: Path = TEMP_DIR / "atamura_spike"
    # faster-whisper model + device for the spike.
    whisper_model: str = "large-v3"
    whisper_device: str = "auto"
    whisper_compute_type: str = "default"

    # --- Groq (production transcription + Russian scoring) ---
    groq_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ATAMURAOKK_GROQ_API_KEY", "GROQ_API_KEY"),
    )
    groq_whisper_model: str = "whisper-large-v3"
    groq_scoring_model: str = "llama-3.3-70b-versatile"

    # --- Yandex (Kazakh/shala scoring; optional Kazakh transcription) ---
    yandex_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ATAMURAOKK_YANDEX_API_KEY", "YANDEX_API_KEY"),
    )
    yandex_folder_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_YANDEX_FOLDER_ID",
            "YANDEX_FOLDER_ID",
        ),
    )
    yandex_gpt_model: str = "yandexgpt/latest"
    # Yandex SpeechKit (STT) model for Kazakh transcription.
    yandex_speechkit_model: str = "general"

    # --- Anthropic (default production scorer) ---
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        ),
    )
    anthropic_model: str = "claude-sonnet-4-6"

    # --- Scoring ---
    # Default scorer provider: "anthropic" (Claude) | "groq_yandex" (language-routed).
    score_provider: str = "anthropic"
    # tm_call_v3 = обновлённый чек-лист звонка (измененные поля, 21 крит, max 100).
    score_rubric_version: str = "tm_call_v3"
    # Sales-script id for the deviation dimension; empty = script check disabled.
    score_script_version: str = ""
    score_pass_threshold: int = 75
    # Detected-language probability above which a call is routed to the Russian
    # scorer; below it (or any Kazakh signal) routes to the Kazakh scorer.
    score_lang_confidence: float = 0.75
    score_max_retries: int = 5
    score_retry_base_delay: float = 1.0
    score_concurrency: int = 4
    transcribe_concurrency: int = 6
    # Minimum duration (seconds) for a full evaluation (ТЗ: raised 60 -> 90).
    score_min_duration_sec: int = 90
    # Below this (seconds) a call is a technical non-call (wrong number / transfer
    # / drop); 30-90s is the "короткий контакт" category — neither is full-scored.
    short_contact_min_sec: int = 30
    # Hard cap on transcript characters sent to the LLM (cost guard).
    score_max_transcript_chars: int = 24000

    # --- Ingestion / workers ---
    # Local directory where call recordings are stored before transcription.
    audio_dir: Path = TEMP_DIR / "atamura_audio"
    # Only ingest answered calls at least this long (seconds).
    ingest_min_duration_sec: int = 15
    # Look-back window (days) on the first ingest run with no cursor.
    ingest_days_back: int = 2
    # Overlap (hours) subtracted from the window end to catch late-written rows.
    ingest_window_overlap_hours: int = 2
    # APScheduler intervals (minutes) and per-tick batch sizes.
    ingest_interval_min: int = 60
    user_sync_interval_min: int = 360
    pipeline_interval_min: int = 5
    download_batch_size: int = 20
    transcribe_batch_size: int = 20
    score_batch_size: int = 20
    # Hardening: max pipeline attempts before a FAILED call is left alone;
    # how often maintenance runs; how long downloaded audio is kept.
    max_call_attempts: int = 3
    requeue_batch_size: int = 50
    maintenance_interval_min: int = 60
    summary_interval_min: int = 1440
    audio_retention_days: int = 90

    @property
    def db_url(self) -> URL:
        """
        Assemble database URL from settings.

        :return: database URL.
        """
        return URL.build(
            scheme="postgresql+asyncpg",
            host=self.db_host,
            port=self.db_port,
            user=self.db_user,
            password=self.db_pass,
            path=f"/{self.db_base}",
        )

    @property
    def bitrix_base(self) -> str:
        """Webhook base URL guaranteed to end with exactly one slash."""
        return self.bitrix_webhook.rstrip("/") + "/"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ATAMURAOKK_",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


settings = Settings()
