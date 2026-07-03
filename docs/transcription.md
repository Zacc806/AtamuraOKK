# Phase 2 — Transcription

Turns downloaded recordings into speaker-labeled transcripts.

```
DOWNLOADED call → pull audio from object storage → ffprobe channel count
  → async (default): send whole file; stereo separates by channel_tag,
    mono uses speaker labeling → interleave per-utterance turns by time
  → stream/whisper/openai: split ch0=agent + ch1=customer (mono: downmix)
  → time-ordered speaker-labeled blocks → detect language (RU/KK from text)
  → persist transcript (+ calls.is_stereo), status TRANSCRIBED
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
- **Mode (`yandex_stt_mode`).** Default **`async`** (`RecognizeFile`,
  `yandex_async_provider.py`): the whole multi-channel file goes inline in one
  request (≤ 60 MB, no Object Storage bucket), returning per-channel finals — this
  is the path that supports stereo interleaving and mono speaker labeling (see
  *Diarization & audio source*). The alternate `stream` mode (`yandex_provider.py`,
  `RecognizeStreaming`) transcribes one pre-split mono channel at a time and is
  capped at 5 min/session. Both use `audio_processing_type=FULL_DATA`,
  text-normalization on, languages whitelisted to `ru-RU`,`kk-KZ`.
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

## Diarization & audio source

Speaker separation is **channel-based**, not an acoustic model: in a stereo
recording the manager and client land on separate audio channels, so the channel
*is* the speaker. Two things make or break it.

**1. Stereo at the source (the keystone).** Diarization quality is decided before
transcription, at the telephony recording config:
- **Native Voximplant:** telephony settings → enable *«Запись всех звонков»* and
  **«Запись в стерео»** (dual-channel).
- **External integration:** the third-party telephony must also record dual-channel
  so calls reach Bitrix as 2-channel MP3 — otherwise its calls arrive **mono** and
  cannot be cleanly separated by channel.
- **Cutover:** stereo recording was enabled for **all** call sources (native +
  external) on `<fill in date when toggled>`. Calls recorded before then may be mono.
  A historical audit found ~3,532 mono (one undifferentiated segment) and ~1,752
  stereo-but-blobbed calls; per decision these are **not** re-processed — the fix is
  forward-only.

**2. Per-utterance interleaving (async provider).** The default async provider
(`yandex_async_provider.py`, `yandex_stt_mode="async"`) sends the whole file and
gets back per-channel finals. Each `final`/`final_refinement` is one utterance with
a time span; we keep them **as separate, timestamped segments** and interleave both
channels by start time, then merge consecutive same-speaker turns. The stored
transcript is therefore a real `[AGENT] … [CUSTOMER] … [AGENT] …` dialogue, not two
glued per-channel blobs. (Scoring relabels these neutrally — `scoring/prompt.py`
treats the two sides as audio channels and identifies the manager by content.)

**3. Mono safety net (`yandex_speaker_labeling`, default on).** For any call that
still arrives mono, the async request enables SpeechKit **speaker labeling**; the
per-speaker turn boundaries come back as `SpeakerAnalysis` `LAST_UTTERANCE` windows,
which we use to attribute each utterance to a speaker. If labeling yields no usable
windows we fall back to one undifferentiated `unknown` segment, so a mono call never
regresses below the old behaviour. (Live-verify this path on a real mono call after
deploy — it is exercised only by genuinely single-channel audio.)

**4. Role reconciliation (channel→role inversion fix).** The channel→role mapping
(`stereo_agent_channel`) is a fixed guess and is **inverted on some calls** — the
manager lands on the channel we labeled `customer`, so the stored transcript shows
«Менеджер»/«Клиент» backwards (scores are unaffected — scoring already identifies the
manager by content). The scorer therefore reports `CallScore.manager_side` (`A` = side
labeled agent, `B` = labeled customer, `unknown` = not determined / mono). After
scoring, `scoring.worker._reconcile_transcript_labels` swaps the segment `speaker`
fields and the `[AGENT]`/`[CUSTOMER]` headers in `full_text` when the manager is on
side `B`, so that **`speaker=="agent"` is always the manager**. It swaps in place
(headers only) rather than rebuilding `full_text` from segments, because entity
correction edits `full_text` alone — a rebuild would drop those fixes. It is guarded
by `transcripts.manager_side_applied` so a re-score never double-flips. **Backfill the
existing backlog** (re-score only, no re-download/re-transcribe) with:
`python -m AtamuraOKK.scoring requeue-relabel` (stereo only by default) then
`python -m AtamuraOKK.scoring run --all`. If a future diagnosis shows the inversion is
cleanly predicted by call direction, `stereo_agent_channel` could be made
direction-conditional to also fix labeling at transcription time (future transcripts
only — history is still fixed by the re-score above).

**Observability.** `transcribe_one` now writes `calls.is_stereo` (true probed
channel count ≥ 2) on every transcript. Query it by day to confirm the mono→stereo
cutover took effect and to catch regressions:
`select date(started_at), is_stereo, count(*) from calls group by 1,2 order by 1;`

## Configuration
```bash
ATAMURAOKK_TRANSCRIBE_PROVIDER=yandex
# Authorized-key JSON (relative paths resolve against the repo root). gitignored.
ATAMURAOKK_YANDEX_SA_KEY_FILE=authorized_key.json
# Global region (use the *.yandexcloud.kz hosts for the KZ installation):
ATAMURAOKK_YANDEX_STT_ENDPOINT=stt.api.cloud.yandex.net:443
ATAMURAOKK_YANDEX_IAM_ENDPOINT=https://iam.api.cloud.yandex.net/iam/v1/tokens
# Optional: ATAMURAOKK_YANDEX_STT_MODEL=general, ATAMURAOKK_YANDEX_STT_NORMALIZE=true
# Mono safety net: label speakers on single-channel calls (default true; stereo
# ignores it and separates by audio channel):
ATAMURAOKK_YANDEX_SPEAKER_LABELING=true
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
