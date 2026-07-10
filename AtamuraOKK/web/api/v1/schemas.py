"""Stable DTOs for the companion read API (``/api/v1``).

These are the contract the sales-companion BFF codes against. They intentionally
expose **derived, business-facing** fields (ОКК 1–5, zone, feedback text) and
never the pipeline's internal ``status`` enum or raw table shape — so the
pipeline can evolve behind this anti-corruption layer.

Identifiers are **Bitrix** ids (``manager_id`` = Bitrix user id,
``department_id`` = Bitrix department id), since the companion is a Bitrix24
app and holds those, not AtamuraOKK's internal row ids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ManagerRef(BaseModel):
    """Identity of a manager, keyed by Bitrix user id."""

    bitrix_user_id: int
    name: str | None = None
    department_id: int | None = Field(
        default=None,
        description="Bitrix department id",
    )
    department_name: str | None = None


class DepartmentRef(BaseModel):
    """Identity of a department, keyed by Bitrix department id."""

    bitrix_id: int
    name: str | None = None


class MeView(BaseModel):
    """Who the cabinet session belongs to — drives the role-aware UI.

    ``role`` is ``manager`` (own data only) or ``head`` (head of sales, sees
    every manager). ``manager`` is the linked profile when the user maps to a
    ``managers`` row (always for managers, optional for the head).
    ``department`` is the session's department scope: the manager's own
    department, or the department a scoped head (office РОП) is limited to;
    ``None`` for the global head.
    """

    role: str
    bitrix_user_id: int | None
    name: str | None = None
    manager: ManagerRef | None = None
    department: DepartmentRef | None = None


class CompanionUserView(BaseModel):
    """One cabinet user as shown in the head's access-management screen."""

    id: int
    role: str
    bitrix_user_id: int | None
    department_id: int | None = Field(
        default=None,
        description="Bitrix department id a head key is scoped to",
    )
    name: str | None
    active: bool
    created_at: datetime | None = None


class CompanionUserCreate(BaseModel):
    """Request to issue a cabinet key.

    ``role=manager`` (the default) needs ``bitrix_user_id``; ``role=head``
    needs ``department_id`` (an office РОП is always department-scoped — the
    cabinet can never mint a global head) plus a ``name`` or a
    ``bitrix_user_id`` to resolve one from.
    """

    role: Literal["manager", "head"] = "manager"
    bitrix_user_id: int | None = Field(
        default=None,
        description="Bitrix user id the key is scoped to (required for manager)",
    )
    department_id: int | None = Field(
        default=None,
        description="Bitrix department id a head key is scoped to (head only)",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Display name; omit to pull it from Bitrix by the user id",
    )

    @model_validator(mode="after")
    def _role_shape(self) -> CompanionUserCreate:
        if self.role == "manager":
            if self.bitrix_user_id is None:
                msg = "bitrix_user_id is required for role 'manager'"
                raise ValueError(msg)
            if self.department_id is not None:
                msg = (
                    "department_id only applies to role 'head' — a manager's "
                    "department comes from the issuing head's scope"
                )
                raise ValueError(msg)
        else:
            if self.department_id is None:
                msg = "department_id is required for role 'head' (an office РОП)"
                raise ValueError(msg)
            if self.name is None and self.bitrix_user_id is None:
                msg = "role 'head' needs a name or a bitrix_user_id to resolve one"
                raise ValueError(msg)
        return self


class CompanionUserCreated(BaseModel):
    """A freshly issued key. ``key`` is returned ONCE — only its hash is stored."""

    user: CompanionUserView
    key: str


class OkkScore(BaseModel):
    """The ОКК call-quality result the bonus modifier is derived from."""

    score_5: int | None = Field(
        default=None,
        description="ОКК 1–5 modifier (null if no scored calls in the period)",
    )
    percent: float | None = Field(
        default=None,
        description="Average 0–100 QA percent over scored qualification calls",
    )
    zone: str | None = Field(
        default=None,
        description="Aggregate zone: strong | normal | borderline | risk",
    )


class MoneyAxis(BaseModel):
    """The conversion/Положение 'money' axis.

    Live on the /day view: meetings come from Zvandau stage history attributed
    via the «Сотрудник ТМ» field, leads from period deal creation (see
    docs/companion-day.md). Scorecard still returns the empty default.
    ``crm_discipline_pct`` stays null — no trustworthy source yet.
    """

    status: str = Field(
        default="not_available",
        description="'live' when the period has leads or meetings",
    )
    conversion_pct: float | None = None
    plan_pct: float | None = None
    crm_discipline_pct: float | None = None
    meetings: int | None = None
    leads_processed: int | None = None
    gates: dict[str, bool] | None = Field(
        default=None,
        description="{plan_ok, crm_ok} bonus gates from the Положение",
    )


class MeetingsScore(BaseModel):
    """Aggregate over scored ОП meetings in a period.

    Meetings keep their own scoring semantics (score %, pass/fail against the
    meeting rubric) — deliberately not the calls' zone/okk_5 scale, because
    each department scores against its own criteria.
    """

    meetings_scored: int = 0
    avg_score_pct: float | None = None
    passed: int = 0
    failed: int = 0
    needs_human_review: int = 0


class ManagerScorecard(BaseModel):
    """Everything the Деньги/KPI screens need for one manager in a period."""

    manager: ManagerRef
    period: str = Field(description="YYYY-MM")
    okk: OkkScore
    calls_scored: int
    zone_distribution: dict[str, int]
    meetings: MeetingsScore = Field(
        default_factory=MeetingsScore,
        description=(
            "Scored-meeting aggregate (встречи); distinct from the planned "
            "MoneyAxis.meetings deal counter"
        ),
    )
    money: MoneyAxis


class CallFeedItem(BaseModel):
    """One scored call in a manager's Звонки feed."""

    call_id: int
    bitrix_call_id: str
    started_at: datetime | None
    percent: float | None
    zone: str | None
    okk_5: int | None
    target_status: str | None
    client_name: str | None = Field(
        default=None,
        description="Client's name resolved from the linked Bitrix contact, when known",
    )
    phone: str | None = Field(
        default=None,
        description="Client's phone (fallback label when the contact has no name)",
    )
    sentiment_customer: str | None
    red_flags: list[str] = Field(default_factory=list)
    call_type: str | None
    is_qualification_call: bool
    summary: str
    bitrix_url: str | None = Field(
        default=None,
        description="Deep link to the call's CRM card in Bitrix24, when known",
    )


class CriterionFeedback(BaseModel):
    """One rubric criterion as scored on a call."""

    criterion_id: int
    block_name: str | None
    criterion_text: str | None
    score: float | None
    max: float | None
    percent_of_max: float | None
    justification: str | None
    evidence: str | None
    recommendation: str | None
    corrected: bool = Field(
        default=False,
        description="True when an accepted appeal awarded this criterion full marks",
    )


class TranscriptBlock(BaseModel):
    """One speaker-labeled block of the call transcript."""

    speaker: str
    text: str


class AppealCriterionInput(BaseModel):
    """One criterion a manager contests when filing an appeal."""

    criterion_id: int = Field(description="Номер критерия из чек-листа звонка")
    reason: str | None = Field(
        default=None,
        description="Почему менеджер не согласен с оценкой по этому критерию",
        max_length=2000,
    )


class AppealCriterionView(BaseModel):
    """A contested criterion enriched with its scored text for the review screen."""

    criterion_id: int
    block_name: str | None = None
    criterion_text: str | None = None
    original_score: float | None = Field(
        default=None,
        description="The LLM score for this criterion being appealed",
    )
    max: float | None = None
    reason: str | None = None
    confirmed: bool = Field(
        default=False,
        description="True once the head confirmed this criterion (full marks)",
    )


class AppealView(BaseModel):
    """A manager appeal against a call's ОКК score and its РОП verdict.

    ``override_percent``/``override_okk_5`` are populated once a head confirms at
    least one criterion (the recomputed total); until then (and on a verdict that
    confirms nothing) they are null and the original LLM score stands. The
    trailing context fields (``manager_name``/``started_at``/``original_percent``)
    let the head's review list render without a second round-trip.
    """

    id: int
    call_id: int
    manager_bitrix_user_id: int
    created_by_bitrix_user_id: int
    department_id: int | None = Field(default=None, description="Bitrix department id")
    disputed_criteria: list[AppealCriterionView] = Field(
        default_factory=list,
        description="The specific criteria the manager contests, enriched",
    )
    confirmed_criteria: list[int] = Field(
        default_factory=list,
        description="Criterion ids the head confirmed (awarded full marks)",
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="The call's red flags, so the review screen can offer to clear",
    )
    dismissed_flags: list[str] = Field(
        default_factory=list,
        description="Red flags the head cleared when accepting this appeal",
    )
    reason: str | None = None
    status: str = Field(description="pending | accepted | rejected")
    override_percent: float | None = Field(
        default=None,
        description="Recomputed 0–100 percent after confirmed criteria, when any",
    )
    override_okk_5: int | None = Field(
        default=None,
        description="ОКК 1–5 derived from override_percent",
    )
    head_note: str | None = None
    reviewed_by_bitrix_user_id: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime | None = None
    manager_name: str | None = None
    started_at: datetime | None = Field(
        default=None,
        description="The appealed call's start time",
    )
    original_percent: float | None = Field(
        default=None,
        description="The LLM percent being appealed (pre-override)",
    )


class AppealCreate(BaseModel):
    """File an appeal against a call's score (manager → РОП)."""

    disputed_criteria: list[AppealCriterionInput] = Field(
        default_factory=list,
        description="Критерии чек-листа, с которыми менеджер не согласен",
        max_length=100,
    )
    reason: str | None = Field(
        default=None,
        description="Обратная связь менеджера по звонку — почему не согласен с оценкой",
        max_length=2000,
    )


class AppealReview(BaseModel):
    """A head's verdict on an appeal: which contested criteria to confirm.

    Each confirmed criterion is awarded full marks and the call's total
    recalculates automatically. Confirming nothing is a rejection (the original
    LLM score stands). ``confirmed_criteria`` must be a subset of the appeal's
    disputed criteria — enforced in the service against the stored appeal.
    """

    confirmed_criteria: list[int] = Field(
        default_factory=list,
        description="Criterion ids to confirm (full marks); empty = reject",
        max_length=100,
    )
    dismissed_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Red flag strings to clear from the call (only applied when the "
            "appeal is accepted, i.e. at least one criterion is confirmed)"
        ),
        max_length=100,
    )
    note: str | None = Field(default=None, max_length=2000)


class CallFeedback(BaseModel):
    """Full авто-разбор за 90 сек for a single call."""

    call_id: int
    bitrix_call_id: str
    manager: ManagerRef
    started_at: datetime | None
    duration_sec: int | None
    language: str | None
    percent: float | None
    zone: str | None
    okk_5: int | None
    target_status: str | None
    sentiment_customer: str | None
    sentiment_agent: str | None
    client_category: str | None = Field(
        default=None,
        description=(
            "Manager-assigned lead qualification grade (A/B/C/X) from the Bitrix "
            "deal field «Квалификация клиента», when resolvable"
        ),
    )
    summary: str
    strengths: str
    growth_zone: str
    training_recommendation: str
    red_flags: list[str] = Field(default_factory=list)
    call_type: str | None
    is_qualification_call: bool
    bitrix_url: str | None = Field(
        default=None,
        description="Deep link to the call's CRM card in Bitrix24, when known",
    )
    criteria: list[CriterionFeedback] = Field(default_factory=list)
    transcript: list[TranscriptBlock] = Field(default_factory=list)
    appeal: AppealView | None = Field(
        default=None,
        description="The latest appeal on this call (status/verdict), if any",
    )


class MeetingFeedItem(BaseModel):
    """One scored ОП meeting in a manager's Встречи feed.

    Attributed to whoever uploaded the recording to the Disk folder
    (``manager.bitrix_user_id``). ``source`` distinguishes departments as more
    of them start dropping recordings ("op" = отдел продаж).
    """

    meeting_id: int
    bitrix_file_id: int
    source: str
    name: str
    meeting_at: datetime | None
    duration_sec: int | None
    percent: float | None
    passed: bool | None
    call_type: str | None
    manager_tone: str | None
    needs_human_review: bool
    red_flags: list[str] = Field(default_factory=list)
    summary: str


class MeetingCriterionFeedback(BaseModel):
    """One meeting-rubric criterion as scored (okk_meeting rubric shape)."""

    criterion_id: int
    block: str | None
    name: str | None
    score: float | None
    max: float | None
    auto: bool = False


class MeetingFeedback(BaseModel):
    """Full авто-разбор for one scored meeting."""

    meeting_id: int
    bitrix_file_id: int
    source: str
    name: str
    # None when the Disk upload carried no usable CREATED_BY (head-only view).
    manager: ManagerRef | None
    meeting_at: datetime | None
    duration_sec: int | None
    language: str | None
    rubric_version: str | None
    percent: float | None
    passed: bool | None
    call_type: str | None
    manager_tone: str | None
    needs_human_review: bool
    script_adherence: float | None = None
    script_deviations: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    summary: str
    criteria: list[MeetingCriterionFeedback] = Field(default_factory=list)


class FeedItem(BaseModel):
    """One kind-tagged entry in the unified Звонки+Встречи feed.

    Exactly one of ``call``/``meeting`` is set, per ``kind``. ``at`` is the
    merge key (call ``started_at`` / ``meeting_at``).
    """

    kind: str = Field(description="call | meeting")
    at: datetime | None
    call: CallFeedItem | None = None
    meeting: MeetingFeedItem | None = None


class RubricCriterionView(BaseModel):
    """One criterion of an active rubric, normalized across rubric shapes."""

    criterion_id: int
    block: str | None
    name: str
    max: float


class RubricView(BaseModel):
    """The active criteria set for one source ("tm" calls / "op" meetings)."""

    source: str
    version: str
    name: str | None
    max_total: float
    criteria: list[RubricCriterionView] = Field(default_factory=list)


class TeamGroupStats(BaseModel):
    """Aggregate over a department in a period (РОП-вид header)."""

    calls_scored: int
    okk: OkkScore
    zone_distribution: dict[str, int]
    meetings: MeetingsScore = Field(default_factory=MeetingsScore)
    money: MoneyAxis = Field(
        default_factory=MoneyAxis,
        description=(
            "Group conversion axis; ``meetings`` is the team's total "
            "conversions to «Фактический визит» (TM department only)"
        ),
    )


class TeamSummary(BaseModel):
    """РОП-вид: per-manager roster + group rollup for a department."""

    department: DepartmentRef
    period: str
    group: TeamGroupStats
    roster: list[ManagerScorecard]


class DayActionItem(BaseModel):
    """One "кому звонить" item — an open TM-funnel deal with a stage-derived reason.

    Identity (client name + phone) is exposed deliberately: this is a manager's
    OWN client, which is normal CRM use (unlike the anonymized call-QA feed).
    """

    deal_id: int
    client_name: str | None = None
    phone: str | None = None
    stage_id: str
    reason: str = Field(description="Plain-language next action from the deal stage")
    heat: str = Field(description="hot | warm | cool — visual urgency")
    queue: str | None = Field(
        default=None,
        description=(
            "Which Мой день queue this deal falls in: no_answer | meetings | "
            "cooling (same buckets as DayStats), or null for a neutral deal"
        ),
    )
    no_task: bool = Field(
        default=False,
        description=(
            "True when the deal has no open (incomplete) activity — a «брошенная» "
            "card without a next task, orthogonal to ``queue``"
        ),
    )
    last_activity_at: datetime | None = None
    bitrix_url: str | None = Field(
        default=None,
        description="Deep link to the deal's CRM card in Bitrix24, when known",
    )


class DayTaskItem(BaseModel):
    """One overdue-task example for the «Просроченные задачи» queue."""

    activity_id: int
    subject: str
    deadline: datetime | None = None
    bitrix_url: str | None = Field(
        default=None,
        description="Deep link to the task's CRM entity (deal/contact/lead)",
    )


class DayStats(BaseModel):
    """The Мой день counters (open-pipeline snapshot)."""

    meetings: int = Field(description="Open deals at a booked/confirmed-visit stage")
    no_answer: int = Field(description="Open deals parked at a Недозвон stage")
    cooling: int = Field(description="Open deals going stale / no-show, need a nudge")
    no_task: int | None = Field(
        default=None,
        description=(
            "Open deals with no open (incomplete) activity — «брошенные» cards "
            "without a next task; null when the activity read could not be made"
        ),
    )


class DayToday(BaseModel):
    """«Важные цифры дня» — today-scoped headline numbers for the Мой день card.

    Each field is None when its source could not be read (the UI shows "—"),
    never a misleading zero. Every count is for *today* in the report timezone.
    """

    planned_calls: int | None = Field(
        default=None,
        description="Записано на сегодня — open (not-done) call activities due today",
    )
    meetings_set: int | None = Field(
        default=None,
        description="Назначено сегодня — deals booked to the meeting stage today",
    )
    talk_time_sec: int | None = Field(
        default=None,
        description="Время на линии — total answered-call talk seconds today",
    )
    push_to_meeting: int | None = Field(
        default=None,
        description="Дожать до встречи — deals entering a hot stage today",
    )
    in_qual: int | None = Field(
        default=None,
        description=(
            "Дожать до встречи — open deals sitting at the qualified stage now "
            "(clients «в квале»), a current-pipeline snapshot, not a today count"
        ),
    )
    deals_closed: int | None = Field(
        default=None,
        description="Дел закрыто — conducted-visit (WON) deals attributed today",
    )
    overdue: int | None = Field(
        default=None,
        description="Просроченных — tasks due today that are already overdue",
    )


class AuditFailedItem(BaseModel):
    """One closed-lost deal whose stated отказ-причина contradicted the actual call.

    Populated from OKK's ``audit_verdicts`` (verdict = ``contradicted``) — a
    self-fix nudge in «Займись сейчас»: the manager reviews the deal and corrects
    the close reason. Only the manager's OWN deals appear (normal CRM use).
    """

    deal_id: int
    client_name: str | None = None
    close_reason: str | None = Field(
        default=None,
        description="The manager-stated close reason the call did not support",
    )
    justification: str | None = Field(
        default=None,
        description="Why the call contradicts the stated reason (LLM, in Russian)",
    )
    evidence_quote: str | None = None
    confidence: float | None = None
    audited_at: datetime | None = None
    bitrix_url: str | None = Field(
        default=None,
        description="Deep link to the deal's CRM card in Bitrix24, when known",
    )


class DayView(BaseModel):
    """Everything the Мой день screen needs for one manager, live from Bitrix.

    ``data_ready`` is False when the manager has no live TM pipeline (the Bitrix
    data-cleanup gate not yet cleared) — the companion then shows an honest
    "данные готовятся" state instead of empty/zero widgets.
    """

    manager: ManagerRef
    period: str
    data_ready: bool
    actions: list[DayActionItem] = Field(default_factory=list)
    stats: DayStats
    money: MoneyAxis
    today: DayToday = Field(default_factory=DayToday)
    overdue_tasks: list[DayTaskItem] = Field(
        default_factory=list,
        description="A few example overdue tasks for the «Просроченные задачи» queue",
    )
    audit_failed: list[AuditFailedItem] = Field(
        default_factory=list,
        description="Closed-lost deals whose stated reason contradicted the call",
    )


class OverdueTaskItem(BaseModel):
    """One overdue CRM task (incomplete activity past its deadline), team-wide.

    Unlike :class:`DayTaskItem` this carries the responsible ``manager`` — the
    РОП view lists tasks across the whole team, so each row must say whose it is.
    """

    activity_id: int
    subject: str
    deadline: datetime | None = None
    manager: ManagerRef
    bitrix_url: str | None = Field(
        default=None,
        description="Deep link to the task's CRM entity (deal/contact/lead)",
    )


class TeamOverdueTasks(BaseModel):
    """РОП-вид: every overdue task of a department's team, oldest-due first.

    All incomplete activities whose deadline has already passed (просроченные до
    сегодня), across the team, ordered by deadline ascending. ``truncated`` is
    True when more matched than the cap and the tail was dropped.
    """

    department: DepartmentRef
    total: int = Field(description="Number of overdue tasks returned (after the cap)")
    truncated: bool = Field(
        default=False,
        description="True when more overdue tasks matched than the returned cap",
    )
    tasks: list[OverdueTaskItem] = Field(default_factory=list)


# --- Моя Аналитика (/analytics) ---------------------------------------------
# Period analytics for one manager, live from Bitrix (stage history + activities
# + telephony). Every block carries its own ``status`` so the cabinet can badge
# each block live/empty independently; fields are None when their source could
# not be read (UI shows "—"), never a misleading zero. See web/api/v1/analytics.py.


class FunnelReason(BaseModel):
    """One sub-reason of a funnel stage — e.g. why a deal was closed as «отказ».

    ``label`` is the human reason (resolved from the Bitrix enum field), ``count``
    its deals in the period, ``reason_id`` the Bitrix enum value id (None for the
    «не указана» bucket / unresolved values).
    """

    label: str
    count: int
    reason_id: str | None = Field(
        default=None,
        description="Bitrix enum value id, when resolved",
    )


class FunnelStage(BaseModel):
    """One stage of the conversion funnel with its period count."""

    key: str = Field(
        description="Stable key: leads|qualified|meeting_set|arrived|bought",
    )
    label: str
    count: int | None = None
    breakdown: list[FunnelReason] | None = Field(
        default=None,
        description=(
            "Sub-breakdown of this stage's count, largest first. Set for "
            "«closed_lost» (by отказ-причина) when the reason field is configured."
        ),
    )


class AnalyticsTrendPoint(BaseModel):
    """One month of the conversion-rate trend (arrived ÷ leads)."""

    period: str = Field(description="YYYY-MM")
    cr_pct: float | None = None


class AnalyticsFunnel(BaseModel):
    """Анализ воронки — stage counts + overall CR + monthly CR trend.

    ``bought`` («купили») follows the deal into the sales funnel: after the visit
    the TM deal moves to cat 2 and is reassigned to the closer but keeps «Сотрудник
    ТМ», so a booking signed (C2:WON) is attributed back to the TM via stage
    history — the same join as «Фактический визит» (``arrived``).
    """

    status: str = Field(
        default="not_available",
        description="'live' when the period has any leads/stage activity",
    )
    stages: list[FunnelStage] = Field(default_factory=list)
    overall_cr_pct: float | None = Field(
        default=None,
        description="Итоговый CR — arrived (Фактический визит) ÷ leads, %",
    )
    trend: list[AnalyticsTrendPoint] = Field(
        default_factory=list,
        description="Trailing-N-months CR, oldest→newest",
    )


class AnalyticsTasks(BaseModel):
    """Анализ задач — activity counts for the period (crm.activity, by deadline).

    ``closed_on_time`` stays None for now: distinguishing on-time vs late closes
    needs a per-activity completion timestamp the count API does not expose.
    """

    status: str = Field(default="not_available")
    total: int | None = None
    closed: int | None = None
    closed_on_time: int | None = None
    overdue: int | None = None
    pending: int | None = Field(
        default=None,
        description="Открыто — open activities not yet past their deadline",
    )


class AnalyticsMeetings(BaseModel):
    """Анализ встреч — назначено / дошли / переназначились / недошли / купили.

    ``rescheduled`` counts deals that entered the meeting-set stage 2+ times in
    the period (a re-booking). ``bought`` is the cat-2 booking-signed count
    attributed to the TM (see AnalyticsFunnel).
    """

    status: str = Field(default="not_available")
    meetings_set: int | None = None
    arrived: int | None = None
    rescheduled: int | None = None
    no_show: int | None = None
    bought: int | None = None


class AnalyticsCalls(BaseModel):
    """Анализ звонков — telephony stats for the period (voximplant.statistic.get)."""

    status: str = Field(default="not_available")
    talk_time_sec: int | None = None
    completed: int | None = None
    no_answer: int | None = None
    incoming: int | None = None


class AnalyticsView(BaseModel):
    """Everything the Моя Аналитика screen needs for one manager in a period."""

    manager: ManagerRef
    period: str = Field(description="YYYY-MM")
    funnel: AnalyticsFunnel = Field(default_factory=AnalyticsFunnel)
    tasks: AnalyticsTasks = Field(default_factory=AnalyticsTasks)
    meetings: AnalyticsMeetings = Field(default_factory=AnalyticsMeetings)
    calls: AnalyticsCalls = Field(default_factory=AnalyticsCalls)


class HygieneCriterion(BaseModel):
    """One CRM-hygiene criterion for a manager in a period.

    ``pct`` is ``numerator / denominator`` (share of cards/tasks in good standing);
    the cabinet colours it against the norm. ``status`` is ``"live"`` when the
    criterion could be computed from Bitrix and ``"not_available"`` when its data
    source is not wired (e.g. the анкета field list is unconfigured) — the cabinet
    badges it «нет данных» rather than showing a fake number. ``note`` carries a
    short caveat or the reason it is unavailable.
    """

    key: str = Field(
        description="statuses|anketa|tasks_set|tasks_on_time|notes",
    )
    status: str = Field(default="not_available", description="'live' | 'not_available'")
    pct: float | None = None
    numerator: int | None = Field(
        default=None,
        description="Cards/tasks in good standing (the share's top)",
    )
    denominator: int | None = Field(
        default=None,
        description="Cards/tasks considered (the share's base)",
    )
    note: str | None = None


class HygieneView(BaseModel):
    """ОКК · Гигиена CRM — five discipline criteria for one manager in a period.

    ``overall_pct`` is the mean of the live criteria (None when none are live).
    ``norm_pct`` is the per-criterion target the cabinet draws the threshold at.
    """

    manager: ManagerRef
    period: str = Field(description="YYYY-MM")
    norm_pct: int = 85
    overall_pct: float | None = None
    criteria: list[HygieneCriterion] = Field(default_factory=list)


class CriterionAverage(BaseModel):
    """Average score of one rubric criterion across a manager's calls in a period."""

    criterion_id: int
    block_name: str | None = None
    criterion_text: str | None = None
    avg_score: float | None = None
    avg_pct_of_max: float | None = None
    max: float | None = None
    count: int = 0


class CriteriaAveragesView(BaseModel):
    """Балл ОКК: per-criterion averages over целевые qualification calls."""

    manager: ManagerRef
    period: str
    calls_scored: int = 0
    criteria: list[CriterionAverage] = Field(default_factory=list)


class ScoreTrendPoint(BaseModel):
    """Average ОКК percent for one time bucket (day / week / month)."""

    bucket: str = Field(description="ISO date of the bucket start (local tz)")
    avg_percent: float | None = None
    calls: int = 0


class ScoreTrendView(BaseModel):
    """Динамика: average ОКК percent per bucket for the trailing window."""

    manager: ManagerRef
    bucket: str = Field(description="day | week | month")
    points: list[ScoreTrendPoint] = Field(default_factory=list)
