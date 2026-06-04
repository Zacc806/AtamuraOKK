"""Scoring subsystem exceptions."""

from __future__ import annotations


class ScoringError(RuntimeError):
    """Base class for scoring failures."""


class MalformedOutputError(ScoringError):
    """The LLM returned output that could not be parsed/validated after retries."""


class ProviderUnavailableError(ScoringError):
    """A scoring provider (Groq/Yandex) was unreachable or rate-limited out."""
