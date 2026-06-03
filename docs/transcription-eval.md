# Transcription evaluation (Phase 0)

Goal: prove the riskiest assumption — **Kazakh transcription quality** — before
building the pipeline, and confirm how recordings are actually obtained from
this Bitrix portal.

Status: **in progress.** Tooling built and live connectivity validated. The WER
numbers (the decision gate) are still pending two unblocks — see *Open blockers*.

---

## Portal reconnaissance (verified against the live API)

Portal: `amanat.bitrix24.kz` · webhook user `65998` (admin) · TZ `Asia/Qyzylorda`.

| Item | Plan assumption | Reality on this portal |
|---|---|---|
| Volume | ~200 calls/day (~6k/mo) | Portal logs **~1,640 call *events*/day** raw, BUT scope is only the **first call per client** / **qualified clients** → **~200 analyzed calls/day**, so the plan's sizing holds. (Phase 1 ingestion must apply this filter — see below.) |
| Recording access | `CALL_RECORD_URL` on the call | **Mixed.** Native Voximplant calls expose a direct `CALL_RECORD_URL` (`storage-gw-ru-02.voximplant.com`, token in the URL, no extra scope). External-integration calls have **only `RECORD_FILE_ID`** (a Bitrix Drive file) and `CALL_RECORD_URL=null`. In a recent sample, ~3/8 had a direct URL. |
| Recording format | (assumed stereo if enabled) | Downloaded samples are **MP3, 8 kHz, 64 kbps, Stereo** — dual-channel container present, consistent with the no-diarization plan. *Still to confirm: the two channels carry agent vs. customer separately (not duplicated mono).* |
| Webhook scopes | telephony + user-read | Granted: **`crm`, `telephony`** only. Missing **`disk`** (resolve `RECORD_FILE_ID`) and **`user`** (map manager → name/email/department for Phase 1). |
| `voximplant.statistic.get` fields | per plan | Confirmed real: `CALL_ID`, `PORTAL_USER_ID`, `CALL_TYPE` (1=outbound, 2=inbound), `CALL_DURATION`, `CALL_START_DATE`, `CALL_RECORD_URL`, `CALL_VOTE`. Also present and useful: `CALL_FAILED_CODE` (200=answered, 304=missed), `RECORD_FILE_ID`, `RECORD_DURATION`, `CRM_ENTITY_TYPE/ID`, `CRM_ACTIVITY_ID`, `CALL_CATEGORY`, `PHONE_NUMBER`, `TRANSCRIPT_ID`. **Note:** `ORDER` is effectively ignored by this method (rows come back ascending by `ID`); we page a date-filtered window instead. |

### Implications for the architecture
- **Diarization is *not* fully avoidable.** Native calls are stereo (good), but
  external-integration calls (the majority right now) may be mono once we can
  fetch them — `pyannote` fallback in Phase 2 is likely needed, not optional.
- **Two recording-fetch paths** must both be supported in Phase 1 ingestion:
  direct `CALL_RECORD_URL`, and `RECORD_FILE_ID` → `disk.file.get` → `DOWNLOAD_URL`.
- **Ingestion must filter to ~200/day.** Only the *first call per client* /
  *qualified clients* are analyzed (not every call). A client = CRM entity
  (`CRM_ENTITY_TYPE` + `CRM_ENTITY_ID`); first call = earliest `CALL_START_DATE`
  for that entity. The exact definition of "qualified" (lead status / deal stage
  / custom field) is a Phase 1 open question to confirm with the operator.

---

## Open blockers (need operator action)

1. **Add `disk` scope** to the inbound webhook — required to fetch the
   external-integration recordings (the bulk of calls). Without it only the
   ~37% native-Voximplant calls are downloadable.
2. **Add `user` scope** — required by Phase 1 to map `PORTAL_USER_ID` →
   manager (name, email, department). Currently `user.get` → `insufficient_scope`.
3. **Run environment for Whisper.** `faster-whisper large-v3` + `ffmpeg` are not
   installed locally yet; the WER eval needs either a local CPU run (slow, large
   model download) or a GPU box.
4. **Human reference transcripts.** WER requires ~10 Russian + ~10 Kazakh calls
   hand-corrected (see *How to run*, stage 4).

---

## How to run the spike

Output dir defaults to `./.spike` (repo-local, gitignored; override with
`ATAMURAOKK_SPIKE_DIR`). Each stage writes inputs the next consumes.

```bash
make install-spike          # faster-whisper, jiwer, soundfile
brew install ffmpeg         # channel split + probe

make spike-fetch            # → calls.json (recent answered+recorded calls)
make spike-download         # → audio/<call>.mp3   (disk scope for most calls)
make spike-transcribe       # → transcripts/<call>.json (stereo-split + merge)
```

Then create hand-corrected references and label languages:

```
.spike/refs/<call_id>.txt            # corrected transcript, one per call
.spike/refs/labels.json              # {"<call_id>": "ru" | "kk", ...}
```

```bash
make spike-wer              # per-language WER table
```

---

## Results

_Pending the blockers above._

| Language | n | Mean WER | Notes |
|---|---|---|---|
| Russian (ru) | — | — | |
| Kazakh (kk) | — | — | |

## Decision gate (to be filled after results)

Rule of thumb from the plan: even ~25–30% WER still supports LLM scoring,
summarization, and compliance keyword detection.

- [ ] Kazakh WER acceptable → use `large-v3` for both languages.
- [ ] Kazakh too poor → remedy: Kazakh-fine-tuned Whisper checkpoint / NVIDIA
      NeMo / route Russian to a managed API, keep self-hosted Whisper for Kazakh.

**Chosen approach:** _TBD_
