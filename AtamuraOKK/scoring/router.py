"""Language-routed scorer: ru -> Groq, kk/shala -> Yandex, with provider fallback.

The single :class:`Scorer` seam the pipeline depends on. Swapping a provider is
a change to :func:`build_scorer` and nothing else.
"""

from __future__ import annotations

from loguru import logger

from AtamuraOKK.scoring.anthropic import AnthropicScorer
from AtamuraOKK.scoring.base import CallForScoring, Scorer, ScoreResult
from AtamuraOKK.scoring.errors import ProviderUnavailableError
from AtamuraOKK.scoring.groq import GroqScorer
from AtamuraOKK.scoring.language import route
from AtamuraOKK.scoring.rubric import Rubric, load_rubric
from AtamuraOKK.scoring.yandex import YandexScorer


class LanguageRoutedScorer:
    """Dispatch a call to the Russian or Kazakh scorer by detected language."""

    def __init__(
        self,
        *,
        ru: Scorer,
        kk: Scorer,
        confidence_threshold: float = 0.75,
    ) -> None:
        self._ru = ru
        self._kk = kk
        self._threshold = confidence_threshold

    async def score(self, call: CallForScoring) -> ScoreResult:
        """Route then score; fall back to the other provider if the first is down."""
        lang = route(call, confidence_threshold=self._threshold)
        primary, fallback = (
            (self._ru, self._kk) if lang == "ru" else (self._kk, self._ru)
        )
        primary_name = "groq" if lang == "ru" else "yandex"
        try:
            result = await primary.score(call)
        except ProviderUnavailableError:
            logger.warning(
                "scorer {p} unavailable for lang={lang}; falling back",
                p=primary_name,
                lang=lang,
            )
            result = await fallback.score(call)
            result.needs_human_review = True
            result.meta["fallback_from"] = primary_name
        result.language = lang
        return result


def build_scorer(rubric: Rubric | None = None) -> Scorer:
    """Build the configured default scorer (Anthropic, or Groq/Yandex routed)."""
    from AtamuraOKK.settings import settings  # noqa: PLC0415

    rb = rubric or load_rubric(settings.score_rubric_version)
    if settings.score_provider == "anthropic":
        return AnthropicScorer(
            rb,
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            max_retries=settings.score_max_retries,
            retry_base_delay=settings.score_retry_base_delay,
            max_transcript_chars=settings.score_max_transcript_chars,
            pass_threshold=settings.score_pass_threshold,
        )
    ru = GroqScorer(
        rb,
        api_key=settings.groq_api_key,
        model=settings.groq_scoring_model,
        max_retries=settings.score_max_retries,
        retry_base_delay=settings.score_retry_base_delay,
        max_transcript_chars=settings.score_max_transcript_chars,
        pass_threshold=settings.score_pass_threshold,
    )
    kk = YandexScorer(
        rb,
        api_key=settings.yandex_api_key,
        folder_id=settings.yandex_folder_id,
        model=settings.yandex_gpt_model,
        max_retries=settings.score_max_retries,
        retry_base_delay=settings.score_retry_base_delay,
        max_transcript_chars=settings.score_max_transcript_chars,
        pass_threshold=settings.score_pass_threshold,
    )
    return LanguageRoutedScorer(
        ru=ru,
        kk=kk,
        confidence_threshold=settings.score_lang_confidence,
    )
