"""Close-reason audit — check a closed-lost deal's stated reason against reality.

The offline probe lives in ``scripts/close_reason_audit.py``; this package is the
productionized loop — a dispatcher pass (``service.run_audit``) that settles freshly
closed-lost deals and persists an ``AuditVerdict`` per deal, which «Мой день» then
surfaces as the «Отказы не по делу» queue. Two routes: the «Дубль…» reasons are
checked against the CRM (``duplicates``: does the number really sit on another deal?),
every other reason is LLM-judged against the client's transcripts (``judge``).
"""

from AtamuraOKK.audit.duplicates import (
    CHECK_ID,
    DuplicateCheck,
    check_many,
    check_one,
    dup_reason_ids,
    project_of,
)
from AtamuraOKK.audit.judge import VERDICTS, build_judge_client, judge_one
from AtamuraOKK.audit.service import AUDIT_CURSOR_KEY, AuditStats, run_audit
from AtamuraOKK.audit.telephony import (
    TelephonyCheck,
    audit_window,
    never_reached_reason_ids,
)

__all__ = [
    "AUDIT_CURSOR_KEY",
    "CHECK_ID",
    "VERDICTS",
    "AuditStats",
    "DuplicateCheck",
    "TelephonyCheck",
    "audit_window",
    "build_judge_client",
    "check_many",
    "check_one",
    "dup_reason_ids",
    "judge_one",
    "never_reached_reason_ids",
    "project_of",
    "run_audit",
]
