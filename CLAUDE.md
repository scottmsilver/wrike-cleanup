# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Two related tools share this repo:

1. **`main.py`** — single-file CLI that reclaims Wrike storage by archiving attachments older than `--days_old_to_replace` (default 365 days) to Google Drive, replacing each with a half-resolution copy (for images/videos) and deleting the original. Uses `ffmpeg` via subprocess.

2. **`preview/`** — Cloud Run service that auto-generates PDF previews for Office attachments via Wrike webhook + Cloud Scheduler reconcile. State in Firestore, conversion via LibreOffice headless. Deployed via `preview/setup.py`. Operator runbook: `preview/SETUP.md`. Design: `docs/superpowers/specs/2026-05-23-wrike-preview-service-design.md`. Implementation plan: `docs/superpowers/plans/2026-05-23-wrike-preview-service.md`.

Both tools share `preview/wrike.py` as the Wrike v4 REST API client.

## Shared

### `preview/wrike.py` — Wrike API client (used by both tools)

`WrikeApi(config_file)` reads `{"WRIKE_API_TOKEN": "..."}` from disk and exposes:
- Original methods used by `main.py`: `list_workspaces`, `list_tasks_in_workspace`, `list_attachments_in_task`, `download_attachment`, `add_comment`, `add_file`, `delete_attachment`.
- Preview-service additions: `get_attachment` (returns None on 404), `download_attachment_by_id`, `add_file_to_task(task_id, file_path, upload_name=None)` (Path-coercing), `list_account_attachments(created_from, created_to, next_page_token=None)`, `delete_attachment_by_id`.

No retry, no rate-limit handling. Methods generally return raw `requests.Response` or parsed JSON `data`.

---

## `main.py` (storage archiver)

### Run

```
pip3 install -r requirements.txt
python main.py --do_nothing      # dry run
python main.py --no-do_nothing   # mutate Wrike + Drive
```

Flags (`argparse.BooleanOptionalAction` for `--do_nothing`):
- `--do_nothing` / `--no-do_nothing` — toggle mutation. Default is `None` (falsy), so a bare `python main.py` mutates. Pass `--do_nothing` explicitly for a true dry run.
- `--days_old_to_replace` (int, default 365).
- `--default_directory` (str, default `"Wrike Backup"`) — Google Drive folder name.
- `--wrike_config_json` (str, default `config.json`).

### Required local files (gitignored)

CWD must be writable. The CLI reads:
- `credentials.json` — Google Cloud OAuth client with Drive API enabled. Scope: `https://www.googleapis.com/auth/drive.file`.
- `config.json` — `{"WRIKE_API_TOKEN": "..."}`.
- `token.json` — written on first run after the local-server OAuth flow; cached + refreshed thereafter.

### System dependency

`ffmpeg` must be on `PATH`. Invoked via `subprocess.run` to half-scale `.jpg .jpeg .png .avi .mp4 .heic`. No fallback; if missing, the shrink step silently breaks but the original is still deleted.

### Architecture

- Imports `WrikeApi` from `preview.wrike` (extracted in Phase 0 of the preview service work; `main.py`'s behavior is unchanged from the original single-file version).
- `process_wrike` (top-level loop): workspaces → tasks → attachments, filter by `createdDate < now - days_old_to_replace` AND `'reduced' not in attachment['name']` (self-output guard). Errors caught + printed; loop continues.
- `process_attachment`: download → Drive upload → shareable link → Wrike comment → optional ffmpeg shrink + re-upload → delete original. Writes the original to CWD using its filename; cleans up in `finally`.
- `create_shareable_link` sets `type: anyone, role: reader` — backups are publicly readable to anyone with the link.
- `get_or_create_folder` has a shape inconsistency: on the *create* branch the code does `folder['id']`, on the *lookup* branch it does `folder[0]['id']` against `response.get('files', [])`. Both happen to work, but read carefully before touching.

---

## `preview/` (Wrike preview service)

A Cloud Run service that listens for Wrike `AttachmentAdded` webhooks, converts Office attachments to PDF via LibreOffice, and uploads them back as `<originalStem>_<attachmentId>_preview.pdf`. A Cloud Scheduler `/reconcile` route catches webhook misses and walks 10 years of historical attachments. Job state in Firestore.

### Architecture

Single Cloud Run service, four routes:

| Route | Caller | Auth | Behavior |
|---|---|---|---|
| `POST /webhook` | Wrike | HMAC of body (`X-Hook-Signature`). Public ingress because Wrike can't sign OIDC. | Handshake handler + per-event `create_job_if_absent`. Wrike sends events as a JSON **array**, not dict — handler normalizes both shapes. ALWAYS returns 200 (Wrike suspends on 4xx). |
| `POST /tick` | Cloud Scheduler (every 1m) | OIDC token (audience-checked against `OIDC_AUDIENCE`) + `X-Internal-Secret` header. Both required in prod. | Transactionally claim up to `BATCH_SIZE=3` pending jobs, run the conversion pipeline per job. |
| `POST /reconcile` | Cloud Scheduler (every 30m) | Same as `/tick`. | Bootstrap mode loops 28-day chunks backward until 240s or 100 Wrike-call budget hits, persisting `oldestCovered` after each chunk. Steady-state walks `[lastReconciledThrough - 1h, now]` with overlap. |
| `GET /healthz` | Cloud Run | None | Liveness. Returns "ok". |

### Per-job pipeline (`worker.py`)

1. Fetch attachment metadata — 404 → permanent `errorCode='deleted'` (no retry).
2. Persist scope/name/mime/size to Firestore.
3. Classify: scope filter → self-output guard → size preflight (>50 MiB → skip) → extension (Office → convert; image/PDF → skip).
4. Version cleanup: query Firestore for prior `done` jobs with same `taskId + originalName`, delete their previews, mark `superseded`.
5. Download → `soffice --headless -env:UserInstallation=file://<tmpdir>/lo-profile --convert-to pdf` (per-job profile for concurrency safety) → upload as `<stem>_<id>_preview.pdf`.
6. Mark `done` with `previewId` and `previewName`.

Permanent error codes (no retry): `deleted`, `password_protected`, `exhausted`, `too_large`, `wrong_scope`, `wrong_type`, `filename_is_preview`. Transient errors get re-leased after backoff (1: 5m, 2: 30m, 3: 2h, 4: 12h; cap at `MAX_ATTEMPTS=5`).

### Firestore schema

`jobs/{attachmentId}`: see `store.py`'s `create_job_if_absent` for the full shape. Key fields: `status`, `attempts`, `nextAttemptAt`, `leasedBy`, `leasedUntil` (epoch zero = unleased — using nullable would break the `<` query), `previewId`, `originalName`.

`state/reconcile`: singleton with `isBootstrap`, `oldestCovered`, `lastReconciledThrough`, `bootstrapFloor`, `updatedAt`.

Two composite indexes (declared in `preview/firestore.indexes.json`, created during setup): `(status, nextAttemptAt, leasedUntil)` for lease-claim query; `(taskId, originalName, status)` for version supersede.

### Run locally

```bash
cd preview && docker compose up           # Firestore emulator + service
python -m pytest preview/ -v              # 34 pass with emulator, 26 + 1 skip without
docker run --rm -v "$(pwd):/work" -w /work wrike-preview:dev \
  python preview/cli.py --from-permalink 721560302   # smoke test real Wrike round-trip
```

### Deploy

```bash
python preview/setup.py                   # interactive, idempotent
python preview/setup.py --dry-run         # print every gcloud command without running
```

10 GCP steps total (project, billing, APIs, Firestore + indexes, Secret Manager, service accounts, Artifact Registry + container build, Cloud Run deploy, IAM, Scheduler jobs, webhook registration). Full runbook: `preview/SETUP.md`.

Build cached via `preview/cloudbuild.yaml` — first build ~5-10 min for LibreOffice install, subsequent rebuilds ~30s.

### Operations

```bash
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py stats
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py list-failed
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py requeue <attachmentId>
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py reset-reconcile-state
gcloud logging read 'resource.type="cloud_run_revision"' --project=$PROJECT --limit=20
```

### Gotchas to know before touching

- **Wrike webhook events are JSON arrays**, not single objects. `server.py:webhook` normalizes; tests in `test_webhook_auth.py` cover the signature path. If you add a new route handler that reads the body, mirror the `isinstance(json_body, list)` branch.
- **`gcloud secrets versions add --data-file=-` stores stdin verbatim, newlines included.** `setup.py:_create_secret` strips whitespace; `server.py` strips again on read. Don't bypass either.
- **`@firestore.transactional` only works on standalone functions, not methods.** `_try_claim` lives at module-level in `store.py` for that reason.
- **Firestore queries can't use `IS NULL`.** `leasedUntil` is initialized to epoch zero so the `where("leasedUntil", "<", now)` query matches unleased jobs.
- **Two-pass deploy required**: the first `gcloud run deploy` returns the service URL; that URL must then be passed back as `OIDC_AUDIENCE` env var in a second deploy. `setup.py` does this automatically. If you redeploy manually, include `OIDC_AUDIENCE=https://...run.app` or `/tick`/`/reconcile` will silently reject Cloud Scheduler.
- **`/healthz` returns 404 from Cloud Run's frontend** in production despite being a real Flask route (root cause unknown; non-blocking because POST routes work and Cloud Run uses TCP probes for liveness). Don't trust HTTP-based health checks against this service.
- **The lint-guard hook** at `~/.claude/lint-guard.sh` runs `ruff format` on every Edit/Write. New files: fine. When touching existing files in this repo, expect imports to get re-sorted and code to be reformatted on save.
