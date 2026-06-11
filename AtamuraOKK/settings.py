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

    # --- Transcription / scoring providers ---
    # Which STT engine the transcription worker uses: "whisper" (local
    # faster-whisper, no API quota — the default), "openai" (gpt-4o-transcribe),
    # or "yandex" (SpeechKit v3 streaming — Russian + Kazakh).
    transcribe_provider: str = "whisper"
    # How many calls to transcribe concurrently. On a 10-core CPU, 2 concurrent
    # whisper decodes (~4 CTranslate2 threads each) ≈ 8 cores — the sweet spot.
    transcribe_concurrency: int = 2
    openai_api_key: str = ""
    openai_transcribe_model: str = "gpt-4o-transcribe"
    # --- Yandex SpeechKit (transcribe_provider="yandex") ---
    # Service-account API key: the *secret* is used as the `Api-Key` auth header;
    # the key-id is kept for reference / rotation (not needed by the v3 call).
    # Accept the older API_KEY/IDENTIFICATION_KEY spellings too.
    yandex_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_YANDEX_SECRET_KEY",
            "ATAMURAOKK_YANDEX_API_KEY",
        ),
    )
    yandex_key_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ATAMURAOKK_YANDEX_KEY_ID",
            "ATAMURAOKK_YANDEX_IDENTIFICATION_KEY",
        ),
    )
    yandex_stt_endpoint: str = "stt.api.cloud.yandex.net:443"
    # Auth: if a service-account authorized-key JSON path is set, the provider
    # mints a short-lived IAM token (Bearer auth) from it — this carries the
    # SA's full role set and has no API-key scope restriction. Otherwise it
    # falls back to the API-key (`Api-Key`) auth above.
    yandex_sa_key_file: str = ""
    # IAM token-exchange endpoint (KZ installation; use iam.api.cloud.yandex.net
    # for the global region).
    yandex_iam_endpoint: str = "https://iam.api.yandexcloud.kz/iam/v1/tokens"
    # Recognition mode: "async" (RecognizeFile — whole stereo file, no 5-min cap,
    # the default) or "stream" (RecognizeStreaming — mono, 5-min/session limit).
    yandex_stt_mode: str = "async"
    # Operations API endpoint for polling async recognition (KZ installation;
    # use operation.api.cloud.yandex.net:443 for the global region).
    yandex_operation_endpoint: str = "operation.api.yandexcloud.kz:443"
    # Async polling cadence + per-call ceiling (status checks are quota'd at 5/s).
    yandex_async_poll_interval: float = 2.0
    yandex_async_timeout: float = 900.0
    yandex_stt_model: str = "general"
    # Apply text normalization (numbers, punctuation, capitalization) to finals.
    yandex_stt_normalize: bool = True
    # Languages SpeechKit may recognize (WHITELIST). RU + KK covers the team.
    yandex_stt_languages: list[str] = Field(
        default_factory=lambda: ["ru-RU", "kk-KZ"],
    )
    # Which LLM scores calls: "anthropic" (Claude, the default) or "openai".
    scoring_provider: str = "anthropic"
    # Scoring model — needs Structured Outputs support (gpt-4o-2024-08-06+).
    openai_scoring_model: str = "gpt-4o"
    # Anthropic scoring (ATAMURAOKK_ANTHROPIC_API_KEY). Sonnet balances cost/quality
    # for ~200 calls/day; structured output via forced tool-use.
    anthropic_api_key: str = ""
    anthropic_scoring_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 8000

    # --- Ingestion ---
    # How far back the very first ingestion run reaches when no cursor exists.
    ingest_initial_days_back: int = 7
    # Overlap re-scanned each run so calls near the cursor boundary aren't missed
    # (idempotent upsert on bitrix_call_id makes the overlap harmless).
    ingest_overlap_minutes: int = 10
    # A call must be answered (CALL_FAILED_CODE) and at least this long to qualify.
    # 90s+ only: shorter calls have too little conversation to score meaningfully.
    ingest_success_code: str = "200"
    ingest_min_duration_sec: int = 90

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

    # --- Ops / hardening ---
    # A FAILED call is retried up to this many attempts; beyond that it's
    # dead-lettered (left FAILED for manual review).
    max_retries: int = 4
    # Cost-estimate rates (USD); tune to your provider contract.
    cost_transcribe_per_min: float = 0.006  # gpt-4o-transcribe
    cost_score_input_per_1k: float = 0.0025  # gpt-4o input
    cost_score_output_per_1k: float = 0.01  # gpt-4o output
    # Alerting via Telegram; leave blank to log alerts instead of sending.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Alert when a single run dead-letters at least this many calls.
    alert_failure_threshold: int = 5

    # --- Unified production worker (python -m AtamuraOKK.worker) ---
    # Full ingestion pass (ingest -> requalify -> download) cadence, in hours.
    worker_ingest_interval_hours: int = 3
    # Auto-recovery (requeue FAILED calls) cadence, in hours.
    worker_retry_interval_hours: int = 1
    # Run one ingestion pass immediately on startup (don't wait for the interval).
    worker_run_on_start: bool = True
    # Send the daily run-summary via the alerter at end-of-day (report_day_end_hour).
    worker_send_daily_summary: bool = True

    # --- Distributed workers / broker (python -m AtamuraOKK.dispatch) ---
    # Redis is a *transient* work-dispatch layer; Postgres status stays the
    # source of truth, so Redis runs cache-only and the reconciler rebuilds the
    # queue from Postgres after a wipe.
    redis_url: str = "redis://localhost:6379/0"
    # How often the dispatcher scans Postgres for ready rows and fans them out.
    # arq cron is calendar-based, so for a sub-minute cadence this must divide 60
    # (e.g. 30, 20, 15); any other value falls back to once per minute.
    dispatch_interval_seconds: int = 60
    # How many rows the dispatcher claims per stage per tick.
    claim_batch_size: int = 50
    # Per-stage worker concurrency (arq max_jobs). transcribe reuses
    # transcribe_concurrency (CPU-bound); download/score are IO-bound.
    download_concurrency: int = 5
    score_concurrency: int = 8
    # A claim left in an in-flight status longer than this (worker crashed) is
    # reverted to its ready status by the reconciler. transcribe gets the longest
    # window — a long CPU whisper decode can take minutes.
    claim_stale_seconds_download: int = 600
    claim_stale_seconds_transcribe: int = 1800
    claim_stale_seconds_score: int = 600
    # An arq job is killed this many seconds *before* its stale TTL so a slow but
    # still-alive job is cancelled before the reconciler would revert+re-enqueue
    # it — otherwise a long job runs twice (job_timeout = TTL guarantees it).
    claim_job_timeout_margin_seconds: int = 120
    # Async engine pool sizing (per process). Keep
    # (dispatcher + stage replicas) * (pool_size + max_overflow) under the
    # Postgres max_connections (default 100).
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # --- Reporting (twice-daily QA reports) ---
    report_timezone: str = "Asia/Qyzylorda"
    # The day splits into two halves at this hour (local report tz).
    report_split_hour: int = 13
    # The afternoon/second half is bounded at this hour (end of working day).
    report_day_end_hour: int = 19
    # When the scheduled reports run: lunch (first half) and evening (second half).
    report_lunch_hour: int = 13
    report_evening_hour: int = 19
    # LLM that writes the narrative sections (Structured Outputs capable).
    report_model: str = "gpt-4o"
    # Where generated reports (.md/.docx) are written.
    report_dir: Path = PROJECT_ROOT / "reports"
    # Team-average score norm (below = underperforming), from the QA-dept docs.
    report_score_norm: int = 75

    # --- Companion read API (/api/v1) ---
    # Shared bearer token the sales-companion BFF must present on every /api/v1
    # request. Empty = fail closed (the API returns 503 until a token is set), so
    # call-quality data is never served unauthenticated by accident.
    companion_api_token: str = ""
    # Static personal key for the РОП (head of sales). Set once in the
    # environment; logging in with it grants the HEAD role without a
    # companion_users row. The head then issues manager keys from the cabinet
    # (POST /api/v1/users) — no CLI access needed. Empty = disabled (heads can
    # still be created via the CLI).
    companion_head_key: str = ""

    # --- Companion "Мой день" read-through (live Bitrix Zvandau TM funnel) ---
    # The "Zvandau" deal category (24) whose stages ARE the TM's day signals
    # (кому звонить / недозвон / записан на встречу / факт. визит). Deals here are
    # owned by the telemarketer via ASSIGNED_BY_ID, so the day view attributes to
    # the manager directly. This is the funnel the scored TMs actually work in
    # (the "Телемаркетинг" cat 0 is legacy/closed-out; cat 2 "Отдел продаж" belongs
    # to the sales closer). See docs/companion-day.md.
    companion_tm_category_id: int = 24
    # The Zvandau stage STATUS_ID that marks a conducted meeting (conversion num.).
    companion_meeting_stage_id: str = "C24:WON"  # "Фактический визит (успешная сделка)"
    # Monthly meeting plan target per manager (a Положение policy input, NOT in
    # Bitrix) — used for plan_pct and the ≥60% bonus gate. Honest config, not data.
    companion_plan_target_meetings: int = 45
    # Action list is capped at max_actions; the three stat counters are computed
    # over up to max_scan open deals (so they reflect the whole pipeline, not just
    # the shown slice). max_scan bounds the paged read for a huge pipeline.
    companion_day_max_actions: int = 50
    companion_day_max_scan: int = 500
    companion_day_cache_ttl_seconds: int = 60

    # --- Phase 0 spike ---
    # Where the transcription-eval spike writes calls metadata, audio, and
    # transcripts. Repo-local + gitignored; persistent across runs (unlike TMPDIR).
    spike_dir: Path = PROJECT_ROOT / ".spike"
    # faster-whisper model + device. Defaults target Apple Silicon / CPU, where
    # CTranslate2 has no Metal backend and int8 is the fast path. On a CUDA GPU
    # set ATAMURAOKK_WHISPER_DEVICE=cuda + ATAMURAOKK_WHISPER_COMPUTE_TYPE=float16.
    whisper_model: str = "large-v3"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    # CTranslate2 inter-op workers: lets one model serve N concurrent transcribe
    # calls in parallel. Match to transcribe_concurrency. cpu_threads=0 -> auto.
    whisper_num_workers: int = 2
    whisper_cpu_threads: int = 0

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
