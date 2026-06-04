# Phase 2 — Transcription

Turns downloaded recordings into speaker-labeled transcripts.

```
DOWNLOADED call → pull audio from object storage
  → ffprobe channel count
  → stereo: split agent (ch0) + customer (ch1); mono: downmix
  → gpt-4o-transcribe each channel  (OpenAI, best-accuracy text)
  → speaker-labeled blocks → detect language (RU/KK from text)
  → RU: persist transcript, status TRANSCRIBED
  → KK: park at PENDING_KK (no Kazakh STT provider yet; transcript not stored)
```

## Decisions
- **Provider:** OpenAI **`gpt-4o-transcribe`** behind the `Transcriber` interface.
  It returns text only (no timestamps, no language field), so:
  - **Transcript form = speaker blocks** (`[AGENT] … / [CUSTOMER] …`), not
    turn-by-turn. One API call per channel (2 per stereo call). Chosen for best
    accuracy + simplicity; can upgrade to interleaved (VAD-segmented) later.
  - **Language is detected from the text** — `transcription/language.py` flags
    Kazakh by its Cyrillic-only letters (ә ғ қ ң ө ұ ү һ і). No extra deps.
- **Kazakh is parked**, not transcribed, until a Kazakh-capable provider exists:
  status `PENDING_KK`, `call.language='kk'`, no transcript stored. When a provider
  is added, re-run the worker over `PENDING_KK` calls.

## Components
- `transcription/base.py` — `Transcriber` interface + `TranscriptResult`/`Segment`.
- `transcription/openai_provider.py` — `OpenAITranscriber` (gpt-4o-transcribe).
- `transcription/language.py` — RU/KK detection.
- `transcription/worker.py` — `transcribe_pending`: DOWNLOADED → TRANSCRIBED/PENDING_KK.
- `audio.py` — ffprobe/ffmpeg channel split + mono downmix.
- `transcription/whisper.py` — self-hosted faster-whisper impl (Phase 0 / future
  Kazakh / fallback), unused in the default path.

## Run
```bash
make transcribe        # transcribe analyzable DOWNLOADED calls
# or: python -m AtamuraOKK.transcription run --limit 50
```
Requires `ATAMURAOKK_OPENAI_API_KEY` and `ffmpeg`.

## Validated live
15 calls: **13 Russian transcribed** (clean, fluent — clearly better than the
Phase 0 faster-whisper output), **2 Kazakh detected and parked** (`PENDING_KK`),
0 failed. Mono and stereo recordings both handled.

## Cost
gpt-4o-transcribe ≈ $0.006/audio-minute. At ~200 analyzable calls/day × ~3 min ≈
600 min/day ≈ **$3.6/day** (~$110/mo). Kazakh calls add a small wasted cost until
routed pre-transcription (currently detected post-transcription).
