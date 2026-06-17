"""Brand/name mis-hearing correction applied to transcripts post-STT."""

from __future__ import annotations

import pytest

from AtamuraOKK.transcription.glossary import correct_terms


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Компания атомра рада помочь", "Компания Атамура рада помочь"),
        ("звоню из тОмРа", "звоню из Атамура"),  # case-insensitive
        ("самурай групп", "Атамура групп"),
        ("уже верно: Атамура", "уже верно: Атамура"),  # no double-correction
        ("атмосфера и керуен", "атмосфера и керуен"),  # ЖК names untouched
    ],
)
def test_corrects_known_mis_hearings(raw: str, expected: str) -> None:
    """Known brand mis-hearings normalize to the canonical spelling."""
    assert correct_terms(raw) == expected


def test_only_whole_words_replaced() -> None:
    """A longer word merely containing a mis-heard form is left alone."""
    assert correct_terms("томрання") == "томрання"
