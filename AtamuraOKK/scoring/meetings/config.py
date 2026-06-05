"""Standalone config for the ОП-meeting scoring automation.

Independent of the call-scoring ``AtamuraOKK.settings`` so this automation does
not depend on (or modify) the other programmer's scoring. Reads the same ``.env``
via ``ATAMURAOKK_`` env vars. Field names mirror what the engine expects, so the
engine modules just import ``config as settings``.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
