"""Unit tests for the block-average score assembly (no DB / no LLM)."""

from __future__ import annotations

from AtamuraOKK.scoring.base import CallScore, CriterionScore
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.worker import _assemble

_N_BLOCKS = 8  # tm-call-v4 has 8 equal-weight blocks
_OBJECTIONS_IDS = {28, 29, 30, 31}


def _call_score(
    rubric: Rubric,
    *,
    objections_present: bool = True,
    scores: dict[int, int] | None = None,
    inapplicable: set[int] | None = None,
) -> CallScore:
    """A CallScore over every element; defaults to ДА=1 everywhere.

    ``scores`` overrides individual element verdicts (id -> 0/1); ``inapplicable``
    marks elements Н.П. (applicable=false).
    """
    scores = scores or {}
    inapplicable = inapplicable or set()
    return CallScore(
        call_type="квалификация",
        is_qualification_call=True,
        manager_identified=True,
        criteria=[
            CriterionScore(
                id=c.id,
                score=scores.get(c.id, 1),
                applicable=c.id not in inapplicable,
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


def test_all_yes_is_100() -> None:
    """Every element ДА -> every block 100% -> average 100%."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric), rubric)

    assert len(payload["blocks"]) == _N_BLOCKS
    assert payload["percent"] == 100.0
    assert payload["zone"] == "strong"
    assert all(b["percent"] == 100.0 for b in payload["blocks"].values())


def test_flat_single_miss_same_regardless_of_block() -> None:
    """Flat model: one НЕТ costs the same wherever it is (each element weighs 1).

    The per-block breakdown still differs (soft-skills 2/3 vs greeting 4/5), but the
    headline percent depends only on the total ДА ÷ applicable — 33/34 either way.
    """
    rubric = load_rubric()
    miss_small = _assemble(_call_score(rubric, scores={32: 0}), rubric)  # soft_skills
    miss_large = _assemble(_call_score(rubric, scores={1: 0}), rubric)  # greeting

    assert miss_small["percent"] == miss_large["percent"] == round(100.0 * 33 / 34, 2)
    # blocks[*].percent stays a display-only breakdown and does differ:
    assert miss_small["blocks"]["soft_skills"]["percent"] == 66.67
    assert miss_large["blocks"]["greeting"]["percent"] == 80.0


def test_flat_percent_over_applicable_only() -> None:
    """Percent = ДА ÷ applicable; Н.П. (objections absent) shrinks the denominator."""
    rubric = load_rubric()
    payload = _assemble(
        _call_score(rubric, objections_present=False, scores={1: 0}), rubric
    )

    # objections block (4 elements) drops -> 30 applicable, one miss -> 29/30
    assert payload["max_points"] == 30
    assert payload["raw_points"] == 29
    assert payload["percent"] == round(100.0 * 29 / 30, 2)


def test_objections_block_drops_when_absent() -> None:
    """No objections -> the whole block leaves the average (7 blocks, not 8)."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, objections_present=False), rubric)

    assert "objections" not in payload["blocks"]
    assert len(payload["blocks"]) == _N_BLOCKS - 1
    assert all(c["id"] not in _OBJECTIONS_IDS for c in payload["per_criterion"])
    assert payload["percent"] == 100.0


def test_element_na_shrinks_block_denominator() -> None:
    """A Н.П. element leaves its block's denominator (not scored as 0).

    Qualification block (15, 16, 17): mark the mortgage-only item 17 as Н.П. and
    fail item 16 -> the block is 1/2 = 50%, not 1/3.
    """
    rubric = load_rubric()
    payload = _assemble(
        _call_score(rubric, scores={16: 0}, inapplicable={17}),
        rubric,
    )

    qual = payload["blocks"]["qualification"]
    assert qual["max"] == 2  # 17 dropped
    assert qual["score"] == 1
    assert qual["percent"] == 50.0
    assert all(c["id"] != 17 for c in payload["per_criterion"])


def test_na_ignored_where_sheet_forbids_it() -> None:
    """applicable=false on a non-Н.П. element is ignored (element still scored)."""
    rubric = load_rubric()
    # id 1 (greeting «Поздоровался») has no Н.П. rule -> must stay in the block.
    payload = _assemble(_call_score(rubric, inapplicable={1}), rubric)

    assert payload["blocks"]["greeting"]["max"] == 5
    assert any(c["id"] == 1 for c in payload["per_criterion"])


def test_conditional_element_may_be_omitted() -> None:
    """A Н.П.-eligible element the model omits is treated as Н.П., not a failure."""
    rubric = load_rubric()
    result = _call_score(rubric)
    result.criteria = [c for c in result.criteria if c.id != 25]  # closing refusal item

    payload = _assemble(result, rubric)

    assert payload["blocks"]["closing"]["max"] == 5  # 25 dropped, 5 remain
    assert payload["percent"] == 100.0


def test_mandatory_omission_fails_the_call() -> None:
    """A mandatory element the model omits raises so the call retries."""
    rubric = load_rubric()
    result = _call_score(rubric)
    result.criteria = [c for c in result.criteria if c.id != 1]  # mandatory greeting

    try:
        _assemble(result, rubric)
    except ValueError as exc:
        assert "omitted criteria" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for a missing mandatory element")


def test_raw_and_max_points_are_flat_counts() -> None:
    """raw_points / max_points are ДА / applicable counts across all blocks."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric, scores={1: 0, 32: 0}), rubric)

    n_elements = len(rubric.scored_criteria)
    assert payload["max_points"] == n_elements  # nothing Н.П. here
    assert payload["raw_points"] == n_elements - 2


def test_category_recorded_but_not_weighted() -> None:
    """client_category is stored but no longer changes the math (v4 dropped it)."""
    rubric = load_rubric()
    a = _assemble(_call_score(rubric), rubric, "A")
    c = _assemble(_call_score(rubric), rubric, "C")

    assert a["client_category"] == "A"
    assert c["client_category"] == "C"
    assert a["percent"] == c["percent"] == 100.0
    assert "closing" in c["blocks"]  # not excluded for category C anymore


def test_sales_signals_in_payload() -> None:
    """payment_method / wants_to_visit / on_premises flow into the score payload."""
    rubric = load_rubric()
    result = _call_score(rubric)
    result.payment_method = "наличные"
    result.wants_to_visit = True
    result.on_premises = False

    payload = _assemble(result, rubric)

    assert payload["payment_method"] == "наличные"
    assert payload["wants_to_visit"] is True
    assert payload["on_premises"] is False


def test_sales_signals_default_when_omitted() -> None:
    """An LLM that omits the new fields gets safe defaults, not a validation error."""
    rubric = load_rubric()
    payload = _assemble(_call_score(rubric), rubric)

    assert payload["payment_method"] == "неизвестно"
    assert payload["wants_to_visit"] is False
    assert payload["on_premises"] is False
