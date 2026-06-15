"""Unit tests for the score assembly logic (no DB / no LLM)."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.worker import _assemble

_OBJECTIONS_MAX = 21  # max of the objection criterion in tm-call-v2
_CLOSING_MAX = 37  # full max of the «Закрытие на КЭВ» criterion (category A)
_CLOSING_B_MAX = 18  # reduced max for category B


def _call_score(rubric: Rubric, *, objections_present: bool) -> CallScore:
    """A CallScore that awards full marks to every transcript-scored criterion."""
    return CallScore(
        call_type="квалификация",
        is_qualification_call=True,
        manager_identified=True,
        criteria=[
            CriterionScore(
                id=c.id,
                score=c.max,
                justification="ok",
                evidence="",
                recommendation="-",
            )
            for c in rubric.scored_criteria
        ],
        objections_present=objections_present,
        sentiment_customer="нейтральный",
        sentiment_agent="нейтральный",
        summary="тест",
        red_flags=[],
        target_status="неясно",
        strengths="-",
        growth_zone="-",
        training_recommendation="-",
    )


def test_objections_excluded_when_absent() -> None:
    """No objections occurred -> block drops out of numerator and denominator."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=False), rubric)

    assert "objections" not in payload["blocks"]
    assert all(c["block_id"] != "objections" for c in payload["per_criterion"])
    assert payload["max_points"] == rubric.max_conversational - _OBJECTIONS_MAX
    assert payload["raw_points"] == payload["max_points"]
    assert payload["percent"] == 100.0


def test_objections_scored_when_present() -> None:
    """Objections occurred -> block is scored against the full conversational max."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric)

    assert "objections" in payload["blocks"]
    assert payload["max_points"] == rubric.max_conversational
    assert payload["percent"] == 100.0


def test_five_criteria_with_recommendation() -> None:
    """tm-call-v2 collapses to 5 criteria, each carrying a recommendation."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric)

    assert len(payload["per_criterion"]) == 5
    assert {c["block_id"] for c in payload["per_criterion"]} == {
        "greeting",
        "needs",
        "presentation",
        "closing",
        "objections",
    }
    assert all("recommendation" in c for c in payload["per_criterion"])


def test_category_a_full_weight() -> None:
    """Category A keeps the full closing weight — identical to the untagged case."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric, "A")

    assert payload["client_category"] == "A"
    assert payload["blocks"]["closing"]["max"] == _CLOSING_MAX
    assert payload["max_points"] == rubric.max_conversational
    assert payload["percent"] == 100.0


def test_category_none_defaults_to_full() -> None:
    """No category (NULL) behaves exactly like category A."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric, None)

    assert payload["client_category"] is None
    assert payload["blocks"]["closing"]["max"] == _CLOSING_MAX
    assert payload["max_points"] == rubric.max_conversational


def test_category_x_defaults_to_full() -> None:
    """Category X (failed conversation) keeps full weight (no override mapped)."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric, "X")

    assert payload["blocks"]["closing"]["max"] == _CLOSING_MAX
    assert payload["max_points"] == rubric.max_conversational


def test_category_b_reduces_closing() -> None:
    """Category B halves the closing weight in numerator and denominator."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric, "B")

    assert payload["blocks"]["closing"]["max"] == _CLOSING_B_MAX
    # full-marks input is clamped to the reduced max
    assert payload["blocks"]["closing"]["score"] == _CLOSING_B_MAX
    assert payload["max_points"] == (
        rubric.max_conversational - _CLOSING_MAX + _CLOSING_B_MAX
    )
    assert payload["percent"] == 100.0
    closing = next(c for c in payload["per_criterion"] if c["block_id"] == "closing")
    assert closing["max"] == _CLOSING_B_MAX


def test_category_c_excludes_closing() -> None:
    """Category C drops the closing block from numerator and denominator."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=True), rubric, "C")

    assert "closing" not in payload["blocks"]
    assert all(c["block_id"] != "closing" for c in payload["per_criterion"])
    assert payload["max_points"] == rubric.max_conversational - _CLOSING_MAX


def test_category_c_omitting_closing_does_not_fail() -> None:
    """C excludes closing *before* the missing-criterion guard — no ValueError."""
    rubric = load_rubric()
    result = _call_score(rubric, objections_present=True)
    result.criteria = [c for c in result.criteria if c.id != 4]  # model omits closing

    payload = _assemble(result, rubric, "C")

    assert "closing" not in payload["blocks"]
    assert payload["max_points"] == rubric.max_conversational - _CLOSING_MAX


def test_max_for_category_weights() -> None:
    """Rubric.max_for resolves the per-category closing weight (and None = exclude)."""
    rubric = load_rubric()
    closing = next(c for c in rubric.scored_criteria if c.block_id == "closing")
    assert rubric.max_for(closing, "A") == _CLOSING_MAX
    assert rubric.max_for(closing, "B") == _CLOSING_B_MAX
    assert rubric.max_for(closing, "C") is None
    assert rubric.max_for(closing, None) == _CLOSING_MAX
    assert rubric.max_for(closing, "X") == _CLOSING_MAX

    greeting = next(c for c in rubric.scored_criteria if c.block_id == "greeting")
    assert rubric.max_for(greeting, "B") == greeting.max  # block has no override
