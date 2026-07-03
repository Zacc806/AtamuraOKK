"""Close-reason audit — check a closed-lost deal's stated reason against the call.

The offline probe lives in ``scripts/close_reason_audit.py``; this package is the
productionized loop — a dispatcher pass (``service.run_audit``) that judges freshly
closed-lost deals against the client's transcripts and persists an ``AuditVerdict``
per deal, which «Мой день» then surfaces as the «Отказы не по делу» queue.
"""

from AtamuraOKK.audit.judge import VERDICTS, build_judge_client, judge_one
from AtamuraOKK.audit.service import AUDIT_CURSOR_KEY, AuditStats, run_audit

__all__ = [
    "AUDIT_CURSOR_KEY",
    "VERDICTS",
    "AuditStats",
    "build_judge_client",
    "judge_one",
    "run_audit",
]
