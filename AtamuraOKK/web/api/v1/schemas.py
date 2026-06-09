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

from pydantic import BaseModel, Field


class ManagerRef(BaseModel):
    """Identity of a manager, keyed by Bitrix user id."""

    bitrix_user_id: int
    name: str | None = None
    department_id: int | None = Field(
        default=None,
        description="Bitrix department id",
    )
    department_name: str | None = None


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
    """The conversion/Положение 'money' axis — not wired in Phase 1.

    Shape is published now so the companion can code against it; every value is
    null until the Bitrix deal/lead ingestion lands (and is trustworthy only
    after the Bitrix data-cleanup gate). ``status`` says why.
    """

    status: str = Field(
        default="not_available",
        description="not_available until the deal/conversion ingestion ships",
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


class ManagerScorecard(BaseModel):
    """Everything the Деньги/KPI screens need for one manager in a period."""

    manager: ManagerRef
    period: str = Field(description="YYYY-MM")
    okk: OkkScore
    calls_scored: int
    zone_distribution: dict[str, int]
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
    criteria: list[CriterionFeedback] = Field(default_factory=list)


class TeamGroupStats(BaseModel):
    """Aggregate over a department in a period (РОП-вид header)."""

    calls_scored: int
    okk: OkkScore
    zone_distribution: dict[str, int]


class DepartmentRef(BaseModel):
    """Identity of a department, keyed by Bitrix department id."""

    bitrix_id: int
    name: str | None = None


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
