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
    last_activity_at: datetime | None = None
    bitrix_url: str | None = Field(
        default=None,
        description="Deep link to the deal's CRM card in Bitrix24, when known",
    )


class DayStats(BaseModel):
    """The three Мой день counters (open-pipeline snapshot)."""

    meetings: int = Field(description="Open deals at a booked/confirmed-visit stage")
    no_answer: int = Field(description="Open deals parked at a Недозвон stage")
    cooling: int = Field(description="Open deals going stale / no-show, need a nudge")


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
