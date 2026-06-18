"""Shared, dependency-light glossary for transcript entity correction.

Both pipelines (the call pipeline ``transcription/`` and the ОП meetings pipeline
``scoring/meetings/``) import from here so the canonical list of residential
complexes (ЖК) and their addresses lives in exactly one place and the two cannot
drift. This package never imports ``AtamuraOKK.settings`` (so the meetings
pipeline stays decoupled) and pulls in no heavy deps — only the ``anthropic`` SDK
both pipelines already use, and that lazily inside the correction call.
"""
