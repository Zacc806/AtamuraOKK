import enum
from pathlib import Path
from tempfile import gettempdir

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from yarl import URL

TEMP_DIR = Path(gettempdir())
# Repository root (parent of the AtamuraOKK package dir).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


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

    # --- Object storage (S3-compatible: MinIO in dev) ---
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"  # noqa: S105
    s3_bucket: str = "call-recordings"
    # Use path-style addressing (required by MinIO).
    s3_use_path_style: bool = True

    # --- OpenAI (Russian transcription via gpt-4o-transcribe) ---
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ATAMURAOKK_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_transcribe_model: str = "gpt-4o-transcribe"

    # --- Yandex (SpeechKit STT for Kazakh / shala) ---
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
    yandex_speechkit_model: str = "general"

    # --- Anthropic (default scorer: Claude Sonnet) ---
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
        ),
    )
    anthropic_model: str = "claude-sonnet-4-6"

    # --- Scoring (Anthropic Claude Sonnet handles ru + kk in one model) ---
    score_rubric_version: str = "tm_call_v3"
    score_script_version: str = ""  # sales-script id for deviation dim; empty = off
    score_pass_threshold: int = 75
    score_lang_confidence: float = 0.75
    score_max_retries: int = 5
    score_retry_base_delay: float = 1.0
    score_max_transcript_chars: int = 24000
    score_min_duration_sec: int = 90
    short_contact_min_sec: int = 30

    # --- Meeting scoring (Этап 3: ОП-встречи, long transcripts) ---
    score_meeting_rubric_version: str = "okk_meeting_v1"
    # Soft per-chunk size cap; long meetings are chunked + map-reduced.
    score_meeting_chunk_chars: int = 12000
    score_meeting_overlap_lines: int = 1

    # --- Manipulation detector (ТЗ 2.1) ---
    # Off until the ЖК knowledge base (scoring/zhk/*.json) is populated.
    manipulation_check_enabled: bool = False
    # Telegram admin alert for detected manipulations (optional; logs either way).
    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
        ),
    )
    telegram_alert_chat_id: str = ""

    # --- Sale-outcome backfill (ТЗ 3.4) ---
    # Days after the contact before its CRM deal outcome (won/lose) is recorded.
    outcome_check_days: int = 30

    # --- Ingestion ---
    # How far back the very first ingestion run reaches when no cursor exists.
    ingest_initial_days_back: int = 7
    # Overlap re-scanned each run so calls near the cursor boundary aren't missed
    # (idempotent upsert on bitrix_call_id makes the overlap harmless).
    ingest_overlap_minutes: int = 10
    # A call must be answered (CALL_FAILED_CODE) and at least this long to qualify.
    ingest_success_code: str = "200"
    ingest_min_duration_sec: int = 15

    # --- Analysis-scope filter (first call AND qualified) ---
    # A call is analyzable only if its client is "qualified": a deal of theirs
    # ever entered the Kanban column below (resolved via deal stage history).
    ingest_require_qualified: bool = True
    # The Kanban column / deal-stage name that marks qualification. Stage IDs are
    # auto-discovered from this name across all deal pipelines.
    qualified_stage_name: str = "Лид квалифицирован"
    # Optional explicit override of the qualified deal STATUS_IDs (skips discovery),
    # e.g. ["PREPARATION", "C24:PREPAYMENT_INVOIC"].
    qualified_deal_stage_ids: list[str] = Field(default_factory=list)

    # --- Phase 0 spike ---
    # Where the transcription-eval spike writes calls metadata, audio, and
    # transcripts. Repo-local + gitignored; persistent across runs (unlike TMPDIR).
    spike_dir: Path = PROJECT_ROOT / ".spike"
    # faster-whisper model + device for the spike.
    whisper_model: str = "large-v3"
    whisper_device: str = "auto"
    whisper_compute_type: str = "default"

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
