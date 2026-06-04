"""Enumerations shared across DB models."""

import enum


class CallDirection(enum.StrEnum):
    """Direction of a call (maps from Bitrix CALL_TYPE: 1=out, 2=in)."""

    OUTBOUND = "outbound"
    INBOUND = "inbound"
    UNKNOWN = "unknown"


class CallStatus(enum.StrEnum):
    """Lifecycle status of an analyzable call.

    Non-analyzable calls (not the client's first call, or client not qualified)
    are parked in ``SKIPPED`` with a ``skip_reason`` and never downloaded.
    """

    NEW = "NEW"  # ingested, awaiting download
    DOWNLOADED = "DOWNLOADED"  # audio in object storage
    TRANSCRIBED = "TRANSCRIBED"  # transcript persisted
    SCORED = "SCORED"  # QA score persisted
    PUSHED = "PUSHED"  # optional writeback to Bitrix done
    FAILED = "FAILED"  # gave up after retries (see error)
    SKIPPED = "SKIPPED"  # out of analysis scope (see skip_reason)
