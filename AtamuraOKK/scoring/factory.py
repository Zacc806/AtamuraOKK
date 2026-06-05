"""Select the scoring engine from settings.

Keeps the scoring worker provider-agnostic: it asks for a :class:`Scorer` and
never names a concrete LLM vendor.
"""

from __future__ import annotations

from loguru import logger

from AtamuraOKK.scoring.base import Scorer
from AtamuraOKK.settings import settings


def get_scorer() -> Scorer:
    """Return the configured scorer ("anthropic" Claude, or "openai")."""
    provider = settings.scoring_provider.lower()
    if provider == "anthropic":
        from AtamuraOKK.scoring.anthropic_scorer import AnthropicScorer  # noqa: PLC0415

        logger.info("Scorer: Anthropic {m}", m=settings.anthropic_scoring_model)
        return AnthropicScorer()
    if provider == "openai":
        from AtamuraOKK.scoring.openai_scorer import OpenAIScorer  # noqa: PLC0415

        logger.info("Scorer: OpenAI {m}", m=settings.openai_scoring_model)
        return OpenAIScorer()
    msg = f"Unknown scoring_provider {provider!r} (use 'anthropic' or 'openai')."
    raise ValueError(msg)
