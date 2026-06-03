# Bitrix24 Call Analysis & QA Feedback System — Implementation Plan

## For the coding agent (read first)
Build this in the phases below, **in order**. After each phase, stop, summarize what you built and how to run it, and wait for my confirmation before starting the next phase. Do not skip ahead.

- Values marked `TODO(me)` are secrets/decisions I will provide. Ask me for them when you reach them — do not invent or hardcode them.
- **Before relying on any Bitrix24 REST method signature or field name, confirm it against the official docs.** Bitrix publishes a REST-docs MCP server you can connect to for exactly this; otherwise use https://apidocs.bitrix24.com. Field names below are my best recollection and must be verified.
- Keep transcription and scoring behind interfaces (`Transcriber`, `Scorer`) so the underlying provider can be swapped without touching the pipeline.

## Context & constraints
- **Source of truth for calls:** Bitrix24 (cloud), via REST API.
- **Volume:** ~200 calls/day (~6,000/month). This is low — do not over-engineer the infrastructure.
- **Deployment:** cloud, no on-prem requirement.
- **Call languages:** ~70% Russian, ~30% Kazakh *(assumption — tell me if it's the reverse)*. The Kazakh share is large enough that **Kazakh transcription quality is the main technical risk**, so we validate it in Phase 0 before building everything else.
- **Primary deliverable:** a per-call QA analysis pipeline + a **dashboard where each department head sees the analysis per manager in their own department**.

## Recommended stack
- **Runtime:** Python 3.11+.
- **Transcription:** `faster-whisper` with **Whisper large-v3**, run on a **serverless GPU** (Modal / Replicate / RunPod serverless) so there is **no idle GPU cost** at this volume. Wrapped behind a `Transcriber` interface (so we can later route the Russian portion to a cheap API if we want).
- **Diarization:** **not needed if Bitrix stereo recording is enabled** — agent and customer land on separate channels, so we transcribe each channel separately. Fallback for mono/historical calls: `pyannote.audio`.
- **Analysis/scoring:** an LLM behind a `Scorer` interface, returning **structured JSON** validated against a schema, using a **bilingual (RU/KK) prompt**.
- **Database:** PostgreSQL.
- **Orchestration:** a scheduled job + a **DB-backed status queue** (a cron/APScheduler worker that processes rows by `status`). At 200/day there is no need for Celery/Redis; use Redis+RQ only if you prefer it.
- **Dashboard:** **Metabase** (fastest to stand up, runs in Docker) on top of Postgres, with group-based row-level access. Superset is an acceptable alternative.
- **Hosting:** one small always-on VM (ingestion + Postgres + Metabase) + serverless GPU for transcription + S3-compatible object storage for audio.

## Architecture (data flow)
```
Bitrix24
  → [Ingestion]      → Postgres call rows (status=NEW) + audio in object storage
  → [Transcription]  → transcripts (status=TRANSCRIBED)
  → [Analysis]       → scores (status=SCORED)
  → [Metabase]       → per-manager / per-department dashboards
  → (optional) score+summary written back to Bitrix on the call
```

## Prerequisites I will set up in Bitrix before Phase 1
- In the telephony settings for the number(s), enable **"Save recordings of all calls"** and **"Record stereo sound."** Stereo lets us skip diarization for every call going forward.
- Create an **inbound webhook** with telephony + user-read scopes and give you the URL → `TODO(me): BITRIX_WEBHOOK_URL`. (For a single internal portal an inbound webhook is correct; we would only move to a full OAuth app if this became a multi-tenant marketplace app.)

---

## Phase 0 — Setup + transcription spike (validate the Kazakh risk first)
**Goal:** prove the riskiest assumption — Kazakh transcription quality — before building the pipeline.

1. Repo scaffold: dependency management, `.env` handling (pydantic-settings), Postgres via Docker Compose, a Makefile with common commands.
2. Bitrix connectivity: call `voximplant.statistic.get` (list method; paginate via `start`; filter by a date/ID cursor). Pull ~50 recent calls with fields: `CALL_ID`, `PORTAL_USER_ID`, `CALL_TYPE` (**1 = outbound, 2 = inbound**), `CALL_DURATION`, `CALL_START_DATE`, `CALL_RECORD_URL`, `CALL_VOTE`. **Verify exact field names in the docs.**
3. Download the recordings from the record URL; confirm whether they are stereo (2 channels).
4. Run `faster-whisper` large-v3 on the ~50 calls (per channel if stereo) with language auto-detection.
5. Hand-correct ~10 Russian and ~10 Kazakh calls and compute **WER per language**.
6. **Decision gate:**
   - If Kazakh WER is good enough for scoring (rough rule of thumb: even ~25–30% WER still supports LLM scoring, summarization, and keyword/compliance detection — it doesn't need to be verbatim-clean) → proceed with large-v3 for both languages.
   - If Kazakh is too poor → pick a remedy and document it: a **Kazakh-fine-tuned Whisper checkpoint** from Hugging Face, **NVIDIA NeMo**, or routing only the Russian calls to a cheap managed API while keeping self-hosted Whisper for Kazakh.

**Deliverable:** `docs/transcription-eval.md` with the per-language WER numbers and the chosen transcription approach.

## Phase 1 — Ingestion
1. Scheduled ingestion (every 2–4 hours) that pulls new calls since the last cursor via `voximplant.statistic.get`. Make it **idempotent on `CALL_ID`** (upsert).
2. Map `PORTAL_USER_ID` → manager via `user.get` (cache users; store name, email, department).
3. Download recordings to **S3-compatible object storage** (AWS S3 / Backblaze B2 / MinIO); store the object path on the call row.
4. Call lifecycle `status`: `NEW → DOWNLOADED → TRANSCRIBED → SCORED → (optional) PUSHED`, plus `FAILED`. Add retry/backoff for Bitrix rate limits.

## Phase 2 — Transcription worker
1. Worker picks `DOWNLOADED` rows and sends audio to the serverless GPU running `faster-whisper` large-v3.
2. **If stereo:** transcribe channel 0 as **agent**, channel 1 as **customer**, then merge into one timestamped, speaker-labeled transcript. **If mono:** run `pyannote` diarization and assign the agent speaker by a documented heuristic (e.g. who speaks first on outbound calls).
3. Persist the transcript: detected language, full text, and a `segments` JSONB array (`speaker`, `start`, `end`, `text`).
4. Mark `TRANSCRIBED`; on failure set `FAILED` + error message.

## Phase 3 — Analysis & scoring
1. Define the QA rubric as **versioned config** (a `rubric_versions` row + a YAML/JSON file in the repo). Starter rubric — weights sum to 100, each criterion scored 0–5 with a one-line justification and a short evidence snippet:
   - Greeting & identification — 10
   - Needs discovery / asking the right questions — 20
   - Product/service knowledge & accuracy — 15
   - Objection handling — 15
   - Required compliance phrases / disclosures — 10
   - Tone, empathy, professionalism — 10
   - Call control & structure — 10
   - Clear next step / CTA secured — 10

   Plus, per call: overall **sentiment** (customer and agent), a 2–3 sentence **summary**, and a list of **red flags** (rudeness, missed compliance, etc.).
2. Implement the `Scorer` interface; default implementation calls an LLM with a **bilingual system prompt** that instructs it to score consistently whether the call is in Russian or Kazakh, and to quote evidence in the original language. Request **structured JSON** matching a fixed schema; **validate** with pydantic/JSON-schema and **retry** on malformed output.
3. Store: `total_score`, per-criterion scores + justifications (JSONB), sentiment (JSONB), summary, flags (JSONB), model name, `rubric_version`, `created_at`. Mark `SCORED`.

## Phase 4 — Dashboard (Metabase) — the core deliverable
1. Stand up Metabase (Docker) against Postgres.
2. Build the views:
   - **Per-manager scorecard:** average total + per-criterion over time, call count, trend.
   - **Department roll-up:** managers ranked, score distribution, week-over-week change.
   - **Call drill-down:** transcript + per-criterion scores + summary + flags + a link to the audio, for a single call.
   - **Flagged-calls queue:** compliance misses, low scores, negative sentiment.
3. **Access control (the original requirement):** create a Metabase **group per department** and use Metabase **data sandboxing / row-level permissions** so a department head only sees calls whose manager's `department_id` matches theirs. The org hierarchy lives in the `departments` and `managers` tables.

## Phase 5 — Hardening & optional Bitrix writeback
1. Observability: structured logs and a **daily run summary** (calls ingested / transcribed / scored, failures, audio minutes, LLM tokens, estimated cost).
2. Retries and a dead-letter path for `FAILED` rows; alert (email/Telegram) on repeated failures.
3. Optional: push the summary/score back onto the call in Bitrix via `telephony.call.attachTranscription` (**confirm the signature in the docs**).
4. **Compliance — confirm with your own legal/DPO; this is not legal advice:** make sure recording consent is announced (Bitrix can play a pre-call warning), lock down dashboard and storage access, and set a recording/transcript **retention policy**. Kazakhstan's personal-data law applies to stored call data (and GDPR too, if any customers are in the EU).

---

## Suggested database schema (starting point)
```sql
departments(id, name)

managers(id, bitrix_user_id UNIQUE, name, email,
         department_id REFERENCES departments(id))

calls(id, bitrix_call_id UNIQUE,
      manager_id REFERENCES managers(id),
      direction, started_at, duration_sec,
      recording_url, audio_path, is_stereo,
      status, error, created_at)

transcripts(id, call_id REFERENCES calls(id) UNIQUE,
            language, full_text, segments JSONB, model, created_at)

scores(id, call_id REFERENCES calls(id),
       rubric_version, total_score,
       criteria JSONB, sentiment JSONB, summary, flags JSONB,
       model, created_at)

rubric_versions(id, version, definition JSONB, active, created_at)

ingest_state(id, last_cursor, updated_at)
```

## Environment / secrets (`TODO(me)` — ask me when needed)
- `BITRIX_WEBHOOK_URL`
- `DATABASE_URL`
- object storage credentials (`S3_*`)
- serverless GPU token (Modal / Replicate / RunPod)
- LLM API key + model name
- Metabase admin credentials

## Build-order recap
Phase 0 (spike) → confirm transcription approach → Phase 1 → 2 → 3 → 4 → 5. **Stop for my confirmation after each phase.**