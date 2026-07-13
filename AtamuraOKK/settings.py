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
    # Safety latch for the only Bitrix *write* path (cash-buyer manager alert via
    # im.notify.personal.add). Off until the webhook is granted the `im` scope and
    # ops opts in; when off the notifier only logs.
    bitrix_notify_enabled: bool = False
    # Only alert for calls that started within this window — guards against a
    # backfill / `scoring run --all` rescore spamming managers about old calls.
    cash_alert_max_age_minutes: int = 60

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
    # Mono safety net: ask SpeechKit to label speakers on single-channel calls so
    # they yield turn-separated segments instead of one undifferentiated blob.
    # Stereo calls separate by audio channel and ignore this.
    yandex_speaker_labeling: bool = True
    # Stereo channel -> role convention. Voximplant records the *customer* on the
    # first channel (lowest channel_tag / left) and our *agent* (manager) on the
    # second — the same layout for inbound and outbound, but the recording is not
    # self-describing, so the mapping is fixed here. This is the 0-based channel
    # that carries the agent; the other channel is the customer. Flip to 0 only if
    # a telephony change reverses the layout (re-check % correct roles on a sample
    # before a paid re-run).
    stereo_agent_channel: int = 1
    # Which LLM scores calls: "anthropic" (Claude, the default) or "openai".
    scoring_provider: str = "anthropic"
    # Scoring model — needs Structured Outputs support (gpt-4o-2024-08-06+).
    openai_scoring_model: str = "gpt-4o"
    # Anthropic scoring (ATAMURAOKK_ANTHROPIC_API_KEY). Sonnet balances cost/quality
    # for ~200 calls/day; structured output via forced tool-use.
    anthropic_api_key: str = ""
    anthropic_scoring_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 8000
    # When True (default), automatic scoring (dispatcher + legacy worker) only
    # claims calls that started *today* in the report timezone. Older TRANSCRIBED
    # calls accumulate and are scored on demand via `python -m AtamuraOKK.scoring
    # run --all`. Set to False to auto-score the full backlog again.
    score_auto_today_only: bool = True

    # --- Close-reason audit (freshly closed-lost deals vs the actual call) ---
    # When True, the dispatcher periodically LLM-judges deals that just closed-lost,
    # comparing the manager's stated отказ-причина against the call transcript, and
    # persists a verdict per deal (surfaced on «Мой день» as «Отказы не по делу»).
    # Off by default — enable only once Anthropic credits are available, else every
    # pass records `error` verdicts. Reuses `anthropic_scoring_model`/max_tokens.
    audit_enabled: bool = False

    # --- Glossary correction (post-STT LLM repair of ЖК names & addresses) ---
    # Yandex v3 has no vocabulary API, so a cheap Claude pass fixes complex names
    # and Kazakh toponyms after transcription. Off by default — enable only once a
    # sample (`python -m AtamuraOKK.spike glossary-sample`) validates the prompt.
    glossary_correct_enabled: bool = False
    glossary_correct_model: str = "claude-haiku-4-5"

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
    # Max calls scanned per ingestion pass. Caps per-tick work so a large
    # cold-start backlog drains over several committed chunks instead of one
    # transaction that overruns the arq job timeout and rolls back — leaving the
    # cursor unadvanced and re-scanning from scratch forever. Each chunk upserts
    # its calls and advances the cursor, so the next tick resumes after it.
    # None = unbounded (the pre-fix behaviour). Override via
    # ATAMURAOKK_INGEST_BATCH_SIZE.
    ingest_batch_size: int | None = 300

    # --- Analysis-scope filter (every recorded call until qualification) ---
    # A call is analyzable until its client "qualifies": the moment a deal of
    # theirs enters the Kanban column below (earliest deal stage-history entry).
    # Calls after that moment are visit logistics, not sales conversations ->
    # skipped (after_qualification). Unknown qualification = in scope.
    ingest_until_qualified: bool = True
    # The Kanban column / deal-stage name that marks qualification. Stage IDs are
    # auto-discovered from this name across all deal pipelines.
    qualified_stage_name: str = "Лид квалифицирован"
    # Optional explicit override of the qualified deal STATUS_IDs (skips discovery),
    # e.g. ["PREPARATION", "C24:PREPAYMENT_INVOIC"].
    qualified_deal_stage_ids: list[str] = Field(default_factory=list)

    # --- Client category (A/B/C/X lead-qualification регламент) ---
    # Bitrix enumeration UF field on the *deal* «Квалификация клиента» holding
    # the manager's A/B/C/X tag. A call's client is resolved to its deals (like
    # qualification) and the tag is read off the most recent deal that carries one.
    # Empty -> categorization disabled (every call full-weight = A). Discover the
    # field id + enum ids via `python -m AtamuraOKK.ingestion discover-category`.
    client_category_field: str = ""
    # Maps the field's enumeration value-ID -> category letter, e.g.
    # {"1006": "A", "1008": "B", "1010": "C", "1012": "X"}.
    client_category_value_map: dict[str, str] = Field(default_factory=dict)

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
    # Bitrix department id of the telemarketing team. The team view counts
    # conversions to «Фактический визит» (companion_meeting_stage_id) only for
    # this department — other departments are meeting offices (ОП) scored on
    # their own recordings, where the TM funnel does not apply.
    companion_tm_department_id: int = 250
    # The Zvandau stage STATUS_ID that marks a conducted meeting (conversion num.).
    companion_meeting_stage_id: str = "C24:WON"  # "Фактический визит (успешная сделка)"
    # Deals never REST at the meeting stage: at the moment of the visit the deal
    # is moved to cat 2 and reassigned to the sales closer, so a snapshot count is
    # always 0. The conducted-meeting fact survives in crm.stagehistory.list and
    # the TM survives in the "Сотрудник TM" employee field — this is that field.
    companion_tm_employee_field: str = "UF_CRM_1751599893"
    # Monthly meeting plan target per manager (a Положение policy input, NOT in
    # Bitrix) — used for plan_pct and the ≥60% bonus gate. Honest config, not data.
    companion_plan_target_meetings: int = 45
    # Action list is capped at max_actions; the three stat counters are computed
    # over up to max_scan open deals (so they reflect the whole pipeline, not just
    # the shown slice). max_scan bounds the paged read for a huge pipeline.
    companion_day_max_actions: int = 50
    companion_day_max_scan: int = 500
    companion_day_cache_ttl_seconds: int = 60
    # «Отказы не по делу» — cap on failed-audit deals shown in «Займись сейчас».
    companion_day_audit_max_items: int = 20
    # РОП «Просроченные задачи» — cap on the team-wide overdue-task list so a
    # long-neglected team can't return an unbounded page (oldest-due first).
    companion_overdue_max_items: int = 200
    # "Важные цифры дня" (today block on /day). The Zvandau stage a deal enters
    # when the manager books a meeting ("Записан на встречу в ОП") — counted via
    # stage history for "назначено сегодня". Call-activity TYPE_ID (Bitrix: 2 =
    # call) for the "записано на сегодня" planned-calls count.
    companion_meeting_set_stage_id: str = "C24:EXECUTING"
    companion_call_activity_type_id: int = 2

    # --- Analytics screen (/analytics) --------------------------------------
    # The Zvandau stage a deal enters when the client is qualified (the funnel's
    # qualification step). The no-show stage feeds the meetings block's no-show
    # count. Both are counted via stage history (entrants), attributed by
    # ASSIGNED_BY_ID (the TM still owns the deal pre-WON, unlike the conducted-
    # visit stage). The CR trend is the trailing N months of conversion (arrived
    # / leads); each month is a separate stage-history pull, so keep this modest.
    companion_qualified_stage_id: str = "C24:PREPAYMENT_INVOIC"
    companion_no_show_stage_id: str = "C24:UC_9OBT14"
    # The Недозвон stages (couldn't reach the client). The funnel's «Недозвон» bar
    # counts distinct deals that entered EITHER of these in the period (stage
    # history, by assignee) — a lead can pass Недозвон 1 then 2 but counts once.
    companion_no_answer_stage_ids: list[str] = Field(
        default_factory=lambda: ["C24:UC_VL3EHH", "C24:UC_LS7DKY"],
    )
    companion_analytics_trend_months: int = 6
    # Deal enumeration UF field holding the «Причина закрытия/отказа» — why a lost
    # deal was closed (значения: «Хронический недозвон», «Дубль…», «Нет одобрения
    # по ипотеке», …). The funnel's «Закрыто (отказ)» bar breaks its count down by
    # this field; labels are resolved live from crm.deal.fields. Empty → no
    # breakdown (just the total). Mirror of companion_tm_employee_field's shape.
    companion_closed_reason_field: str = "UF_CRM_1751600682"
    # "Bought" — after the visit the TM deal moves to the sales funnel (cat 2)
    # and is reassigned to the closer, but it keeps the TM-employee field, so a
    # signed booking (C2:WON) is attributable back to the TM via stage history,
    # the same join used for the conducted visit. Deals don't rest at C2:WON
    # either, so it must be read from stage history, not a snapshot.
    companion_sales_category_id: int = 2
    companion_sold_stage_id: str = "C2:WON"
    # Analytics data (funnel/tasks/meetings/calls + CR trend) changes slowly, so
    # cache it longer than /day's live 60s — a cold pull fans out to many Bitrix
    # reads (the CR trend especially), so a warm cache keeps the screen instant.
    companion_analytics_cache_ttl_seconds: int = 600

    # --- CRM hygiene screen (/hygiene) --------------------------------------
    # "OKK / CRM hygiene" — discipline of keeping the deal card in order after a
    # call, scored live (read-only) from Bitrix. Five criteria, each independently
    # resilient. Computed straight through to Bitrix and cached like /analytics.
    companion_hygiene_cache_ttl_seconds: int = 600
    # Norm (target) per criterion, %, shown on the cards and used to colour them.
    companion_hygiene_norm_pct: int = 85
    # Status criterion: an open TM deal with no activity for longer than this many
    # days is treated as a card whose status is not maintained (stuck stage). The
    # strict "stage matches the call outcome" version needs an OKK transcript-vs-
    # stage check on the scoring side (not wired).
    companion_hygiene_stale_days: int = 14
    # Questionnaire ("anketa") criterion: deal fields (UF_CRM_*) that make up the
    # client questionnaire; a deal counts as filled only when ALL are non-empty, and
    # only deals past qualification are scored (hygiene._ANKETA_STAGES). Empty
    # (default) -> the criterion reports "not_available" until the PM supplies the
    # real field list. The env value is a JSON array — pydantic parses list fields as
    # JSON, so a bare comma-separated string raises SettingsError at import and
    # crash-loops every container:
    # ATAMURAOKK_COMPANION_ANKETA_FIELDS=["UF_CRM_a","UF_CRM_b"]
    companion_anketa_fields: list[str] = Field(default_factory=list)
    # Note criterion («примечание по шаблону»): the note is the manager's own
    # timeline comment on the deal card they called — Bitrix telephony never fills
    # the call activity's DESCRIPTION, so that field is not a source. The base is
    # the deals called in the period (collapsed from their calls), bounded by
    # max_deals; max_calls bounds the activity scan that discovers them.
    # 800 covers a full month for the busiest caller (~760 distinct deals); a
    # tighter cap would silently measure only the most recently called cards, which
    # reads several points better than the real month.
    companion_hygiene_notes_max_deals: int = 800
    companion_hygiene_notes_max_calls: int = 4000
    # A comment counts as a proper note only when it contains this marker. Empty ->
    # any note the manager wrote counts (BB-code markup and integration autoposts
    # are stripped/ignored either way).
    companion_note_template_marker: str = ""
    # Minimum length (chars, markup stripped) for a comment to count as a note.
    # 0 -> any non-empty note counts, however terse («НДЗ»). Raise it once the PM
    # decides what «содержательное примечание» means.
    companion_note_min_chars: int = 0

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

    @property
    def bitrix_portal_origin(self) -> str:
        """Portal origin (scheme://host) derived from the webhook URL.

        The webhook is ``https://<portal>.bitrix24.kz/rest/<user>/<token>/``;
        its scheme+host is the base for human-facing CRM card links. Empty when
        the webhook is unset/malformed, which callers treat as "no link".
        """
        if not self.bitrix_webhook:
            return ""
        url = URL(self.bitrix_webhook)
        if not url.scheme or not url.host:
            return ""
        return f"{url.scheme}://{url.host}"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ATAMURAOKK_",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


settings = Settings()
