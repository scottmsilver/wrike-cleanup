# Wrike Preview Service — Design

**Date:** 2026-05-23
**Status:** Spec, pending implementation plan
**Revision:** rev 2 — incorporates Codex review findings (correctness, security, ops gaps)

## Problem

Wrike previews images, PDFs, and (on Business+ tier) Office files inline. For accounts without Business+ — or for any file type Wrike doesn't render — users must download attachments just to glance at them. We want every Office attachment (`.doc/.docx/.xls/.xlsx/.ppt/.pptx`) on a task to have a sibling `preview_<sourceAttachmentId>.pdf` automatically attached to the same task, so it previews natively in Wrike.

## Goals

- **Real-time-ish** preview generation for new attachments (target: minutes, not hours).
- **Self-bootstrapping**: on first deploy, walk historical attachments and generate previews for them too.
- **Resilient**: webhook delivery is at-least-once and not exactly-once; missed events must be caught, and missing events for many hours/days must not lose data.
- **Idempotent**: webhook redeliveries and reconciliation overlap must never double-convert, double-upload, or resurrect terminal-state jobs.
- **Safe under concurrency**: simultaneous `/tick` invocations and Cloud Run multi-request containers must not double-process the same job.
- **Cheap**: target ~$0/mo on personal scale.

## Non-goals

- No web UI / admin dashboard. Firestore console + a small CLI for requeue/inspect.
- No support for non-Office types (text, code, markdown, .pages, etc.) — future work.
- No support for folder-scoped or comment-scoped attachments — future work; documented as `skipped` reason.
- No metrics dashboards. Cloud Logging with structured log lines is enough for v1.
- No size-limit handling beyond a pre-download size check (see §`/tick`).
- No support for attachment macros / scripting. Conversion runs with macros disabled.

## Stack

| Piece | Choice | Why |
|---|---|---|
| Compute | Cloud Run (single service) | Scale-to-zero, free tier covers personal volume. |
| State | Firestore (Native mode) | Free tier, ADC auth from Cloud Run, no connection strings. |
| Scheduling | Cloud Scheduler (2 jobs) | Free tier (3 jobs free). |
| Conversion | LibreOffice headless (`soffice --headless --convert-to pdf -env:UserInstallation=...`) | Best fidelity across DOCX/XLSX/PPTX. Per-job profile isolates concurrent runs. |
| Secrets | Secret Manager (mounted as env into Cloud Run) | Wrike permanent tokens grant broad account access — rotate-able, audit-able. |
| Wrike integration | Webhook (account-wide, `AttachmentAdded` only) + REST API for fetch/upload | Real-time trigger; minimal API surface. |

Cold start of a Python+LibreOffice container is ~10–20s. Decoupling webhook receipt from conversion keeps the webhook handler tiny and fast: it does no LibreOffice work, so even a cold-start delivery fits comfortably in Wrike's webhook timeout. The heavy conversion runs in `/tick`, where cold start doesn't affect Wrike's view of webhook health.

## Architecture

One Cloud Run service. One Docker image. Four HTTP routes — three of them authenticated:

| Route | Caller | Auth | Purpose |
|---|---|---|---|
| `POST /webhook` | Wrike | HMAC-SHA256 of body, verified against `X-Hook-Signature`. Public Cloud Run ingress (Wrike can't OIDC-sign). | Verify, write `jobs/{id}` create-if-absent, return 200. |
| `POST /tick` | Cloud Scheduler (every 1 min) | OIDC token signed by Scheduler's service account, verified via Google's public keys. **Also** rejects requests missing `X-Internal-Secret` header (defense-in-depth). | Lease + drain `jobs`. |
| `POST /reconcile` | Cloud Scheduler (every 30 min) | Same as `/tick`. | Bootstrap walk or steady-state catch-up. |
| `GET /healthz` | Cloud Run startup/liveness probe | None | Liveness only. Returns 200 always. |

**Public-route exposure (Codex P1):** the Cloud Run service has `--allow-unauthenticated` because Wrike can't sign OIDC tokens for `/webhook`. That means `/tick` and `/reconcile` are also publicly reachable. Both verify two things in the handler before doing any work:
1. `Authorization: Bearer <OIDC token>` validates against Google's JWKS and matches the Scheduler service account.
2. `X-Internal-Secret: <shared secret from Secret Manager>` matches the configured value.

If either fails, return 200 (do not 4xx — see §webhook signature handling for why); log at WARN; do nothing. Bots probing the routes cost us a Firestore noop read and a log line.

Latency: webhook arrival → PDF on task ≈ 5s to 1 min, depending where in the minute `/tick` fires + cold-start cost.

## Data model (Firestore)

### `jobs` collection — one document per attachment we've ever seen

Document ID = Wrike `attachmentId`. Idempotency for free.

```
jobs/{attachmentId}
  taskId          : string | null            // null for folder/comment attachments
  scope           : "task" | "folder" | "comment"
  originalName    : string                   // the source filename, lowercased extension preserved
  mimeType        : string | null            // from Wrike metadata
  sizeBytes       : number | null            // from Wrike metadata
  status          : "pending" | "done" | "skipped" | "failed" | "superseded"
  skippedReason   : string | null            // "already_previewable" | "wrong_scope" | "filename_is_preview" | "wrong_type" | "too_large" | ...
  error           : string | null            // truncated error message, last 500 chars, secrets scrubbed
  errorCode       : string | null            // "deleted" | "password_protected" | "soffice_crash" | "timeout" | "wrike_5xx" | ...
  previewId       : string | null            // the PDF's Wrike attachment id once uploaded
  previewName     : string | null            // the PDF filename we used (preview_<sourceAttachmentId>.pdf)
  attempts        : number                   // incremented at lease time, not at completion
  nextAttemptAt   : Timestamp                // when this row is eligible to be leased
  leasedBy        : string | null            // request ID of holder; null if unleased
  leasedUntil     : Timestamp | null         // expiry; null if unleased
  createdAt       : Timestamp
  updatedAt       : Timestamp
```

Composite indexes (declared in `preview/firestore.indexes.json`, deployed by `gcloud firestore indexes create`):
- `(status ASC, nextAttemptAt ASC, leasedUntil ASC)` — for `/tick` lease query.

### `state` collection — singleton documents

```
state/reconcile
  isBootstrap            : boolean         // true while walking backward; flips false at floor
  oldestCovered          : Timestamp       // bootstrap cursor; window slides backward
  lastReconciledThrough  : Timestamp       // steady-state cursor; advances forward
  bootstrapFloor         : Timestamp       // hard floor; default = (firstRunTime - 10 years)
  updatedAt              : Timestamp
```

No "emptyChunks" heuristic. Bootstrap walks to `bootstrapFloor` unconditionally (Codex P1: a quiet 90-day stretch shouldn't terminate the walk).

## `/webhook` — Wrike event receiver

### Handshake

When Wrike registers (or re-verifies) the webhook, it POSTs the registration URL with header `X-Hook-Secret: <random string>` and body `{"requestType":"WebHook secret verification"}`. The handler must:
1. Recognize the verification request by its body shape.
2. Respond `200 OK` with header `X-Hook-Secret: hmac_sha256(WEBHOOK_SIGNING_SECRET, received_secret_value)` (hex-encoded).

Refuse to respond to any other `X-Hook-Secret` value (signing-oracle guard): the verification body must be present; otherwise treat as a regular event delivery.

### Event delivery

Every subsequent POST is an event payload. Algorithm:

1. Compute `expected = hmac_sha256(WEBHOOK_SIGNING_SECRET, raw_body)`.
2. Compare to `X-Hook-Signature` (constant-time compare).
3. **On mismatch: return 200, log WARN.** Wrike treats most 4xx (including 401) as non-retryable and will suspend the webhook (Codex P1). Returning 200 makes a noisy attacker visible in logs without breaking our pipeline.
4. Parse the JSON body. If `eventType != "AttachmentAdded"`: return 200 immediately.
5. **Create-if-absent** (Codex P1): try `db.collection("jobs").document(attachmentId).create({...})` with `status='pending'`, `attempts=0`, `nextAttemptAt=now`, leases null, and a minimal initial doc (taskId from the event, everything else null until `/tick` enriches). If the doc already exists, swallow `AlreadyExists` and return 200 — we've seen this attachment before; never reset a `done`/`failed`/in-progress row.
6. Return 200.

Firestore write failure → return 200, log ERROR. The reconcile loop catches it later. (Returning 500 would just get Wrike to retry 3× and suspend the webhook on persistent Firestore outage. Reconcile is a real safety net because of §`/reconcile`'s `lastReconciledThrough` cursor.)

## `/tick` — converter

Triggered every 1 minute by Cloud Scheduler.

### Lease query (Codex P1, P2)

In a Firestore transaction, find one job to claim:

```python
jobs.where("status", "==", "pending") \
    .where("nextAttemptAt", "<=", now) \
    .where("leasedUntil", "<", now)      # null sorts as < any timestamp via where() — see note
    .order_by("nextAttemptAt") \
    .limit(BATCH_SIZE)                    # BATCH_SIZE = 3
```

For each candidate, in its own transaction:
- Re-read the doc.
- If `status != 'pending'` or `leasedUntil > now` or `nextAttemptAt > now`: skip (raced).
- Else: set `leasedBy=<this request id (a UUID)>`, `leasedUntil=now + LEASE_TTL`, `attempts=attempts+1`. **Attempts increment at lease time, not at completion** — this ensures an OOM-killed container still counts the attempt (Codex P1).
- Process the job (steps below). On final success or final failure, update the doc and clear the lease.

`LEASE_TTL = 6 minutes`. Cloud Scheduler fires every 1 minute, so an expired lease becomes claimable on the next tick.

Firestore quirk: `where("leasedUntil", "<", now)` won't match documents where `leasedUntil` is null. Workaround: initialize `leasedUntil` to epoch zero when creating a job, never null. (Updated data model: `leasedUntil` is a Timestamp, never null; semantic-null = epoch.)

`BATCH_SIZE=3` keeps a `/tick` invocation under Cloud Run's default 300s request timeout even with worst-case ~30s conversions and a cold start.

### Per-job pipeline

1. **Fetch metadata**: `GET /attachments/{attachmentId}`. On 404 → `status='failed'`, `errorCode='deleted'`. No retry (Codex P1: deleted originals don't recover).
2. **Fill missing fields**: `originalName`, `mimeType`, `sizeBytes`, `scope` (task/folder/comment), `taskId` (if scope=task). Persist.
3. **Filter by scope** (Codex P1: comment + folder attachments out of v1 scope):
   - `scope != 'task'` → `status='skipped'`, `skippedReason='wrong_scope'`.
4. **Filter by filename** (Codex P2: classification ordering):
   - Name matches `^preview_[A-Z0-9]+\.pdf$` (our output pattern) → `status='skipped'`, `skippedReason='filename_is_preview'`.
5. **Filter by size**:
   - `sizeBytes > MAX_SIZE_BYTES` (`MAX_SIZE_BYTES = 50 * 1024 * 1024`, i.e. 50 MiB) → `status='skipped'`, `skippedReason='too_large'`. No retry, no download.
6. **Classify by extension**:
   - `.pdf, .png, .jpg, .jpeg, .heic, .svg, .tiff, .psd` → `status='skipped'`, `skippedReason='already_previewable'`.
   - `.doc, .docx, .xls, .xlsx, .ppt, .pptx` → convert.
   - Anything else → `status='skipped'`, `skippedReason='wrong_type'`.
7. **Version cleanup** (Codex P1): query Firestore for `jobs.where(taskId==X).where(originalName==thisName).where(status=='done')`, excluding self. For each match, delete its `previewId` from Wrike (best-effort), then mark the old job's `previewId=null`, `status='superseded'` (new status value — add to enum). This handles attachment-version replacements where Wrike issues a new `attachmentId` for the same logical file.
8. **Download**: `GET /attachments/{id}/download` to a `tempfile.TemporaryDirectory()`. Stream to disk.
9. **Convert** with a per-job LibreOffice profile (Codex P1: concurrency safety):
   ```
   soffice --headless \
           -env:UserInstallation=file://${tmpdir}/lo-profile \
           --norestore --nofirststartwizard \
           --convert-to pdf \
           --outdir ${tmpdir} \
           ${input_path}
   ```
   Timeout: 120s. Macros disabled by default in headless mode; we further set `Macros::Disable` via the profile.
10. **Classify conversion errors**:
    - Exit code 0, output PDF present → success.
    - Exit code 0, no output → `errorCode='soffice_no_output'`.
    - Timeout → `errorCode='soffice_timeout'`.
    - Non-zero exit → inspect stderr for `Password to open` / `password-protected` → `errorCode='password_protected'` → no retry (permanent failure).
    - Any other non-zero exit → `errorCode='soffice_crash'`.
11. **Upload**: `POST /tasks/{taskId}/attachments` with filename `preview_<sourceAttachmentId>.pdf` (Codex P2: unique forever, no collision with user files). Capture new attachment id.
12. **Mark done**: `status='done'`, `previewId=<new id>`, `previewName='preview_<sourceAttachmentId>.pdf'`, clear lease, `updatedAt=now`.

### Retry / backoff

On any transient error in steps 1–11 (excluding `errorCode` values marked "permanent"):

```python
if attempts >= MAX_ATTEMPTS:                  # MAX_ATTEMPTS = 5
    set(status='failed', error=str(e)[:500], errorCode='exhausted', leasedBy=null, leasedUntil=epoch)
else:
    set(status='pending', error=str(e)[:500], errorCode=<classified>,
        nextAttemptAt=now + BACKOFF[attempts], leasedBy=null, leasedUntil=epoch)
```

Backoff schedule (seconds): `{1: 5*60, 2: 30*60, 3: 2*60*60, 4: 12*60*60}`. Five tries spread across ~14.5 hours.

Permanent errors short-circuit to `status='failed'` with no retry: `deleted`, `password_protected`, `too_large` (also `skipped`), `wrong_scope`, `wrong_type`, `filename_is_preview`.

### Comment attachment handling

`AttachmentAdded` may fire for comment-scoped attachments. Step 2's metadata fetch reveals `scope`; step 3 marks them `skipped` rather than uploading a preview to the wrong place (Codex P2).

## `/reconcile` — bootstrap + steady-state

Reads `state/reconcile`. First-ever call (doc missing): initialize `isBootstrap=true`, `oldestCovered=now`, `lastReconciledThrough=now`, `bootstrapFloor=now-10years`. Then proceed.

### Bootstrap mode (`isBootstrap == true`)

```
window_end   = state.oldestCovered
window_start = max(window_end - 28 days, state.bootstrapFloor)    # 28 days, not 30 (Codex P2: <31 day API limit)
attachments  = paginate(wrike.GET /attachments?createdDate=[window_start, window_end])
for a in attachments:
    if jobs/{a.id} does not exist:
        create jobs/{a.id} as pending (same shape as /webhook's create)
state.oldestCovered = window_start
if window_start <= state.bootstrapFloor:
    state.isBootstrap = false
```

No empty-chunks heuristic (Codex P1). Walk unconditionally back to the 10-year floor. For a 5-year account, ~65 invocations × 30-min cadence ≈ 33 hours total backfill time.

### Steady-state mode (`isBootstrap == false`)

```
window_start = state.lastReconciledThrough - 1 hour     # 1h overlap for safety
window_end   = now
attachments  = paginate(wrike.GET /attachments?createdDate=[window_start, window_end])
for a in attachments:
    if jobs/{a.id} does not exist:
        create jobs/{a.id} as pending
state.lastReconciledThrough = window_end
```

`lastReconciledThrough` is the durable high-water mark (Codex P1: 24h floating window loses data when reconcile/webhook is broken for >24h). With a persistent cursor, breaking reconcile for a week and then fixing it walks the entire missed week on the next run.

If a single `lastReconciledThrough → now` span exceeds 28 days (the Wrike API window cap), chunk it into 28-day pages within the same `/reconcile` call, advancing the cursor after each chunk. Single calls bounded by Cloud Run's 300s default request timeout.

## Setup (one-time)

A `preview/SETUP.md` documents this. Sketch:

1. Create GCP project. Enable: Cloud Run, Firestore, Cloud Scheduler, Artifact Registry, Secret Manager.
2. `gcloud firestore databases create --location=us-central` (Native mode).
3. Store secrets:
   ```
   gcloud secrets create wrike-api-token --data-file=-
   gcloud secrets create webhook-signing-secret --data-file=-
   gcloud secrets create internal-secret --data-file=-       # for /tick + /reconcile defense-in-depth
   ```
4. Create a runtime service account `preview-sa@...` for Cloud Run. Grant: `roles/datastore.user`, `roles/secretmanager.secretAccessor` (limited to the three secrets above).
5. Build + push container: `gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/preview/server`.
6. Deploy: `gcloud run deploy preview --image ... --service-account=preview-sa@... --allow-unauthenticated --concurrency=2 --memory=1Gi --timeout=300 --set-secrets=WRIKE_API_TOKEN=wrike-api-token:latest,WEBHOOK_SIGNING_SECRET=webhook-signing-secret:latest,INTERNAL_SECRET=internal-secret:latest`. Stable URL returned, e.g. `https://preview-xxxxxx.run.app`.
7. Deploy Firestore indexes: `gcloud firestore indexes create --file=preview/firestore.indexes.json`.
8. Create Scheduler service account `preview-scheduler@...`. Grant: `roles/run.invoker` on the Cloud Run service.
9. Create two Cloud Scheduler jobs:
   ```
   gcloud scheduler jobs create http wrike-tick \
       --schedule="* * * * *" \
       --uri=https://preview-xxxxxx.run.app/tick \
       --oidc-service-account-email=preview-scheduler@... \
       --headers="X-Internal-Secret=$(gcloud secrets versions access latest --secret=internal-secret)"
   # similar for wrike-reconcile, every 30 min
   ```
10. Register the Wrike webhook (one API call from `preview/register_webhook.py`):
    ```python
    wrike.post("/webhooks", {
        "hookUrl": "https://preview-xxxxxx.run.app/webhook",
        "secret": webhook_signing_secret,
        "events": ["AttachmentAdded"]
    })
    ```
    Wrike POSTs back with the verification body; the handler responds with the HMAC; Wrike marks it active.

## Repo structure

```
wrike-cleanup/
├── main.py                       # existing cleanup CLI (unchanged behavior)
├── wrike.py                      # NEW: extracted WrikeApi class, shared with preview/
├── CLAUDE.md
├── README.md
├── config.json                   # gitignored
└── preview/                      # NEW: the cloud service
    ├── server.py                 # Flask app: /webhook /tick /reconcile /healthz
    ├── convert.py                # download → soffice → upload (pure functions)
    ├── reconcile.py              # bootstrap + steady-state walk
    ├── store.py                  # Firestore wrappers (jobs + state, lease ops)
    ├── auth.py                   # OIDC verifier + internal-secret check
    ├── classify.py               # extension/mime/scope/size classification
    ├── register_webhook.py       # one-shot setup helper
    ├── admin.py                  # CLI: list-failed, requeue, pause, resume
    ├── firestore.indexes.json    # composite index definition
    ├── Dockerfile                # python:3.12-slim + libreoffice + fonts
    ├── docker-compose.yml        # local emulator stack
    ├── requirements.txt
    └── SETUP.md
```

`Dockerfile` installs LibreOffice + a sensible font pack (`fonts-liberation`, `fonts-dejavu`, `fonts-noto-cjk`). Without fonts, conversions render in fallback faces and look wrong (Codex P2).

## Phased rollout & local testing

The user explicitly wants to be able to test in clean phases, locally, with `docker compose`. Each phase is independently testable and produces a usable artifact.

### Phase 0 — Refactor `WrikeApi` out of `main.py`

- Move the `WrikeApi` class verbatim to `wrike.py`.
- Update `main.py`'s import. Behavior unchanged.
- **Test**: run `python main.py --do_nothing` against your existing `config.json`. Output should match pre-refactor behavior.
- **Done when**: `git diff` shows pure code motion + import lines.

### Phase 1 — Conversion core, no Wrike, no Firestore

- Write `preview/convert.py` with one pure function: `convert_to_pdf(input_path: Path, work_dir: Path) -> Path`. Handles per-job LibreOffice profile + error classification.
- Add `preview/Dockerfile` (Python + LibreOffice + fonts).
- **Test locally**: `docker build -t preview-convert preview/` then `docker run --rm -v $(pwd)/test-fixtures:/work preview-convert python -c "from convert import convert_to_pdf; convert_to_pdf('/work/sample.docx', '/work/out')"`. Verify a PDF appears.
- Repeat for sample DOCX, XLSX, PPTX, a password-protected DOCX, and a deliberately corrupt file. Each should produce either a PDF or the expected `errorCode`.
- **Done when**: all sample types convert (or fail with the correct code) inside the container.

### Phase 2 — Wrike round-trip CLI, no Firestore

- Write `preview/cli.py`: takes `--task-id` and `--attachment-id`. Downloads via Wrike, runs `convert.py`, uploads as `preview_<id>.pdf`.
- Hits live Wrike; **use a test Wrike workspace if you can — this is a real write.**
- **Test**: `docker run --rm -v $(pwd):/app -w /app -e WRIKE_API_TOKEN=$(jq -r .WRIKE_API_TOKEN config.json) preview-convert python preview/cli.py --task-id IEAAAAAA --attachment-id IEAAAAAB`.
- Verify the PDF appears on the task in Wrike's UI and previews correctly.
- **Done when**: round-trip works for one real DOCX in your account.

### Phase 3 — Firestore emulator, lease semantics, end-to-end on localhost

- Add `preview/store.py` (Firestore wrappers with transactional lease).
- Add `preview/docker-compose.yml`:
  ```yaml
  services:
    firestore:
      image: gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators
      command: gcloud emulators firestore start --host-port=0.0.0.0:8080
      ports: ["8080:8080"]
    app:
      build: .
      environment:
        - FIRESTORE_EMULATOR_HOST=firestore:8080
        - WRIKE_API_TOKEN=...
        - WEBHOOK_SIGNING_SECRET=devsecret
        - INTERNAL_SECRET=devinternal
      ports: ["5000:5000"]
      depends_on: [firestore]
  ```
- Write `preview/server.py` with all four routes, but skip OIDC verification when `FIRESTORE_EMULATOR_HOST` is set (dev mode).
- **Test**: `docker compose up`, then `curl -X POST localhost:5000/tick -H "X-Internal-Secret: devinternal"`. With no pending jobs: returns 200, empty. Seed Firestore manually (or via the CLI from Phase 2 pointed at the emulator), re-trigger `/tick`, verify conversion + upload.
- **Idempotency test**: send the same fake webhook twice with `curl`. Verify only one Firestore doc + only one preview uploaded.
- **Lease test**: hold a lease open in one shell while another `/tick` runs. Verify second tick skips the leased job.
- **Done when**: end-to-end webhook → /tick → preview on Wrike works locally with docker compose, idempotency + lease verified.

### Phase 4 — Webhook from real Wrike to localhost via tunnel

- Run `docker compose up`.
- Open a tunnel: `cloudflared tunnel --url http://localhost:5000` (or ngrok). Get a public HTTPS URL.
- Register a webhook against your test workspace pointing at `<tunnel>/webhook`.
- **Test**: upload a DOCX to a task in the test workspace. Watch logs. Verify webhook arrives, signature verifies, doc is created, `/tick` (curl-triggered or natural cadence) processes it, PDF appears on the task.
- **Test handshake separately**: delete the webhook, re-register, verify the handshake response is accepted by Wrike.
- **Done when**: a real Wrike upload in your test workspace triggers a real preview, hitting only local containers.

### Phase 5 — Reconcile mode locally

- With docker compose still up + tunnel still live, manually trigger `/reconcile` with curl. Verify it walks the test workspace's recent attachments and enqueues anything missed.
- Manually pull a job back to `status='pending'` via the admin CLI, verify reconcile doesn't duplicate it.
- **Test bootstrap**: reset `state/reconcile` to `isBootstrap=true`, `oldestCovered=now`, `bootstrapFloor=now-30days`. Trigger reconcile repeatedly. Verify it walks one 28-day chunk per call, terminates correctly.
- **Done when**: both reconcile modes work end-to-end against local emulator.

### Phase 6 — Deploy to Cloud Run

- Push container to Artifact Registry.
- Run `gcloud run deploy ...` from §Setup step 6.
- Migrate from emulator data to production Firestore (or just start fresh — backfill will re-discover).
- Create Schedulers and Wrike webhook (steps 9, 10).
- **Test**: upload one new DOCX to a real task. Watch Cloud Logging. Verify preview appears within ~1 minute.
- **Done when**: production deployment passes the same single-DOCX test as Phase 4.

### Phase 7 — Backfill live

- Bootstrap starts naturally on first `/reconcile`.
- Monitor Cloud Logging for the next ~33 hours (5-year history × 28-day windows × 30-min cadence).
- Spot-check tasks to confirm previews are appearing.
- Use the admin CLI to query `status='failed'` jobs periodically; investigate any patterns.

### Local testing notes

- **Test fixtures**: keep `preview/test-fixtures/` with one of each: `sample.docx`, `sample.xlsx`, `sample.pptx`, `password.docx`, `corrupt.docx`, `huge.docx` (50MB+), `not-office.zip`. Gitignore the directory, document the expected files in a README.
- **Local credentials**: docker compose reads `WRIKE_API_TOKEN` from your shell. Never commit them.
- **Emulator persistence**: the Firestore emulator drops state on container restart. Use `--data-dir=/data` mounted volume if you want persistence across compose ups.
- **Cost while testing**: $0. Emulator + local container + your existing Wrike account.

## Open questions / risks

- **Wrike `GET /attachments` shape**: assumed to support `createdDate=[from, to]` range filter and pagination. Verify in Phase 5; if absent, fall back to walking `folders → tasks → attachments` and apply date filtering client-side.
- **`AttachmentAdded` scope**: docs are ambiguous about whether it fires only for task attachments or also folder/comment. Step 3 handles both regardless; verify the actual event distribution during Phase 4.
- **Firestore "leasedUntil < now" with null**: documented above; we initialize to epoch zero rather than null.
- **LibreOffice rendering quality for XLSX/PPTX**: page-size, print-area, hidden sheets, slide notes all affect output. Phase 1's test fixtures should include a few real-world spreadsheets/decks to set baseline expectations.
- **Wrike permanent token**: never expires, broad scope. In v2, consider OAuth flow + refresh; for v1, Secret Manager + a rotation runbook is good enough.

## What we explicitly are not building

- Web UI / admin dashboard. (`admin.py` CLI is the v1 admin tool.)
- Auto-retry of `status='failed'` beyond `MAX_ATTEMPTS` (use `admin.py requeue`).
- Folder / comment-scoped attachments (marked `skipped:wrong_scope`).
- Non-Office file types (`.txt`, `.md`, `.pages`, `.numbers`, `.keynote`, `.eml`).
- Files > 50 MiB.
- Versioning of generated PDFs beyond the "supersede old preview" cleanup (no history of past previews).
- Metrics dashboards. Structured Cloud Logging is the v1 observability surface.
