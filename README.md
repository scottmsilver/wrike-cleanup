# wrike-cleanup

Two related tools for Wrike accounts:

| Tool | What it does |
|---|---|
| [`main.py`](#wrike-cleanup-storage-archiver) | One-shot CLI that reclaims Wrike storage by archiving attachments older than ~1 year to Google Drive, replacing each with a half-resolution copy (for images/videos), then deleting the original. |
| [`preview/`](preview/README.md) | Cloud Run service that auto-generates PDF previews for Office attachments (DOCX/XLSX/PPTX) so they preview natively in Wrike's UI. Real-time via webhook, with a 10-year backfill. |

Both share `preview/wrike.py` as the Wrike API client.

---

## wrike-cleanup (storage archiver)

Reduces Wrike storage usage by, for each attachment older than `--days_old_to_replace`:

- Uploads the original to Google Drive (NB: permissions on the backup file are wide open — see `create_shareable_link`).
- Posts a comment on the task with the Drive link.
- For images/videos, re-uploads a half-resolution copy via `ffmpeg`.
- Deletes the original.

### Install

```
pip3 install -r requirements.txt
```

Also requires `ffmpeg` on `PATH` for image/video shrinking.

### Configure

Two files in the working directory (both gitignored):

- **`credentials.json`** — OAuth client from a Google Cloud project with the Drive API enabled. Scope used: `https://www.googleapis.com/auth/drive.file`.
- **`config.json`** — `{"WRIKE_API_TOKEN": "..."}`. Override path with `--wrike_config_json`.

First run launches a local-server OAuth flow that writes `token.json` (cached + refreshed on subsequent runs). The CWD must be writable.

### Usage

```
python main.py --do_nothing      # dry run — list what would be archived
python main.py --no-do_nothing   # actually mutate Wrike + Drive
```

> Note: `--do_nothing` uses `argparse.BooleanOptionalAction` with no default, so a bare `python main.py` evaluates `do_nothing=None` (falsy) and **will** mutate. Always pass `--do_nothing` explicitly for a true dry run.

Flags:
- `--do_nothing` / `--no-do_nothing` — toggle mutation.
- `--days_old_to_replace` (int, default `365`).
- `--default_directory` (str, default `"Wrike Backup"`) — Google Drive folder name for archives.
- `--wrike_config_json` (str, default `config.json`).

---

## preview/ (PDF preview service)

See **[`preview/README.md`](preview/README.md)** for the overview, deploy steps, and operations.

Short version: deploy with `python preview/setup.py` (interactive, idempotent). After that the service runs unattended on Cloud Run, triggered by Wrike webhooks, with a Cloud Scheduler reconcile loop that catches missed events and walks historical attachments.

Spec and implementation plan live under [`docs/superpowers/`](docs/superpowers/).
