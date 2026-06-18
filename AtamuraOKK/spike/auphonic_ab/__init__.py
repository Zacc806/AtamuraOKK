"""Auphonic A/B spike: measure whether audio cleanup improves transcription.

Self-contained experiment — pulls recordings from object storage, transcribes
them with the production Yandex async path, runs each through Auphonic
(leveler/denoise/filter), re-transcribes the cleaned audio, and exports a
before/after comparison. It **never** writes to the prod ``calls``/``transcripts``
tables; all state lives under ``.auphonic_ab/``.
"""
