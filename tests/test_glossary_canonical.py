"""The canonical ЖК/address glossary data and its rendered reference block."""

from __future__ import annotations

from AtamuraOKK.glossary.canonical import (
    COMPLEXES,
    TOPONYMS,
    build_reference_text,
)


def test_every_complex_has_name_and_address() -> None:
    """No complex may ship with a blank name or address."""
    assert COMPLEXES
    for c in COMPLEXES:
        assert c.zhk.strip()
        assert c.address.strip()


def test_atmosfera_pair_shares_address() -> None:
    """Атмосфера 2 is the second phase on the same site as Атмосфера."""
    by_name = {c.zhk: c.address for c in COMPLEXES}
    assert by_name["Атмосфера"] == by_name["Атмосфера 2"]


def test_reference_text_includes_all_names_and_toponyms() -> None:
    """A future edit dropping a name/toponym must fail this guard."""
    ref = build_reference_text()
    for c in COMPLEXES:
        assert c.zhk in ref
    for toponym in TOPONYMS:
        assert toponym in ref


def test_reference_text_is_deterministic() -> None:
    """The block must be byte-stable so the system prompt is cacheable."""
    assert build_reference_text() == build_reference_text()
