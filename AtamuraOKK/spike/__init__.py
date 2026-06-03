"""Phase 0 transcription-quality spike.

A small staged CLI that validates the riskiest assumption before the pipeline
is built: Kazakh transcription quality. Run stages with::

    python -m AtamuraOKK.spike fetch
    python -m AtamuraOKK.spike download
    python -m AtamuraOKK.spike transcribe
    python -m AtamuraOKK.spike wer
"""
