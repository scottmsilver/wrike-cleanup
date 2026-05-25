# Wrike Preview Service

A Cloud Run service that watches Wrike for new Office attachments (`.doc/.docx/.xls/.xlsx/.ppt/.pptx`) and uploads a sibling `<stem>_<id>_preview.pdf` so they preview natively in Wrike's UI. Real-time via webhook, with a self-bootstrapping reconciliation walk that backfills 10 years of historical attachments.

```
┌──────────┐   AttachmentAdded   ┌─────────────┐   enqueue  ┌──────────────┐
│  Wrike   │ ──────────────────► │  /webhook   │ ─────────► │  Firestore   │
└──────────┘                     └─────────────┘            │  jobs queue  │
     ▲                                                       └──────┬───────┘
     │ upload preview_*.pdf                                          │ /tick (every 1m)
     │                                                               ▼
     │      ┌──────────────────────────────────────────────────────────┐
     └──────┤ download → LibreOffice convert → upload as preview pdf  │
            └──────────────────────────────────────────────────────────┘
                                                                       ▲
                                                ┌────────────────┐    │ enqueue missed
                                                │  /reconcile    │────┘
                                                │  (every 30m)   │
                                                └────────────────┘
```

## Deploy

One command, idempotent, resumable:

```bash
python preview/setup.py
```

This walks 10 GCP setup steps interactively (project, billing, APIs, Firestore + indexes, Secret Manager, service accounts, container build, two-pass Cloud Run deploy, Scheduler jobs, Wrike webhook registration). Re-run any time — completed steps detect and skip.

Useful flags: `--dry-run`, `--project ID`, `--region us-central1`, `--billing-account ID`, `--register-webhook yes|no`, `--non-interactive` (for CI).

Full operator runbook: [`SETUP.md`](SETUP.md).

## Local dev

```bash
# Bring up the Firestore emulator + service
cd preview && docker compose up

# In another shell, run the test suite
python -m pytest preview/ -v
# 34 pass with emulator running; 26 + 1 skip without it.
```

Smoke test conversion in the image (needs a `preview/test-fixtures/sample.docx` — gitignored, see [`test-fixtures/README.md`](test-fixtures/README.md)):

```bash
docker run --rm -v "$(pwd)/preview:/app" wrike-preview:dev \
  python -c "from convert import convert_to_pdf; from pathlib import Path; \
             convert_to_pdf(Path('/app/test-fixtures/sample.docx'), Path('/tmp/out'))"
```

Smoke test a full Wrike round-trip against your account (writes a real PDF to a real task):

```bash
docker run --rm -v "$(pwd):/work" -w /work wrike-preview:dev \
  python preview/cli.py --from-permalink 721560302   # numeric ID from a Wrike URL
```

## Operations

After deploy, the service runs unattended. Two Cloud Scheduler jobs (`wrike-tick` every 1m, `wrike-reconcile` every 30m) drain the queue and catch webhook misses. Job state lives in Firestore.

Read-only inspection:

```bash
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py stats           # status counts
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py list-failed     # what broke

# Live log tail
gcloud logging read 'resource.type="cloud_run_revision" AND \
  resource.labels.service_name="preview"' --project=$PROJECT --limit=20
```

Manual interventions:

```bash
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py requeue <attachmentId>
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py reset-reconcile-state
```

## File layout

| File | Role |
|---|---|
| `server.py` | Flask app, four routes (`/webhook`, `/tick`, `/reconcile`, `/healthz`). |
| `worker.py` | `/tick` per-job pipeline: classify → version cleanup → download → convert → upload. |
| `reconcile.py` | Bootstrap walk + steady-state catch-up. Budget-bounded loop. |
| `store.py` | Firestore wrappers: `create_job_if_absent`, transactional lease claim, retry/done/failed/skip. |
| `convert.py` | LibreOffice subprocess wrapper with per-job profile + error classification. |
| `classify.py` | Pure-function extension/scope/size/self-output classifier. |
| `auth.py` | OIDC + internal-secret check on `/tick` and `/reconcile`. |
| `webhook_auth.py` | HMAC signature verify + handshake response for Wrike. |
| `backoff.py` | Retry schedule (1: 5m, 2: 30m, 3: 2h, 4: 12h; cap at 5 attempts). |
| `wrike.py` | Wrike v4 API client. Shared with the root-level `main.py` cleanup tool. |
| `cli.py` | One-off "convert one attachment now" tool, useful for smoke tests. |
| `admin.py` | Stats / list-failed / requeue / reset-reconcile-state. |
| `register_webhook.py` | One-off Wrike webhook registration helper. |
| `setup.py` | Interactive GCP deployment driver. |
| `Dockerfile` | Python 3.12 + LibreOffice + fonts. |
| `docker-compose.yml` | Local emulator stack for dev. |
| `cloudbuild.yaml` | `--cache-from` build config so rebuilds finish in ~30s. |
| `firestore.indexes.json` | Composite indexes (`status, nextAttemptAt, leasedUntil`) + supersede query. |

## How it works

Three guarantees the design defends:

1. **Idempotency** — webhook redeliveries and reconcile overlap never double-convert. Doc ID is the attachment ID; `create_job_if_absent` is atomic; terminal states (`done`/`failed`/`superseded`) never resurrect.
2. **Concurrency safety** — `/tick` claims jobs in a Firestore transaction with a 6-minute lease. Two concurrent ticks never grab the same job. Attempts increment at claim time, so an OOM-killed container still counts toward `MAX_ATTEMPTS=5`.
3. **At-least-once delivery** — Wrike's webhook is at-least-once and may drop; `/reconcile`'s durable `lastReconciledThrough` cursor with 1h overlap recovers from outages of any length.

Full design rationale + Codex review findings: [`docs/superpowers/specs/2026-05-23-wrike-preview-service-design.md`](../docs/superpowers/specs/2026-05-23-wrike-preview-service-design.md). Implementation phases + tests: [`docs/superpowers/plans/2026-05-23-wrike-preview-service.md`](../docs/superpowers/plans/2026-05-23-wrike-preview-service.md).
