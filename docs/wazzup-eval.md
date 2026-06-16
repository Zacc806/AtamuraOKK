# Wazzup API evaluation (Phase 0 spike)

Goal: confirm how to pull WhatsApp **calls + recordings** directly from the Wazzup
API (`api.wazzup24.com` v3) so they can be downloaded and Yandex-transcribed,
bypassing Bitrix. Run via `python -m AtamuraOKK.spike wazzup-probe` (read-only;
keys read from `ATAMURAOKK_WAZZUP_<number>` env vars). Raw findings:
`.spike/wazzup/probe.json`.

## Result: a direct API backfill is NOT possible

The Wazzup v3 API has **no read/history endpoint** for calls, messages, or
recordings. Data leaves Wazzup **only via webhook push**. There is nothing to
poll or backfill.

### What the probe found (verified against the live API, 2026-06-16)

| Endpoint | Result | Meaning |
|---|---|---|
| auth `Authorization: Bearer <key>` | works | both `.env` keys are the **same account key** (identical value), seeing the same account |
| `GET /v3/channels` | 200 | 17 WhatsApp channels; **16 are `state:"blocked"`** (disconnected), only one WABA channel (`transport:"wapi"`, `plainId 77006410499`) is `"active"` |
| `GET /v3/webhooks` | 200 | a webhook is **already configured** → `http://95.163.249.204:8036/wazzup_webhook`, subscribed to `messagesAndStatuses:true` + `contactsAndDealsCreation:true` |
| `GET /v3/calls`, `/call`, `/messages`, `/messages/history`, `/chats` | 404 | Express `Cannot GET …` — these routes **do not exist** |
| `GET /v3/contacts`, `/deals` | 403 | exist as push-sync entities but key is forbidden, and they carry no recordings |
| `GET /v3/users` | 403 | forbidden for this key |

### Implications

1. **No historical pull.** "Transcribe *all* the Wazzup calls" via a direct API
   backfill is infeasible — Wazzup exposes no message/call history over REST.
2. **One webhook slot, already taken.** Wazzup allows a single `webhooksUri` per
   account, currently pointed at `95.163.249.204:8036` — almost certainly the
   existing Wazzup→Bitrix bridge that already lands these calls in Bitrix as
   external-integration calls (`record_file_id`, no `recording_url`), which this
   pipeline **already downloads and Yandex-transcribes** (~3,300 such calls are
   `TRANSCRIBED`/`SCORED`). Repointing the webhook to this pipeline would **break
   that existing integration** and is an outward-facing, production-affecting
   change — not to be done without the operator's explicit coordination.
3. **Most channels are blocked**, so even a new webhook would capture little until
   the WhatsApp connections are restored.

## Options (need operator decision — see handoff to user)

- **A. Use the data already in Bitrix.** The Wazzup calls already arrive via the
  existing bridge and are already Yandex-transcribed. The real backlog is the
  174 `DOWNLOADED` (awaiting transcription) and 651 `FAILED`-at-*scoring* external
  calls — not transcription. This needs no Wazzup API work.
- **B. Webhook capture going forward** (no history): stand up a receiver and
  repoint Wazzup's single webhook at it — breaks the current bridge; requires
  operator sign-off and a plan for the existing consumer. Captures only new calls,
  and only on unblocked channels.
- **C. Confirm with Wazzup support** whether an export/analytics API exists on a
  higher plan (none reachable from this key).
