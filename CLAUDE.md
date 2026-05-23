# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python CLI (`main.py`) that reclaims Wrike storage by archiving attachments older than `--days_old_to_replace` (default 365 days). For each old attachment it: downloads it from Wrike, uploads it to a Google Drive folder, posts a comment on the task linking to the Drive copy, re-uploads a half-resolution version to Wrike if it's an image/video (via `ffmpeg`), then deletes the original from Wrike.

## Run

```
pip3 install -r requirements.txt
python main.py                  # dry run-ish; see flag note below
python main.py --no-do_nothing  # actually mutate Wrike + Drive
```

Flags (`argparse.BooleanOptionalAction` for `--do_nothing`):
- `--do_nothing` / `--no-do_nothing` — toggle mutation. Default is `None` (falsy), so a bare `python main.py` will actually mutate; only `--do_nothing` is a true dry run. (README's claim that bare `python main.py` is a dry run is wrong.)
- `--days_old_to_replace` (int, default 365)
- `--default_directory` (str, default `"Wrike Backup"`) — Google Drive folder name to create/use for archives
- `--wrike_config_json` (str, default `config.json`)

No test suite, lint config, or build step exists.

## Required local files (all gitignored)

The script reads these from the current working directory:
- `credentials.json` — OAuth client from a Google Cloud project with the Drive API enabled. Scope used: `https://www.googleapis.com/auth/drive.file`.
- `config.json` — `{"WRIKE_API_TOKEN": "..."}`. Path overridable via `--wrike_config_json`.
- `token.json` — written on first run after the OAuth local-server flow; cached and refreshed on subsequent runs.

The CWD must be writable (token.json + temp downloads land there).

## System dependency

`ffmpeg` must be on `PATH`. It's invoked via `subprocess.run` to half-scale images/videos with extensions `.jpg .jpeg .png .avi .mp4 .heic`. There is no Python-level check or fallback — if ffmpeg is missing or fails, the reduced re-upload step silently breaks but the original is still deleted.

## Architecture notes

- `WrikeApi` (main.py:16) wraps the Wrike v4 REST API (`https://www.wrike.com/api/v4`) with bearer-token auth. All methods return raw `requests.Response` or parsed JSON `data`; there is no retry, pagination, or rate-limit handling (despite the README listing a `--wrike_api_rate_limit` flag that does not actually exist in the code).
- `process_wrike` (main.py:171) is the top-level loop: enumerate workspaces → tasks → attachments, filter by `createdDate < now - days_old_to_replace` AND `'reduced' not in attachment['name']` (the latter is how the script avoids re-processing its own previous output). Errors at any level are caught and printed; the loop continues.
- `process_attachment` (main.py:113) executes the 7-step pipeline (download → Drive upload → shareable link → Wrike comment → optional ffmpeg shrink + re-upload → delete original). It writes the original to CWD with the attachment's original name and cleans it up in `finally`.
- `create_shareable_link` (main.py:103) sets `type: anyone, role: reader` on the uploaded Drive file — README explicitly notes this means permissions on backups are wide open.
- `get_or_create_folder` (main.py:153) has a known shape bug: when the folder is *created*, `service.files().create(...).execute()` returns a dict, but the code then does `folder[0]['id'] if isinstance(folder, list) else folder['id']` — the create branch falls through to `folder['id']` which works, but the lookup branch indexes into `response.get('files', [])`. Touch carefully when changing folder logic.
