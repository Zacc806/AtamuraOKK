"""Tests for the OKK meeting-checklist xlsx loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from AtamuraOKK.calibration.xlsx_loader import load_human_calls

openpyxl = pytest.importorskip("openpyxl")

# The real business workbook (gitignored); used for an extra local-only check.
_REAL_XLSX = Path("C:/AtamuraOKK/Чек лист встречи ОП - Январь.xlsx")


def _build_synthetic(path: Path) -> None:
    """Write a minimal workbook mirroring the real sheet layout."""
    workbook = openpyxl.Workbook()
    summary = workbook.active
    summary.title = "Сводная"
    summary["A1"] = "should be skipped"

    sheet = workbook.create_sheet("Толегенова")
    sheet.cell(row=1, column=5, value="Альбина")
    sheet.cell(row=4, column=5, value="https://amanat.bitrix24.kz/crm/deal/details/436100/")
    sheet.cell(row=27, column=5, value=21)
    # criterion numbers in column B (row 14 is an unnumbered sub-note)
    numbers = [*range(1, 9), None, *range(9, 21)]
    for offset, number in enumerate(numbers):
        row = 6 + offset
        if number is not None:
            sheet.cell(row=row, column=2, value=number)
    # scores for this call sit one column right of the base (col F = 6)
    sheet.cell(row=6, column=6, value=1)  # criterion 1
    sheet.cell(row=9, column=6, value=5)  # criterion 4 (row 9)
    workbook.save(path)


def test_loads_synthetic_workbook(tmp_path: Path) -> None:
    """The loader extracts a scored call and skips the summary sheet."""
    xlsx = tmp_path / "checklist.xlsx"
    _build_synthetic(xlsx)

    calls = load_human_calls(xlsx)

    assert len(calls) == 1
    call = calls[0]
    assert call.manager == "Толегенова"
    assert call.crm_deal_id == 436100
    assert call.raw_total == 21
    assert call.reviewer == "Альбина"
    assert call.per_criterion[1] == 1
    assert call.per_criterion[4] == 5


@pytest.mark.skipif(not _REAL_XLSX.exists(), reason="business xlsx not present")
def test_loads_real_workbook() -> None:
    """Against the real workbook: many calls, valid totals, parsed deal ids."""
    calls = load_human_calls(_REAL_XLSX)
    assert len(calls) > 10
    assert any(c.crm_deal_id is not None for c in calls)
    assert all(c.raw_total is None or 0 <= c.raw_total <= 50 for c in calls)
