"""Enumerations shared across DB models."""

import enum


class CallDirection(enum.StrEnum):
    """Direction of a call (maps from Bitrix CALL_TYPE: 1=out, 2=in)."""

    OUTBOUND = "outbound"
    INBOUND = "inbound"
    UNKNOWN = "unknown"


class CompanionRole(enum.StrEnum):
    """Access level of a companion-cabinet user.

    ``MANAGER`` sees only their own data (scorecard/calls/day/feedback);
    ``HEAD`` (руководитель отдела продаж) sees every manager and the team
    rollup.
    """

    MANAGER = "manager"
    HEAD = "head"


class CallStatus(enum.StrEnum):
    """Lifecycle status of an analyzable call.

    Non-analyzable calls (not the client's first call, or client not qualified)
    are parked in ``SKIPPED`` with a ``skip_reason`` and never downloaded.

    The ``*ING`` states (``DOWNLOADING``/``TRANSCRIBING``/``SCORING``) are the
    in-flight claims: a worker atomically flips a row into one of them (see
    ``dispatch.claim``) so no other worker re-processes it. A crashed worker's
    claim is reverted by the stale-claim reconciler (``claimed_at`` + TTL).
    """

    NEW = "NEW"  # ingested, awaiting download
    DOWNLOADING = "DOWNLOADING"  # claimed for download by a worker (in flight)
    DOWNLOADED = "DOWNLOADED"  # audio in object storage
    TRANSCRIBING = "TRANSCRIBING"  # claimed for transcription by a worker (in flight)
    TRANSCRIBED = "TRANSCRIBED"  # transcript persisted
    SCORING = "SCORING"  # claimed for scoring by a worker (in flight)
    SCORED = "SCORED"  # QA score persisted
    PUSHED = "PUSHED"  # optional writeback to Bitrix done
    FAILED = "FAILED"  # gave up after retries (see error)
    SKIPPED = "SKIPPED"  # out of analysis scope (see skip_reason)
    PENDING_KK = "PENDING_KK"  # Kazakh call held until a Kazakh STT provider exists
