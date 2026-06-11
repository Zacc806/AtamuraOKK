"""Tests for the ЖК knowledge-base loader (ТЗ 2.1)."""

from __future__ import annotations

import json
from pathlib import Path

from AtamuraOKK.scoring.meetings.zhk import ZhkFacts, load_zhk_facts


def test_loads_real_files_skips_underscore_templates(tmp_path: Path) -> None:
    """``*.json`` load; ``_*.json`` (templates) are skipped."""
    (tmp_path / "aura.json").write_text(
        json.dumps({"name": "Аура", "has_elevator": False, "floors": 6}),
        encoding="utf-8",
    )
    (tmp_path / "_example.json").write_text(
        json.dumps({"name": "Шаблон"}),
        encoding="utf-8",
    )

    facts = load_zhk_facts(tmp_path)

    assert [f.name for f in facts] == ["Аура"]


def test_empty_or_missing_dir_returns_empty(tmp_path: Path) -> None:
    """No populated KB -> detector stays inert (empty list)."""
    assert load_zhk_facts(tmp_path) == []
    assert load_zhk_facts(tmp_path / "nope") == []


def test_render_flags_missing_elevator() -> None:
    """The fact sheet states the absence of an elevator explicitly."""
    sheet = ZhkFacts(name="Аура", has_elevator=False, floors=6).render()
    assert "Аура" in sheet
    assert "лифта НЕТ" in sheet
