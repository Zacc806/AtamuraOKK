# Phase 2 — Transcription

Turns downloaded recordings into speaker-labeled transcripts.

```
DOWNLOADED call → pull audio from object storage
  → ffprobe channel count
  → stereo: split agent (ch0) + customer (ch1); mono: downmix → 16 kHz mono WAV
  → transcribe each channel with the configured provider
  → speaker-labeled blocks → detect language (RU/KK from text)
  → persist transcript, status TRANSCRIBED
  → KK only parked at PENDING_KK if the engine can't do Kazakh (whisper/openai)
```

## Provider (current: Yandex SpeechKit)

The worker depends only on the `AsyncTranscriber` interface; `factory.py` picks the
impl from `transcribe_provider`:

| `transcribe_provider` | Engine | Kazakh | Notes |
|---|---|---|---|
| `yandex` *(default)* | SpeechKit v3 streaming gRPC | ✅ | RU **and** KK; remote API |
| `whisper` | local faster-whisper large-v3 | ❌ parks | no API quota; CPU fallback |
| `openai` | gpt-4o-transcribe | ❌ parks | text only |

Each provider declares `handles_kazakh`; the worker parks `kk` calls at
`PENDING_KK` only when the engine can't handle them. SpeechKit can, so `kk` calls
now advance to `TRANSCRIBED`.

### SpeechKit specifics
- **v3 *streaming* gRPC** (`RecognizeStreaming`), one mono channel at a time, with
  `audio_processing_type=FULL_DATA` (recognize the whole file, return finals once),
  text-normalization on, languages whitelisted to `ru-RU`,`kk-KZ`. Streaming is
  used (not the async/long-running API) because it accepts raw audio inline — **no
  Yandex Object Storage bucket required**; the 16 kHz mono WAV is read as raw LPCM
  and streamed straight from disk.
- **Region matters.** SA/key/endpoints must all be in the same Yandex Cloud
  installation:
  - **Global** (SA ids start with `aje…`): `stt.api.cloud.yandex.net:443`,
    IAM `https://iam.api.cloud.yandex.net/iam/v1/tokens`.
  - **Kazakhstan** (`b2b…`/`ao7…`): `stt.api.yandexcloud.kz:443`,
    IAM `https://iam.api.yandexcloud.kz/iam/v1/tokens`.
  A key from one installation is `Unknown api key` on the other endpoint.
- **Auth = IAM token (Bearer)**, minted from a service-account **authorized-key
  JSON** (`yandex_iam.py`: PS256 JWT → IAM token, cached ~11 h). This carries the
  SA's full role set with **no scope restriction**. API-key (`Api-Key`) auth is
  also supported via `yandex_secret_key`, **but scoped API keys fail with a
  misleading folder-level `PERMISSION_DENIED`** — prefer the authorized key.
- **Prerequisites on the SA / cloud:** the SA needs role `ai.speechKit-stt.user`
  on the folder (or cloud), and the **cloud must have active billing** — an
  unbilled cloud returns the same generic `resource-manager … denied` regardless
  of roles.

## Configuration
```bash
ATAMURAOKK_TRANSCRIBE_PROVIDER=yandex
# Authorized-key JSON (relative paths resolve against the repo root). gitignored.
ATAMURAOKK_YANDEX_SA_KEY_FILE=authorized_key.json
# Global region (use the *.yandexcloud.kz hosts for the KZ installation):
ATAMURAOKK_YANDEX_STT_ENDPOINT=stt.api.cloud.yandex.net:443
ATAMURAOKK_YANDEX_IAM_ENDPOINT=https://iam.api.cloud.yandex.net/iam/v1/tokens
# Optional: ATAMURAOKK_YANDEX_STT_MODEL=general, ATAMURAOKK_YANDEX_STT_NORMALIZE=true
```
Dependencies are in the `yandex` group (`uv sync --group yandex`): `grpcio`,
`yandexcloud` (ships the v3 stubs), `pyjwt[crypto]`.

> The authorized-key JSON holds the SA private key — keep it gitignored; mount or
> copy it into the container for production rather than committing it.

## Components
- `transcription/base.py` — `AsyncTranscriber` interface + `TranscriptResult`/`Segment`.
- `transcription/yandex_provider.py` — `YandexSpeechKitTranscriber` (v3 streaming).
- `transcription/yandex_iam.py` — authorized-key → cached IAM token.
- `transcription/whisper.py` / `openai_provider.py` — alternate engines.
- `transcription/language.py` — RU/KK detection from text.
- `transcription/worker.py` — `transcribe_pending` / `transcribe_one`;
  `requeue_pending_kk` reverts parked Kazakh calls.
- `audio.py` — ffprobe/ffmpeg channel split + 16 kHz mono downmix.

## Run
```bash
make transcribe                                   # batch over DOWNLOADED
python -m AtamuraOKK.transcription run --limit 50 --concurrency 3
python -m AtamuraOKK.transcription requeue-kk     # PENDING_KK -> DOWNLOADED (re-transcribe with kk)
```
Requires `ffmpeg` and the Yandex config above.

## Scope
Only calls **≥ `ingest_min_duration_sec` (90 s)** are analyzable — shorter calls
have too little conversation to score. The filter is applied at ingestion
(`ingestion/service.py`); raising it does not retroactively skip already-ingested
calls.

## Throughput / throttling
SpeechKit throttles an account after a burst (latency injected, not errors): an
isolated call seen at ~1–2 s can climb to ~17 s, and **higher concurrency makes it
worse**. Keep `--concurrency` low (2–3) for backlogs; for fast bulk runs raise the
SpeechKit RPS/units quota in the Yandex console first. Work is idempotent and
commits per call, so a throttled run is safe to leave running or resume later.

## Validated live
SpeechKit transcribed real RU **and** KK calls cleanly (model `yandex/general`),
including Kazakh openings (`алло сәлеметсіз бе …`) that the whisper path could not
handle — those calls previously sat in `PENDING_KK` and now reach `TRANSCRIBED`.
