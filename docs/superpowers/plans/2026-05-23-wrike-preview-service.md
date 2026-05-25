# Wrike Preview Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Cloud Run service that auto-generates PDF previews for Office attachments (DOCX/XLSX/PPTX) on Wrike tasks, triggered by Wrike webhooks and reconciled by Cloud Scheduler.

**Architecture:** Single Cloud Run service with four routes (`/webhook` public + `/tick` + `/reconcile` + `/healthz`). Wrike webhook enqueues jobs to Firestore; Cloud Scheduler drains the queue via `/tick`. Conversion uses LibreOffice headless with per-job profiles for concurrency safety. Reconciliation walks both forward (steady-state with durable cursor) and backward (bootstrap to 10-year floor).

**Tech Stack:** Python 3.12, Flask, Firestore (Native + emulator for local dev), LibreOffice headless, Docker Compose, Google Cloud (Run, Scheduler, Secret Manager, Artifact Registry), cloudflared (for Phase 4 tunnel).

**Spec:** `docs/superpowers/specs/2026-05-23-wrike-preview-service-design.md` — read this first if any ambiguity arises.

---

## Phase 0 — Extract WrikeApi (refactor)

**Goal:** Move the `WrikeApi` class out of `main.py` into a shared `wrike.py` module without changing any behavior. The new preview service needs the same Wrike client, and shipping it together avoids duplication.

### Task 1: Move WrikeApi to its own module

**Files:**
- Create: `wrike.py`
- Modify: `main.py:1-69`

- [ ] **Step 1: Read the existing `main.py` carefully to identify the exact class boundary**

The class spans lines 16-69 of `main.py`. The `import json`, `import requests` imports at the top are also needed by it.

- [ ] **Step 2: Create `wrike.py` with the extracted class**

```python
import json
import requests


class WrikeApi:
    def __init__(self, config_file):
        with open(config_file, 'r') as fh:
            config = json.load(fh)

        self.WRIKE_API_TOKEN = config["WRIKE_API_TOKEN"]
        self.WRIKE_BASE_URL = "https://www.wrike.com/api/v4"
        self.WRIKE_DEFAULT_HEADERS = {
            "Authorization": f"Bearer {self.WRIKE_API_TOKEN}",
        }

    def list_workspaces(self):
        url = f"{self.WRIKE_BASE_URL}/folders"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        print(data)
        return data['data']

    def list_tasks_in_workspace(self, workspace_id):
        url = f"{self.WRIKE_BASE_URL}/folders/{workspace_id}/tasks"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        return data['data']

    def list_attachments_in_task(self, task_id):
        url = f"{self.WRIKE_BASE_URL}/tasks/{task_id}/attachments"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        data = json.loads(response.text)
        return data['data']

    def download_attachment(self, attachment):
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}/download"
        return requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, stream=True)

    def add_comment(self, attachment, comment_text):
        url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/comments"
        return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, json={"text": comment_text})

    def add_file(self, attachment, new_filename):
        url = f"{self.WRIKE_BASE_URL}/tasks/{attachment['taskId']}/attachments"
        with open(new_filename, 'rb') as f:
            files = {'file': (new_filename, f)}
            return requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, files=files)

    def delete_attachment(self, attachment):
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment['id']}"
        return requests.delete(url, headers=self.WRIKE_DEFAULT_HEADERS)
```

- [ ] **Step 3: Modify `main.py` to import from `wrike`**

In `main.py`, delete lines 16-69 (the `WrikeApi` class) and add at the top of the file (after the existing imports):

```python
from wrike import WrikeApi
```

The line `import requests` in `main.py` is no longer needed by `main.py` itself (only the class used it). Remove it from `main.py`.

- [ ] **Step 4: Verify `main.py` still parses**

Run: `python -c "import main"`
Expected: no output, exit code 0. If it errors, the import path or remaining references to `WrikeApi` are wrong.

- [ ] **Step 5: Verify dry-run still works**

Run: `python main.py --do_nothing` (or interrupt with Ctrl-C if it starts hitting Wrike — we only want to verify the imports load).
Expected: prints `Do nothing.` lines or workspace titles — the same shape of output as before the refactor. If it errors before reaching the Wrike API, fix the import.

- [ ] **Step 6: Commit**

```bash
git add wrike.py main.py
git commit -m "Extract WrikeApi to wrike.py for sharing with preview service"
```

---

## Phase 1 — Conversion core (local-only, no Wrike, no Firestore)

**Goal:** Build a pure conversion function that turns an Office file on disk into a PDF on disk, with classified error codes. Test it inside a Docker container that mirrors what Cloud Run will run.

### Task 2: Scaffold the preview/ directory

**Files:**
- Create: `preview/__init__.py`
- Create: `preview/requirements.txt`

- [ ] **Step 1: Create the package marker**

Create empty file `preview/__init__.py`.

- [ ] **Step 2: Create requirements.txt with v1 deps**

Create `preview/requirements.txt`:

```
Flask==3.0.3
google-cloud-firestore==2.18.0
google-auth==2.34.0
requests==2.32.3
pytest==8.3.3
```

(Note: `google-cloud-firestore` brings the emulator-aware client. `google-auth` provides the OIDC verifier used in Phase 6. `pytest` ships in the image because the same image runs the test suite during development phases.)

- [ ] **Step 3: Commit**

```bash
git add preview/__init__.py preview/requirements.txt
git commit -m "Scaffold preview package"
```

### Task 3: Implement `convert.py` error-code classifier (TDD)

**Files:**
- Create: `preview/convert.py`
- Create: `preview/test_convert.py`

- [ ] **Step 1: Write the failing test for stderr classification**

Create `preview/test_convert.py`:

```python
from convert import classify_soffice_error


def test_classify_password_protected():
    stderr = "Error: Password to open required for /tmp/foo.docx"
    assert classify_soffice_error(returncode=1, stderr=stderr) == "password_protected"


def test_classify_timeout():
    assert classify_soffice_error(returncode=124, stderr="") == "soffice_timeout"


def test_classify_crash():
    assert classify_soffice_error(returncode=139, stderr="Segmentation fault") == "soffice_crash"


def test_classify_no_output():
    # returncode 0 but no PDF produced - caller passes a sentinel
    assert classify_soffice_error(returncode=0, stderr="") == "soffice_no_output"


def test_classify_unknown_nonzero():
    assert classify_soffice_error(returncode=1, stderr="some other error") == "soffice_crash"
```

- [ ] **Step 2: Run the test to confirm it fails with import error**

Run: `cd preview && python -m pytest test_convert.py -v`
Expected: `ModuleNotFoundError: No module named 'convert'`

- [ ] **Step 3: Write the minimal implementation**

Create `preview/convert.py`:

```python
import subprocess
from pathlib import Path


def classify_soffice_error(returncode: int, stderr: str) -> str:
    """Classify a LibreOffice failure into a permanent or transient errorCode.

    Permanent codes (caller should not retry): password_protected.
    Transient codes (caller may retry per backoff): soffice_timeout, soffice_crash, soffice_no_output.
    """
    if returncode == 124:
        return "soffice_timeout"
    if "password" in stderr.lower() and "open" in stderr.lower():
        return "password_protected"
    if returncode == 0:
        return "soffice_no_output"
    return "soffice_crash"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd preview && python -m pytest test_convert.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add preview/convert.py preview/test_convert.py
git commit -m "Add LibreOffice error classifier"
```

### Task 4: Implement `convert_to_pdf()` with per-job profile

**Files:**
- Modify: `preview/convert.py`
- Modify: `preview/test_convert.py`

- [ ] **Step 1: Add the wrapper function**

Append to `preview/convert.py`:

```python
class ConvertError(Exception):
    def __init__(self, error_code: str, stderr: str = ""):
        self.error_code = error_code
        self.stderr = stderr
        super().__init__(f"{error_code}: {stderr[:200]}")


def convert_to_pdf(input_path: Path, work_dir: Path, timeout_s: int = 120) -> Path:
    """Convert an Office file to PDF in work_dir. Returns the PDF path on success.

    Raises ConvertError with .error_code in {soffice_timeout, password_protected,
    soffice_no_output, soffice_crash} on failure.

    Uses a per-job LibreOffice user profile under work_dir/lo-profile, so concurrent
    invocations in the same container do not collide on the default profile.
    """
    input_path = Path(input_path)
    work_dir = Path(work_dir)
    profile_dir = work_dir / "lo-profile"
    profile_url = f"file://{profile_dir.resolve()}"

    cmd = [
        "soffice",
        "--headless",
        f"-env:UserInstallation={profile_url}",
        "--norestore",
        "--nofirststartwizard",
        "--convert-to", "pdf",
        "--outdir", str(work_dir),
        str(input_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        raise ConvertError("soffice_timeout", e.stderr or "") from e

    expected_pdf = work_dir / (input_path.stem + ".pdf")

    if result.returncode != 0:
        raise ConvertError(classify_soffice_error(result.returncode, result.stderr), result.stderr)

    if not expected_pdf.exists():
        raise ConvertError("soffice_no_output", result.stderr)

    return expected_pdf
```

- [ ] **Step 2: Add a stub test that confirms the function imports**

Append to `preview/test_convert.py`:

```python
def test_convert_to_pdf_importable():
    from convert import convert_to_pdf  # noqa: F401
```

- [ ] **Step 3: Run tests**

Run: `cd preview && python -m pytest test_convert.py -v`
Expected: 6 passed. (We don't run a real conversion here — that needs the container.)

- [ ] **Step 4: Commit**

```bash
git add preview/convert.py preview/test_convert.py
git commit -m "Add convert_to_pdf with per-job LibreOffice profile"
```

### Task 5: Add Dockerfile with LibreOffice + fonts

**Files:**
- Create: `preview/Dockerfile`
- Create: `preview/.dockerignore`

- [ ] **Step 1: Create `.dockerignore`**

```
__pycache__
*.pyc
.pytest_cache
test-fixtures
*.md
docker-compose.yml
```

- [ ] **Step 2: Create `Dockerfile`**

```dockerfile
FROM python:3.12-slim

# LibreOffice for Office → PDF conversion.
# Fonts: liberation (Times/Arial substitutes), dejavu (broad coverage),
# noto-cjk (Asian scripts) — without these, conversions render in fallback faces.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-liberation \
    fonts-dejavu \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=5000
EXPOSE 5000

# Default command is the Flask server (configured in Task 17). Phase 1
# overrides this with `docker run ... python -m pytest` for testing.
CMD ["python", "server.py"]
```

- [ ] **Step 3: Build the image**

Run: `cd preview && docker build -t wrike-preview:dev .`
Expected: build succeeds. First build takes 3-5 min (LibreOffice install). Subsequent builds use cache.

- [ ] **Step 4: Verify LibreOffice is in the image**

Run: `docker run --rm wrike-preview:dev soffice --version`
Expected: `LibreOffice X.Y.Z ...` (Debian's packaged version).

- [ ] **Step 5: Commit**

```bash
git add preview/Dockerfile preview/.dockerignore
git commit -m "Add Dockerfile for preview service with LibreOffice"
```

### Task 6: Smoke-test conversion in the container

**Files:**
- Create: `preview/test-fixtures/README.md` (instructions for what to put here)

- [ ] **Step 1: Document expected fixtures**

Create `preview/test-fixtures/README.md`:

```markdown
# Test fixtures

This directory is gitignored. Place small test files here before running Phase 1
smoke tests:

- `sample.docx` — a small (<100KB) Word document with mixed text + a table.
- `sample.xlsx` — a 2-sheet workbook with formulas.
- `sample.pptx` — a 3-slide deck with text + one image.
- `password.docx` — a Word doc protected with a password.
- `corrupt.docx` — a deliberately-mangled .docx (e.g., truncate the file).
- `huge.docx` — a 50MB+ DOCX (for Phase 3's size-preflight test).
- `not-office.zip` — any random zip file (for Phase 3's wrong-type test).
```

- [ ] **Step 2: Add the fixture dir to .gitignore**

Modify the root `.gitignore`, appending:

```
preview/test-fixtures/*
!preview/test-fixtures/README.md
```

- [ ] **Step 3: Place at least `sample.docx` in the fixtures dir**

This is a manual step — drop a small Word file into `preview/test-fixtures/sample.docx`. Any small DOCX works; you can create one in Wrike itself.

- [ ] **Step 4: Run conversion inside the container**

Run:
```bash
docker run --rm -v "$(pwd)/preview:/app" wrike-preview:dev \
  python -c "
from pathlib import Path
from convert import convert_to_pdf
pdf = convert_to_pdf(Path('/app/test-fixtures/sample.docx'), Path('/tmp/out'))
print('OK', pdf)
"
```

Expected: `OK /tmp/out/sample.pdf` and no traceback. The PDF stays inside the container (we used `/tmp/out`); we just want to confirm conversion works.

- [ ] **Step 5: Repeat for xlsx and pptx if you have fixtures**

Same command, substitute file. Each should produce a PDF.

- [ ] **Step 6: Commit fixture instructions**

```bash
git add preview/test-fixtures/README.md .gitignore
git commit -m "Document test fixtures for preview service"
```

---

## Phase 2 — Wrike round-trip CLI (no Firestore yet)

**Goal:** Prove the full pipeline end-to-end against a real Wrike attachment: download from Wrike, convert, upload back. This validates the Wrike API surface area before any complexity from Firestore or webhooks is added.

### Task 7: Add Wrike convenience methods needed by the preview service

**Files:**
- Modify: `wrike.py`

- [ ] **Step 1: Add `get_attachment`, `list_account_attachments`, and rename-friendly `add_file` to `WrikeApi`**

Append these methods inside the `WrikeApi` class in `wrike.py`:

```python
    def get_attachment(self, attachment_id):
        """Return the attachment metadata, or None on 404."""
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}"
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()['data'][0]

    def download_attachment_by_id(self, attachment_id):
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}/download"
        return requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, stream=True)

    def add_file_to_task(self, task_id, file_path, upload_name=None):
        """Upload a file to a task with an optional override filename. Returns the new
        attachment's id."""
        upload_name = upload_name or file_path.name
        url = f"{self.WRIKE_BASE_URL}/tasks/{task_id}/attachments"
        with open(file_path, 'rb') as fh:
            files = {'file': (upload_name, fh)}
            response = requests.post(url, headers=self.WRIKE_DEFAULT_HEADERS, files=files)
        response.raise_for_status()
        return response.json()['data'][0]['id']

    def list_account_attachments(self, created_from, created_to, next_page_token=None):
        """List attachments in the account in a date range. Returns (items, nextPageToken)."""
        url = f"{self.WRIKE_BASE_URL}/attachments"
        params = {
            'createdDate': '{"start":"' + created_from + '","end":"' + created_to + '"}',
        }
        if next_page_token:
            params['nextPageToken'] = next_page_token
        response = requests.get(url, headers=self.WRIKE_DEFAULT_HEADERS, params=params)
        response.raise_for_status()
        body = response.json()
        return body['data'], body.get('responseNextPageToken')

    def delete_attachment_by_id(self, attachment_id):
        url = f"{self.WRIKE_BASE_URL}/attachments/{attachment_id}"
        return requests.delete(url, headers=self.WRIKE_DEFAULT_HEADERS)
```

Note: the date filter shape (`{"start":"...","end":"..."}` as a JSON string in a query param) is from Wrike's API conventions. If `list_account_attachments` returns errors during Phase 5, check the actual format in Wrike's API docs and adjust.

- [ ] **Step 2: Verify import still works**

Run: `python -c "from wrike import WrikeApi"`
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add wrike.py
git commit -m "Add Wrike API methods needed by preview service"
```

### Task 8: Implement the one-off `cli.py`

**Files:**
- Create: `preview/cli.py`

- [ ] **Step 1: Write the CLI**

```python
"""One-off Wrike attachment → PDF preview tool.

Usage:
    python preview/cli.py --attachment-id IEAAAAAB

Requires:
    - config.json in the working directory with the Wrike API token (same as main.py).
    - LibreOffice on PATH (or run inside the preview Docker image).
"""

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # so we can import wrike

from wrike import WrikeApi
from convert import convert_to_pdf, ConvertError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attachment-id", required=True)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    wrike = WrikeApi(args.config)

    meta = wrike.get_attachment(args.attachment_id)
    if meta is None:
        print(f"Attachment {args.attachment_id} not found (404).", file=sys.stderr)
        sys.exit(2)

    task_id = meta.get("taskId")
    if not task_id:
        print(f"Attachment is not task-scoped (scope: {meta.get('scope')}).", file=sys.stderr)
        sys.exit(3)

    name = meta["name"]
    print(f"Converting attachment {args.attachment_id} (name={name}, task={task_id})...")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / name

        # Download
        response = wrike.download_attachment_by_id(args.attachment_id)
        response.raise_for_status()
        with open(input_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=65536):
                fh.write(chunk)
        print(f"  downloaded {input_path.stat().st_size} bytes")

        # Convert
        try:
            pdf_path = convert_to_pdf(input_path, tmp_path)
        except ConvertError as e:
            print(f"  conversion failed: {e.error_code} -- {e.stderr[:200]}", file=sys.stderr)
            sys.exit(4)
        print(f"  converted to {pdf_path.name} ({pdf_path.stat().st_size} bytes)")

        # Upload
        upload_name = f"preview_{args.attachment_id}.pdf"
        new_id = wrike.add_file_to_task(task_id, pdf_path, upload_name=upload_name)
        print(f"  uploaded as {upload_name} (new attachment id: {new_id})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Build the image again (to pick up new files)**

Run: `cd preview && docker build -t wrike-preview:dev .`
Expected: cached layers used; quick.

- [ ] **Step 3: Run the CLI against a real attachment**

**Manual step.** Find a small DOCX attachment in your Wrike test workspace. Get its attachment ID from Wrike's URL or via `wrike.list_attachments_in_task()`.

Run:
```bash
docker run --rm \
  -v "$(pwd):/work" -w /work \
  wrike-preview:dev \
  python preview/cli.py --attachment-id <YOUR_ATTACHMENT_ID>
```

Expected: prints "Converting...", "downloaded N bytes", "converted to X.pdf (N bytes)", "uploaded as preview_<ID>.pdf (new attachment id: ...)".

- [ ] **Step 4: Verify in Wrike's UI**

Open the task in Wrike. Confirm the new `preview_<ID>.pdf` attachment is present and previews in the UI.

- [ ] **Step 5: Commit**

```bash
git add preview/cli.py
git commit -m "Add Wrike round-trip CLI for preview generation"
```

---

## Phase 3 — Firestore + lease semantics + local end-to-end

**Goal:** Stand up the full service against a local Firestore emulator. Webhook + tick + reconcile all work locally; idempotency and lease semantics are unit-tested.

### Task 9: Add docker-compose for the emulator stack

**Files:**
- Create: `preview/docker-compose.yml`

- [ ] **Step 1: Write the compose file**

```yaml
services:
  firestore:
    image: gcr.io/google.com/cloudsdktool/google-cloud-cli:emulators
    command: >
      gcloud emulators firestore start
      --host-port=0.0.0.0:8080
      --database-mode=firestore-native
    ports:
      - "8080:8080"

  app:
    build: .
    environment:
      FIRESTORE_EMULATOR_HOST: firestore:8080
      GOOGLE_CLOUD_PROJECT: local-dev
      WRIKE_API_TOKEN: "${WRIKE_API_TOKEN}"
      WEBHOOK_SIGNING_SECRET: "devsecret"
      INTERNAL_SECRET: "devinternal"
      DEV_MODE: "1"
    ports:
      - "5000:5000"
    depends_on:
      - firestore
    volumes:
      - ./:/app
```

`DEV_MODE=1` will be checked by `auth.py` to skip OIDC verification (Phase 6 adds the real check).

- [ ] **Step 2: Bring it up to verify the emulator starts**

Run:
```bash
cd preview
WRIKE_API_TOKEN=$(jq -r .WRIKE_API_TOKEN ../config.json) docker compose up --build
```

Expected: `firestore` logs `Dev App Server is now running` and `app` logs an import error (because `server.py` doesn't exist yet). Ctrl-C to stop.

- [ ] **Step 3: Commit**

```bash
git add preview/docker-compose.yml
git commit -m "Add docker-compose with Firestore emulator for local dev"
```

### Task 10: Implement `classify.py` (pure functions, TDD)

**Files:**
- Create: `preview/classify.py`
- Create: `preview/test_classify.py`

- [ ] **Step 1: Write the failing tests**

```python
from classify import classify_attachment, ClassifyResult


def _attach(name, mime=None, size=1000, scope="task", task_id="T1"):
    return {
        "name": name,
        "mimeType": mime,
        "size": size,
        "scope": scope,
        "taskId": task_id,
    }


def test_office_docx_is_convert():
    r = classify_attachment(_attach("report.docx"))
    assert r == ClassifyResult(action="convert", reason=None)


def test_pdf_is_skip_already_previewable():
    r = classify_attachment(_attach("doc.pdf"))
    assert r == ClassifyResult(action="skip", reason="already_previewable")


def test_image_is_skip_already_previewable():
    r = classify_attachment(_attach("photo.jpg"))
    assert r == ClassifyResult(action="skip", reason="already_previewable")


def test_preview_filename_is_skip_self_output():
    r = classify_attachment(_attach("preview_IEAAA12345.pdf"))
    assert r == ClassifyResult(action="skip", reason="filename_is_preview")


def test_oversize_is_skip_too_large():
    r = classify_attachment(_attach("big.docx", size=60 * 1024 * 1024))
    assert r == ClassifyResult(action="skip", reason="too_large")


def test_text_file_is_skip_wrong_type():
    r = classify_attachment(_attach("notes.txt"))
    assert r == ClassifyResult(action="skip", reason="wrong_type")


def test_comment_scope_is_skip_wrong_scope():
    r = classify_attachment(_attach("report.docx", scope="comment"))
    assert r == ClassifyResult(action="skip", reason="wrong_scope")


def test_folder_scope_is_skip_wrong_scope():
    r = classify_attachment(_attach("report.docx", scope="folder"))
    assert r == ClassifyResult(action="skip", reason="wrong_scope")


def test_preview_guard_fires_before_extension():
    # A user could upload "preview_foo.docx" — the self-output guard must
    # win over the extension-based convert path.
    r = classify_attachment(_attach("preview_foo.docx"))
    # Our preview_*.pdf regex won't match preview_foo.docx, so this falls
    # through to the docx-convert branch. That's fine — the guard targets
    # only our actual outputs (preview_<id>.pdf), not arbitrary preview_*.
    assert r == ClassifyResult(action="convert", reason=None)
```

- [ ] **Step 2: Confirm tests fail**

Run: `cd preview && python -m pytest test_classify.py -v`
Expected: `ModuleNotFoundError: No module named 'classify'`

- [ ] **Step 3: Implement `classify.py`**

```python
import re
from dataclasses import dataclass
from typing import Literal, Optional


PREVIEW_FILENAME_RE = re.compile(r"^preview_[A-Z0-9]+\.pdf$", re.IGNORECASE)

CONVERTIBLE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
ALREADY_PREVIEWABLE_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".heic", ".svg", ".tiff", ".psd",
}

MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MiB


@dataclass(frozen=True)
class ClassifyResult:
    action: Literal["convert", "skip"]
    reason: Optional[str]   # one of the skippedReason values, or None for convert


def classify_attachment(attachment: dict) -> ClassifyResult:
    """Classify a Wrike attachment.

    Order of checks matters: self-output guard fires before extension check
    (so a user-uploaded preview_<id>.pdf is skipped instead of looping).
    """
    name: str = attachment["name"]
    scope: str = attachment.get("scope", "task")
    size: int = attachment.get("size") or 0

    # 1. Scope filter
    if scope != "task":
        return ClassifyResult(action="skip", reason="wrong_scope")

    # 2. Self-output guard
    if PREVIEW_FILENAME_RE.match(name):
        return ClassifyResult(action="skip", reason="filename_is_preview")

    # 3. Size preflight
    if size > MAX_SIZE_BYTES:
        return ClassifyResult(action="skip", reason="too_large")

    # 4. Extension
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ALREADY_PREVIEWABLE_EXTS:
        return ClassifyResult(action="skip", reason="already_previewable")
    if ext in CONVERTIBLE_EXTS:
        return ClassifyResult(action="convert", reason=None)
    return ClassifyResult(action="skip", reason="wrong_type")
```

- [ ] **Step 4: Run tests**

Run: `cd preview && python -m pytest test_classify.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add preview/classify.py preview/test_classify.py
git commit -m "Add attachment classifier with self-output guard"
```

### Task 11: Implement `store.py` skeleton with `create_job_if_absent`

**Files:**
- Create: `preview/store.py`
- Create: `preview/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
import os
import time
from datetime import datetime, timezone

import pytest

# Tests rely on the Firestore emulator. Set FIRESTORE_EMULATOR_HOST=localhost:8080
# before running (or run inside docker compose).
if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
    pytest.skip("FIRESTORE_EMULATOR_HOST not set", allow_module_level=True)

from google.cloud import firestore

from store import Store


@pytest.fixture
def store():
    db = firestore.Client(project="local-dev")
    # Clean the jobs collection between tests
    for doc in db.collection("jobs").stream():
        doc.reference.delete()
    return Store(db)


def test_create_job_if_absent_inserts_new_doc(store):
    created = store.create_job_if_absent("attA", task_id="T1")
    assert created is True
    doc = store.get_job("attA")
    assert doc["status"] == "pending"
    assert doc["taskId"] == "T1"
    assert doc["attempts"] == 0


def test_create_job_if_absent_does_not_resurrect_done(store):
    store.create_job_if_absent("attB", task_id="T1")
    # Simulate prior completion
    store.mark_done("attB", preview_id="PA", preview_name="preview_attB.pdf")

    created = store.create_job_if_absent("attB", task_id="T1")
    assert created is False
    doc = store.get_job("attB")
    assert doc["status"] == "done"   # NOT resurrected
    assert doc["previewId"] == "PA"


def test_create_job_if_absent_does_not_resurrect_failed(store):
    store.create_job_if_absent("attC", task_id="T1")
    store.mark_failed("attC", error="boom", error_code="exhausted")

    created = store.create_job_if_absent("attC", task_id="T1")
    assert created is False
    doc = store.get_job("attC")
    assert doc["status"] == "failed"
```

- [ ] **Step 2: Run to confirm failure**

Run (with emulator up via `docker compose up firestore`):
```bash
FIRESTORE_EMULATOR_HOST=localhost:8080 cd preview && python -m pytest test_store.py -v
```
Expected: `ModuleNotFoundError: No module named 'store'`.

- [ ] **Step 3: Implement `store.py`**

```python
from datetime import datetime, timezone, timedelta
from typing import Optional

from google.cloud import firestore


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Store:
    def __init__(self, db: firestore.Client):
        self.db = db

    # -------- jobs --------

    def jobs(self):
        return self.db.collection("jobs")

    def get_job(self, attachment_id: str) -> Optional[dict]:
        snap = self.jobs().document(attachment_id).get()
        return snap.to_dict() if snap.exists else None

    def create_job_if_absent(self, attachment_id: str, *, task_id: Optional[str]) -> bool:
        """Atomically insert a pending job. Returns True if inserted, False if
        a doc already existed (any status — never overwrite)."""
        ref = self.jobs().document(attachment_id)
        try:
            now = _now()
            ref.create({
                "taskId": task_id,
                "scope": None,
                "originalName": None,
                "mimeType": None,
                "sizeBytes": None,
                "status": "pending",
                "skippedReason": None,
                "error": None,
                "errorCode": None,
                "previewId": None,
                "previewName": None,
                "attempts": 0,
                "nextAttemptAt": now,
                "leasedBy": None,
                "leasedUntil": EPOCH,
                "createdAt": now,
                "updatedAt": now,
            })
            return True
        except Exception as e:
            # Firestore raises google.cloud.exceptions.AlreadyExists when the doc exists.
            if e.__class__.__name__ == "AlreadyExists":
                return False
            raise

    def mark_done(self, attachment_id: str, *, preview_id: str, preview_name: str):
        self.jobs().document(attachment_id).update({
            "status": "done",
            "previewId": preview_id,
            "previewName": preview_name,
            "leasedBy": None,
            "leasedUntil": EPOCH,
            "updatedAt": _now(),
        })

    def mark_failed(self, attachment_id: str, *, error: str, error_code: str):
        self.jobs().document(attachment_id).update({
            "status": "failed",
            "error": error[:500],
            "errorCode": error_code,
            "leasedBy": None,
            "leasedUntil": EPOCH,
            "updatedAt": _now(),
        })

    def mark_skipped(self, attachment_id: str, *, reason: str):
        self.jobs().document(attachment_id).update({
            "status": "skipped",
            "skippedReason": reason,
            "leasedBy": None,
            "leasedUntil": EPOCH,
            "updatedAt": _now(),
        })
```

- [ ] **Step 4: Run tests**

With the Firestore emulator already running from `docker compose up firestore`:

```bash
FIRESTORE_EMULATOR_HOST=localhost:8080 GOOGLE_CLOUD_PROJECT=local-dev \
  cd preview && python -m pytest test_store.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add preview/store.py preview/test_store.py
git commit -m "Add Store with create-if-absent semantics for jobs"
```

### Task 12: Add lease semantics to `store.py` (TDD)

**Files:**
- Modify: `preview/store.py`
- Modify: `preview/test_store.py`

- [ ] **Step 1: Add tests for `claim_next_pending` and lease behavior**

Append to `preview/test_store.py`:

```python
def test_claim_next_pending_returns_a_job(store):
    store.create_job_if_absent("attA", task_id="T1")
    claimed = store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60)
    assert claimed is not None
    assert claimed["attachmentId"] == "attA"
    assert claimed["leasedBy"] == "worker1"
    assert claimed["attempts"] == 1


def test_claim_skips_already_leased(store):
    store.create_job_if_absent("attA", task_id="T1")
    store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60)
    second = store.claim_next_pending(claimer_id="worker2", lease_ttl_s=60)
    assert second is None


def test_claim_picks_up_expired_lease(store):
    store.create_job_if_absent("attA", task_id="T1")
    store.claim_next_pending(claimer_id="worker1", lease_ttl_s=-1)  # already expired
    second = store.claim_next_pending(claimer_id="worker2", lease_ttl_s=60)
    assert second is not None
    assert second["leasedBy"] == "worker2"
    assert second["attempts"] == 2   # incremented again at re-claim


def test_claim_skips_terminal_states(store):
    store.create_job_if_absent("attA", task_id="T1")
    store.mark_done("attA", preview_id="P1", preview_name="preview_attA.pdf")
    assert store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60) is None


def test_release_lease_for_retry(store):
    store.create_job_if_absent("attA", task_id="T1")
    claimed = store.claim_next_pending(claimer_id="worker1", lease_ttl_s=60)
    store.release_for_retry("attA", next_attempt_in_s=5, error="boom", error_code="soffice_crash")
    job = store.get_job("attA")
    assert job["status"] == "pending"
    assert job["leasedBy"] is None
    assert job["errorCode"] == "soffice_crash"
```

- [ ] **Step 2: Implement the lease methods**

The `claim_next_pending`, `release_for_retry`, and `update_job_metadata` methods go inside the existing `Store` class (after `mark_skipped`). The `_try_claim` function is a **module-level helper** outside the class — that's required because `@firestore.transactional` works on standalone functions, not methods. Add both:

```python
    def claim_next_pending(self, *, claimer_id: str, lease_ttl_s: int) -> Optional[dict]:
        """Atomically claim one pending+eligible job. Returns the job dict
        (with attachmentId added) or None if no eligible jobs exist."""
        now = _now()
        candidates = (
            self.jobs()
            .where("status", "==", "pending")
            .where("nextAttemptAt", "<=", now)
            .where("leasedUntil", "<", now)
            .order_by("nextAttemptAt")
            .limit(5)
            .stream()
        )

        for snap in candidates:
            transaction = self.db.transaction()
            result = _try_claim(transaction, snap.reference, claimer_id, lease_ttl_s)
            if result is not None:
                result["attachmentId"] = snap.id
                return result
        return None

    def release_for_retry(self, attachment_id: str, *, next_attempt_in_s: int,
                          error: str, error_code: str):
        self.jobs().document(attachment_id).update({
            "status": "pending",
            "leasedBy": None,
            "leasedUntil": EPOCH,
            "nextAttemptAt": _now() + timedelta(seconds=next_attempt_in_s),
            "error": error[:500],
            "errorCode": error_code,
            "updatedAt": _now(),
        })

    def update_job_metadata(self, attachment_id: str, *, scope: str,
                            original_name: str, mime_type: Optional[str],
                            size_bytes: Optional[int]):
        self.jobs().document(attachment_id).update({
            "scope": scope,
            "originalName": original_name,
            "mimeType": mime_type,
            "sizeBytes": size_bytes,
            "updatedAt": _now(),
        })


@firestore.transactional
def _try_claim(transaction, ref, claimer_id, lease_ttl_s) -> Optional[dict]:
    snap = ref.get(transaction=transaction)
    if not snap.exists:
        return None
    data = snap.to_dict()
    now = _now()
    if data["status"] != "pending":
        return None
    if data.get("nextAttemptAt") and data["nextAttemptAt"] > now:
        return None
    if data.get("leasedUntil") and data["leasedUntil"] > now:
        return None

    new_attempts = (data.get("attempts") or 0) + 1
    new_lease_until = now + timedelta(seconds=lease_ttl_s)
    transaction.update(ref, {
        "leasedBy": claimer_id,
        "leasedUntil": new_lease_until,
        "attempts": new_attempts,
        "updatedAt": now,
    })
    data["leasedBy"] = claimer_id
    data["leasedUntil"] = new_lease_until
    data["attempts"] = new_attempts
    return data
```

- [ ] **Step 3: Run tests**

```bash
FIRESTORE_EMULATOR_HOST=localhost:8080 GOOGLE_CLOUD_PROJECT=local-dev \
  cd preview && python -m pytest test_store.py -v
```

Expected: 8 passed.

If the test for "claim_picks_up_expired_lease" fails because the emulator can't filter on `leasedUntil < now` when older docs have `leasedUntil` in the future, that's the documented Firestore composite-index requirement. The emulator usually handles single-field <, but composite (`status==pending AND leasedUntil<now`) may need:

```bash
# Set in env once for the session:
export FIRESTORE_EMULATOR_HOST_PATH=localhost:8080
```

If the emulator surfaces an "index needed" error, the message includes a JSON snippet — capture it; we'll fold it into `firestore.indexes.json` in Task 18.

- [ ] **Step 4: Commit**

```bash
git add preview/store.py preview/test_store.py
git commit -m "Add transactional lease claim with expiry-aware retry"
```

### Task 13: Add backoff schedule helper

**Files:**
- Create: `preview/backoff.py`
- Create: `preview/test_backoff.py`

- [ ] **Step 1: Write failing tests**

```python
from backoff import backoff_seconds, MAX_ATTEMPTS


def test_backoff_first_failure():
    assert backoff_seconds(attempts=1) == 5 * 60


def test_backoff_second_failure():
    assert backoff_seconds(attempts=2) == 30 * 60


def test_backoff_third_failure():
    assert backoff_seconds(attempts=3) == 2 * 60 * 60


def test_backoff_fourth_failure():
    assert backoff_seconds(attempts=4) == 12 * 60 * 60


def test_max_attempts_is_5():
    assert MAX_ATTEMPTS == 5
```

- [ ] **Step 2: Confirm fails, then implement**

```python
"""Retry backoff schedule for /tick failures."""

MAX_ATTEMPTS = 5

_SCHEDULE = {
    1: 5 * 60,
    2: 30 * 60,
    3: 2 * 60 * 60,
    4: 12 * 60 * 60,
}


def backoff_seconds(attempts: int) -> int:
    """Return seconds to wait before the next attempt. Caller checks
    attempts >= MAX_ATTEMPTS for the give-up case."""
    return _SCHEDULE.get(attempts, _SCHEDULE[4])
```

- [ ] **Step 3: Run tests**

Run: `cd preview && python -m pytest test_backoff.py -v`
Expected: 5 passed.

- [ ] **Step 4: Commit**

```bash
git add preview/backoff.py preview/test_backoff.py
git commit -m "Add retry backoff schedule"
```

### Task 14: Implement webhook HMAC verification (TDD)

**Files:**
- Create: `preview/webhook_auth.py`
- Create: `preview/test_webhook_auth.py`

- [ ] **Step 1: Write tests**

```python
import hmac, hashlib

from webhook_auth import verify_event_signature, compute_handshake_response


SECRET = "devsecret"


def test_verify_event_signature_matches():
    body = b'{"eventType":"AttachmentAdded","attachmentId":"A","taskId":"T"}'
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_event_signature(SECRET, body, sig) is True


def test_verify_event_signature_rejects_wrong():
    body = b'{"eventType":"AttachmentAdded"}'
    assert verify_event_signature(SECRET, body, "deadbeef") is False


def test_handshake_response_is_hmac_of_secret_value():
    secret_value = "wrike-supplied-handshake-value"
    expected = hmac.new(SECRET.encode(), secret_value.encode(), hashlib.sha256).hexdigest()
    assert compute_handshake_response(SECRET, secret_value) == expected
```

- [ ] **Step 2: Implement**

```python
import hashlib
import hmac


def verify_event_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def compute_handshake_response(secret: str, secret_value: str) -> str:
    return hmac.new(secret.encode(), secret_value.encode(), hashlib.sha256).hexdigest()
```

- [ ] **Step 3: Run tests, commit**

Run: `cd preview && python -m pytest test_webhook_auth.py -v` → 3 passed.

```bash
git add preview/webhook_auth.py preview/test_webhook_auth.py
git commit -m "Add webhook HMAC verification and handshake helpers"
```

### Task 15: Implement internal auth + dev-mode bypass

**Files:**
- Create: `preview/auth.py`

- [ ] **Step 1: Write the simple auth helper**

```python
"""Authentication for /tick and /reconcile.

Two checks (defense in depth):
1. Internal shared secret (X-Internal-Secret header).
2. OIDC token from Cloud Scheduler (Phase 6 — gated on DEV_MODE).

In DEV_MODE=1 (local docker compose) only check #1 is enforced.
"""
import os


def is_dev_mode() -> bool:
    return os.environ.get("DEV_MODE") == "1"


def check_internal_secret(request_headers) -> bool:
    expected = os.environ.get("INTERNAL_SECRET", "")
    if not expected:
        return False
    return request_headers.get("X-Internal-Secret", "") == expected


def check_oidc_token(request_headers) -> bool:
    """Placeholder. Phase 6 replaces this with real google.oauth2.id_token verification."""
    if is_dev_mode():
        return True
    auth = request_headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    # TODO Phase 6: validate the JWT against Google's JWKS.
    # For now, in non-dev mode, presence of a bearer token is the minimum bar.
    # This MUST be replaced before deploying to Cloud Run.
    return False


def authorize_scheduler_request(request_headers) -> bool:
    if not check_internal_secret(request_headers):
        return False
    if not is_dev_mode() and not check_oidc_token(request_headers):
        return False
    return True
```

- [ ] **Step 2: Commit**

```bash
git add preview/auth.py
git commit -m "Add internal-secret auth + OIDC stub (Phase 6 completes it)"
```

### Task 16: Implement the `/tick` worker logic in a dedicated module

**Files:**
- Create: `preview/worker.py`

- [ ] **Step 1: Write the worker**

This module is the most complex part of the service. It threads through download → classify → version-cleanup → convert → upload → mark-done, with the retry/backoff branches.

```python
"""The /tick worker: claim one pending job and run the full conversion pipeline."""
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from classify import classify_attachment, ClassifyResult
from convert import convert_to_pdf, ConvertError
from backoff import backoff_seconds, MAX_ATTEMPTS


LEASE_TTL_S = 6 * 60


def process_one_job(store, wrike) -> Optional[str]:
    """Claim and process exactly one pending job. Returns the attachmentId
    processed (any terminal status), or None if no eligible jobs."""
    claimer = f"tick-{uuid.uuid4()}"
    job = store.claim_next_pending(claimer_id=claimer, lease_ttl_s=LEASE_TTL_S)
    if job is None:
        return None

    attachment_id = job["attachmentId"]
    attempts = job["attempts"]

    try:
        _run_pipeline(store, wrike, attachment_id, attempts)
    except _PermanentFailure as e:
        store.mark_failed(attachment_id, error=str(e), error_code=e.error_code)
    except Exception as e:
        _record_transient(store, attachment_id, attempts, error=str(e), error_code="unexpected")

    return attachment_id


class _PermanentFailure(Exception):
    def __init__(self, error_code: str, msg: str):
        self.error_code = error_code
        super().__init__(msg)


def _record_transient(store, attachment_id, attempts, *, error, error_code):
    if attempts >= MAX_ATTEMPTS:
        store.mark_failed(attachment_id, error=error, error_code="exhausted")
    else:
        store.release_for_retry(
            attachment_id,
            next_attempt_in_s=backoff_seconds(attempts),
            error=error,
            error_code=error_code,
        )


def _run_pipeline(store, wrike, attachment_id, attempts):
    # 1. Fetch metadata
    meta = wrike.get_attachment(attachment_id)
    if meta is None:
        raise _PermanentFailure("deleted", f"attachment {attachment_id} returned 404")

    # 2. Persist metadata
    scope = _infer_scope(meta)
    store.update_job_metadata(
        attachment_id,
        scope=scope,
        original_name=meta["name"],
        mime_type=meta.get("mimeType"),
        size_bytes=meta.get("size"),
    )

    # 3. Classify (size, scope, filename, extension)
    attachment_for_classify = {
        "name": meta["name"],
        "size": meta.get("size") or 0,
        "scope": scope,
        "taskId": meta.get("taskId"),
    }
    result = classify_attachment(attachment_for_classify)
    if result.action == "skip":
        store.mark_skipped(attachment_id, reason=result.reason)
        return

    task_id = meta["taskId"]

    # 4. Version cleanup: any prior done job with same task + same original name
    # is a previous version. Delete its preview and mark it superseded.
    _supersede_old_versions(store, wrike, attachment_id, task_id, meta["name"])

    # 5. Download → convert → upload
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / meta["name"]

        response = wrike.download_attachment_by_id(attachment_id)
        if response.status_code == 404:
            raise _PermanentFailure("deleted", "download returned 404")
        response.raise_for_status()
        with open(input_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=65536):
                fh.write(chunk)

        try:
            pdf_path = convert_to_pdf(input_path, tmp_path)
        except ConvertError as e:
            if e.error_code == "password_protected":
                raise _PermanentFailure("password_protected", e.stderr[:200])
            # Other ConvertError codes are transient (retry).
            raise

        upload_name = f"preview_{attachment_id}.pdf"
        new_id = wrike.add_file_to_task(task_id, pdf_path, upload_name=upload_name)

    store.mark_done(attachment_id, preview_id=new_id, preview_name=upload_name)


def _infer_scope(meta: dict) -> str:
    """Derive scope from Wrike attachment metadata. Adjust if Wrike's
    schema differs from what we expect."""
    if meta.get("taskId"):
        return "task"
    if meta.get("commentId"):
        return "comment"
    if meta.get("folderId"):
        return "folder"
    return "unknown"


def _supersede_old_versions(store, wrike, current_attachment_id, task_id, original_name):
    """Find prior done jobs with the same task+name; delete their previews and mark superseded."""
    prior = (
        store.jobs()
        .where("taskId", "==", task_id)
        .where("originalName", "==", original_name)
        .where("status", "==", "done")
        .stream()
    )
    for snap in prior:
        if snap.id == current_attachment_id:
            continue
        data = snap.to_dict()
        preview_id = data.get("previewId")
        if preview_id:
            try:
                wrike.delete_attachment_by_id(preview_id)
            except Exception:
                # Best-effort. If the preview is already gone, we still mark superseded.
                pass
        snap.reference.update({
            "status": "superseded",
            "previewId": None,
        })
```

- [ ] **Step 2: Commit**

```bash
git add preview/worker.py
git commit -m "Add /tick worker pipeline with retry, version cleanup, and supersede logic"
```

### Task 17: Implement `server.py` with all four routes

**Files:**
- Create: `preview/server.py`

- [ ] **Step 1: Write the Flask app**

```python
"""Flask app exposing /webhook, /tick, /reconcile, /healthz."""
import logging
import os
from pathlib import Path

from flask import Flask, request, abort
from google.cloud import firestore

# Local imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))   # to find wrike.py
from wrike import WrikeApi

from auth import authorize_scheduler_request, is_dev_mode
from store import Store
from webhook_auth import verify_event_signature, compute_handshake_response
from worker import process_one_job


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("preview")


def _make_app():
    app = Flask(__name__)

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "local-dev")
    db = firestore.Client(project=project)
    store = Store(db)

    config_path = os.environ.get("WRIKE_CONFIG_PATH", "/app/wrike-config.json")
    if not os.path.exists(config_path):
        # In dev mode we fall back to building a config from WRIKE_API_TOKEN env.
        token = os.environ.get("WRIKE_API_TOKEN", "")
        if not token:
            raise RuntimeError("Neither WRIKE_CONFIG_PATH nor WRIKE_API_TOKEN is set.")
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as fh:
            fh.write(f'{{"WRIKE_API_TOKEN":"{token}"}}\n')
    wrike = WrikeApi(config_path)

    signing_secret = os.environ.get("WEBHOOK_SIGNING_SECRET", "")

    @app.get("/healthz")
    def healthz():
        return ("ok", 200)

    @app.post("/webhook")
    def webhook():
        body = request.get_data()
        x_hook_secret = request.headers.get("X-Hook-Secret")
        x_hook_signature = request.headers.get("X-Hook-Signature")

        # Handshake: Wrike sends a verification body.
        try:
            json_body = request.get_json(silent=True) or {}
        except Exception:
            json_body = {}

        if json_body.get("requestType") == "WebHook secret verification" and x_hook_secret:
            response_value = compute_handshake_response(signing_secret, x_hook_secret)
            resp = ("", 200)
            from flask import make_response
            r = make_response(resp)
            r.headers["X-Hook-Secret"] = response_value
            return r

        # Event delivery
        if not verify_event_signature(signing_secret, body, x_hook_signature):
            log.warning("webhook signature mismatch")
            return ("", 200)   # do NOT 4xx — Wrike suspends on 4xx

        event_type = json_body.get("eventType")
        if event_type != "AttachmentAdded":
            return ("", 200)

        attachment_id = json_body.get("attachmentId")
        task_id = json_body.get("taskId")
        if not attachment_id:
            log.warning("webhook event missing attachmentId: %s", json_body)
            return ("", 200)

        created = store.create_job_if_absent(attachment_id, task_id=task_id)
        log.info("webhook attachment=%s task=%s created=%s", attachment_id, task_id, created)
        return ("", 200)

    @app.post("/tick")
    def tick():
        if not authorize_scheduler_request(request.headers):
            log.warning("tick: unauthorized")
            return ("", 200)

        processed = []
        for _ in range(3):   # BATCH_SIZE = 3
            attachment_id = process_one_job(store, wrike)
            if attachment_id is None:
                break
            processed.append(attachment_id)
        return ({"processed": processed}, 200)

    @app.post("/reconcile")
    def reconcile_route():
        if not authorize_scheduler_request(request.headers):
            log.warning("reconcile: unauthorized")
            return ("", 200)
        # Implementation comes in Phase 5 (Task 19).
        return ({"status": "not_yet_implemented"}, 200)

    return app


app = _make_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
```

- [ ] **Step 2: Bring up docker compose**

```bash
cd preview
WRIKE_API_TOKEN=$(jq -r .WRIKE_API_TOKEN ../config.json) docker compose up --build
```

Expected: both services start. App logs `Running on http://0.0.0.0:5000`.

- [ ] **Step 3: Hit healthz**

In another shell: `curl -s http://localhost:5000/healthz`
Expected: `ok`.

- [ ] **Step 4: Test the webhook handshake**

```bash
curl -i -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Hook-Secret: testhandshakevalue" \
  -d '{"requestType":"WebHook secret verification"}'
```

Expected: `200 OK` with header `X-Hook-Secret: <hex-string>`. The string must equal:

```bash
python -c "import hmac, hashlib; print(hmac.new(b'devsecret', b'testhandshakevalue', hashlib.sha256).hexdigest())"
```

- [ ] **Step 5: Test a forged-signature webhook**

```bash
curl -i -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Hook-Signature: deadbeef" \
  -d '{"eventType":"AttachmentAdded","attachmentId":"FAKE","taskId":"T1"}'
```

Expected: `200 OK` (Wrike doesn't suspend), and the app log shows `webhook signature mismatch`.

- [ ] **Step 6: Test a valid-signature webhook**

```bash
BODY='{"eventType":"AttachmentAdded","attachmentId":"FAKE1","taskId":"T1"}'
SIG=$(python -c "import hmac,hashlib,sys; print(hmac.new(b'devsecret', sys.argv[1].encode(), hashlib.sha256).hexdigest())" "$BODY")
curl -i -X POST http://localhost:5000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Hook-Signature: $SIG" \
  -d "$BODY"
```

Expected: `200 OK`, app log shows `webhook attachment=FAKE1 task=T1 created=True`.

- [ ] **Step 7: Re-send identical webhook (idempotency)**

Run the same curl as Step 6 again. Expected: `200 OK`, app log shows `created=False`.

- [ ] **Step 8: Trigger /tick (will fail on FAKE1 because Wrike returns 404)**

```bash
curl -s -X POST http://localhost:5000/tick -H "X-Internal-Secret: devinternal"
```

Expected: `{"processed":["FAKE1"]}`. The job moves to `status='failed'`, `errorCode='deleted'`. Verify in the Firestore emulator UI at http://localhost:8080/ or via a quick read:

```bash
docker compose exec firestore curl -s 'http://localhost:8080/v1/projects/local-dev/databases/(default)/documents/jobs/FAKE1'
```

- [ ] **Step 9: Commit**

```bash
git add preview/server.py
git commit -m "Add Flask app with webhook handshake, signature verification, and tick worker"
```

### Task 18: Add `firestore.indexes.json` for the composite index

**Files:**
- Create: `preview/firestore.indexes.json`

- [ ] **Step 1: Declare the index for `claim_next_pending`**

```json
{
  "indexes": [
    {
      "collectionGroup": "jobs",
      "queryScope": "COLLECTION",
      "fields": [
        { "fieldPath": "status", "order": "ASCENDING" },
        { "fieldPath": "nextAttemptAt", "order": "ASCENDING" },
        { "fieldPath": "leasedUntil", "order": "ASCENDING" }
      ]
    },
    {
      "collectionGroup": "jobs",
      "queryScope": "COLLECTION",
      "fields": [
        { "fieldPath": "taskId", "order": "ASCENDING" },
        { "fieldPath": "originalName", "order": "ASCENDING" },
        { "fieldPath": "status", "order": "ASCENDING" }
      ]
    }
  ],
  "fieldOverrides": []
}
```

The second index covers `_supersede_old_versions`'s query.

- [ ] **Step 2: Commit**

```bash
git add preview/firestore.indexes.json
git commit -m "Declare Firestore composite indexes"
```

---

## Phase 4 — Real Wrike via tunnel

**Goal:** Hit the local service from real Wrike via a tunnel; register and verify the webhook handshake against Wrike for real.

### Task 19: Build `register_webhook.py`

**Files:**
- Create: `preview/register_webhook.py`

- [ ] **Step 1: Write the registration helper**

```python
"""One-shot registration of the Wrike webhook.

Usage:
    python preview/register_webhook.py \
        --hook-url https://example.trycloudflare.com/webhook \
        --secret devsecret

Prints the webhook id on success. Save it; you'll need it to delete the webhook later.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from wrike import WrikeApi

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hook-url", required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    wrike = WrikeApi(args.config)
    url = f"{wrike.WRIKE_BASE_URL}/webhooks"
    headers = wrike.WRIKE_DEFAULT_HEADERS
    payload = {
        "hookUrl": args.hook_url,
        "secret": args.secret,
        "events": ["AttachmentAdded"],
    }
    response = requests.post(url, headers=headers, data=payload)
    response.raise_for_status()
    data = response.json()["data"]
    print(json.dumps(data, indent=2))


def list_webhooks(args):
    wrike = WrikeApi(args.config)
    response = requests.get(f"{wrike.WRIKE_BASE_URL}/webhooks", headers=wrike.WRIKE_DEFAULT_HEADERS)
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))


def delete_webhook(webhook_id, args):
    wrike = WrikeApi(args.config)
    response = requests.delete(
        f"{wrike.WRIKE_BASE_URL}/webhooks/{webhook_id}",
        headers=wrike.WRIKE_DEFAULT_HEADERS,
    )
    response.raise_for_status()
    print("deleted")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Open a tunnel**

In a terminal: `cloudflared tunnel --url http://localhost:5000`
(Install: `brew install cloudflared` or download the binary. Alternative: `ngrok http 5000`.)

Note the public URL it prints, e.g. `https://random-words.trycloudflare.com`.

- [ ] **Step 3: Make sure docker compose is up**

`cd preview && WRIKE_API_TOKEN=... docker compose up`

- [ ] **Step 4: Register the webhook against your TEST Wrike workspace**

**Caution:** this writes to live Wrike. Use a test workspace if possible.

```bash
python preview/register_webhook.py \
    --hook-url https://random-words.trycloudflare.com/webhook \
    --secret devsecret
```

Expected: prints the new webhook's metadata. Wrike will POST to your tunnel for the handshake — watch the docker compose logs to see the verification request and confirm the handshake succeeded.

- [ ] **Step 5: Test with a real upload**

Upload a DOCX to a task in your test Wrike workspace via the Wrike UI. Watch the logs:

1. `webhook attachment=<id> task=<taskid> created=True` should appear within a few seconds.
2. Trigger `/tick` manually (Scheduler isn't running locally):
   ```bash
   curl -s -X POST http://localhost:5000/tick -H "X-Internal-Secret: devinternal"
   ```
3. App logs should show the download, conversion, upload sequence.
4. Refresh the task in Wrike — `preview_<id>.pdf` should appear.

- [ ] **Step 6: Delete the test webhook when done**

```bash
python preview/register_webhook.py --config ../config.json   # list
# pick the id from output
python -c "from preview.register_webhook import delete_webhook; import argparse; \
  delete_webhook('WEBHOOK_ID', argparse.Namespace(config='config.json'))"
```

- [ ] **Step 7: Commit**

```bash
git add preview/register_webhook.py
git commit -m "Add Wrike webhook registration helper"
```

---

## Phase 5 — Reconcile (bootstrap + steady-state)

**Goal:** Implement and locally test the reconcile loop. Both modes (bootstrap walking back, steady-state walking forward with durable cursor) run against the Firestore emulator.

### Task 20: Implement `reconcile.py`

**Files:**
- Create: `preview/reconcile.py`

- [ ] **Step 1: Write the reconcile module**

```python
"""Reconcile: bootstrap walk + steady-state catch-up."""
from datetime import datetime, timedelta, timezone
from typing import Tuple


BOOTSTRAP_WINDOW_DAYS = 28
BOOTSTRAP_FLOOR_YEARS = 10
STEADY_OVERLAP_HOURS = 1


def reconcile_one_chunk(store, wrike) -> dict:
    """Run one reconcile chunk. Returns a status dict for logging."""
    state = _get_or_init_state(store)

    if state["isBootstrap"]:
        return _bootstrap_chunk(store, wrike, state)
    return _steady_state_chunk(store, wrike, state)


def _get_or_init_state(store):
    ref = store.db.collection("state").document("reconcile")
    snap = ref.get()
    if snap.exists:
        return snap.to_dict()
    now = datetime.now(tz=timezone.utc)
    floor = now - timedelta(days=BOOTSTRAP_FLOOR_YEARS * 365)
    state = {
        "isBootstrap": True,
        "oldestCovered": now,
        "lastReconciledThrough": now,
        "bootstrapFloor": floor,
        "updatedAt": now,
    }
    ref.set(state)
    return state


def _bootstrap_chunk(store, wrike, state):
    window_end = state["oldestCovered"]
    window_start = max(
        window_end - timedelta(days=BOOTSTRAP_WINDOW_DAYS),
        state["bootstrapFloor"],
    )

    enqueued = _walk_and_enqueue(store, wrike, window_start, window_end)

    new_oldest = window_start
    is_bootstrap = new_oldest > state["bootstrapFloor"]

    store.db.collection("state").document("reconcile").update({
        "oldestCovered": new_oldest,
        "isBootstrap": is_bootstrap,
        "updatedAt": datetime.now(tz=timezone.utc),
    })
    return {
        "mode": "bootstrap",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "enqueued": enqueued,
        "is_bootstrap_after": is_bootstrap,
    }


def _steady_state_chunk(store, wrike, state):
    now = datetime.now(tz=timezone.utc)
    window_start = state["lastReconciledThrough"] - timedelta(hours=STEADY_OVERLAP_HOURS)
    window_end = now

    # If the gap exceeds the Wrike API window cap, chunk it.
    total_enqueued = 0
    cursor_start = window_start
    while cursor_start < window_end:
        cursor_end = min(cursor_start + timedelta(days=BOOTSTRAP_WINDOW_DAYS), window_end)
        total_enqueued += _walk_and_enqueue(store, wrike, cursor_start, cursor_end)
        cursor_start = cursor_end

    store.db.collection("state").document("reconcile").update({
        "lastReconciledThrough": now,
        "updatedAt": now,
    })
    return {
        "mode": "steady",
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "enqueued": total_enqueued,
    }


def _walk_and_enqueue(store, wrike, window_start, window_end) -> int:
    enqueued = 0
    page_token = None
    while True:
        items, page_token = wrike.list_account_attachments(
            created_from=window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            created_to=window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            next_page_token=page_token,
        )
        for item in items:
            task_id = item.get("taskId")
            if store.create_job_if_absent(item["id"], task_id=task_id):
                enqueued += 1
        if not page_token:
            break
    return enqueued
```

- [ ] **Step 2: Wire `/reconcile` to this module**

In `preview/server.py`, replace the `reconcile_route` body:

```python
    @app.post("/reconcile")
    def reconcile_route():
        if not authorize_scheduler_request(request.headers):
            log.warning("reconcile: unauthorized")
            return ("", 200)
        from reconcile import reconcile_one_chunk
        result = reconcile_one_chunk(store, wrike)
        log.info("reconcile %s", result)
        return (result, 200)
```

- [ ] **Step 3: Manually test the bootstrap path**

With docker compose up and your tunnel pointed at a TEST workspace with some history:

```bash
curl -s -X POST http://localhost:5000/reconcile -H "X-Internal-Secret: devinternal"
```

Expected: returns `{"mode":"bootstrap", "window_start":"...", "window_end":"...", "enqueued": N, "is_bootstrap_after": true}` for the first call. Subsequent calls walk further back; `enqueued` may be 0 for old windows.

- [ ] **Step 4: Test bootstrap termination**

Set the floor close to `now` to force termination on one call. Use a small Python script:

```bash
docker compose exec app python -c "
from google.cloud import firestore
from datetime import datetime, timedelta, timezone
db = firestore.Client(project='local-dev')
now = datetime.now(tz=timezone.utc)
db.collection('state').document('reconcile').set({
    'isBootstrap': True,
    'oldestCovered': now,
    'lastReconciledThrough': now,
    'bootstrapFloor': now - timedelta(days=30),
    'updatedAt': now,
})
print('reset')
"
```

Then `curl -X POST .../reconcile` once. After the call, fetch the state:

```bash
docker compose exec app python -c "
from google.cloud import firestore
db = firestore.Client(project='local-dev')
print(db.collection('state').document('reconcile').get().to_dict())
"
```

Expected: `isBootstrap: False`.

- [ ] **Step 5: Test steady-state mode**

Trigger `/reconcile` again. Expected: response `mode='steady'`.

- [ ] **Step 6: Commit**

```bash
git add preview/reconcile.py preview/server.py
git commit -m "Add reconcile with bootstrap walk-back and steady-state cursor"
```

### Task 21: Add admin CLI

**Files:**
- Create: `preview/admin.py`

- [ ] **Step 1: Write the admin script**

```python
"""Admin operations for the preview service queue.

Usage examples:
    python preview/admin.py list-failed
    python preview/admin.py requeue <attachmentId>
    python preview/admin.py reset-reconcile-state
    python preview/admin.py stats

Requires FIRESTORE_EMULATOR_HOST + GOOGLE_CLOUD_PROJECT in dev, or ADC in prod.
"""
import argparse
import json
import os
from datetime import datetime, timezone

from google.cloud import firestore


def list_failed(args, db):
    failed = db.collection("jobs").where("status", "==", "failed").stream()
    for snap in failed:
        d = snap.to_dict()
        print(snap.id, d.get("errorCode"), d.get("originalName"), d.get("error"))


def requeue(args, db):
    ref = db.collection("jobs").document(args.attachment_id)
    snap = ref.get()
    if not snap.exists:
        print("not found")
        return
    now = datetime.now(tz=timezone.utc)
    ref.update({
        "status": "pending",
        "attempts": 0,
        "nextAttemptAt": now,
        "leasedBy": None,
        "leasedUntil": datetime(1970, 1, 1, tzinfo=timezone.utc),
        "error": None,
        "errorCode": None,
        "updatedAt": now,
    })
    print(f"requeued {args.attachment_id}")


def stats(args, db):
    counts = {}
    for snap in db.collection("jobs").stream():
        s = snap.to_dict().get("status", "?")
        counts[s] = counts.get(s, 0) + 1
    print(json.dumps(counts, indent=2))


def reset_reconcile_state(args, db):
    now = datetime.now(tz=timezone.utc)
    db.collection("state").document("reconcile").delete()
    print("deleted state/reconcile (will be re-initialized on next /reconcile call)")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-failed")
    sub.add_parser("stats")
    sub.add_parser("reset-reconcile-state")

    p_req = sub.add_parser("requeue")
    p_req.add_argument("attachment_id")

    args = parser.parse_args()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "local-dev")
    db = firestore.Client(project=project)

    {"list-failed": list_failed, "requeue": requeue, "stats": stats,
     "reset-reconcile-state": reset_reconcile_state}[args.cmd](args, db)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test against the emulator**

```bash
docker compose exec app python admin.py stats
docker compose exec app python admin.py list-failed
```

Expected: prints job counts by status; prints failed job ids if any.

- [ ] **Step 3: Commit**

```bash
git add preview/admin.py
git commit -m "Add admin CLI: list-failed, requeue, stats, reset-reconcile-state"
```

---

## Phase 6 — Deploy to Cloud Run

**Goal:** Ship the working local service to Cloud Run with real Firestore, Secret Manager, and Cloud Scheduler. Replace the OIDC stub with a real implementation.

### Task 22: Complete OIDC verification in `auth.py`

**Files:**
- Modify: `preview/auth.py`

- [ ] **Step 1: Replace the OIDC stub with a real verifier**

In `preview/auth.py`, replace the `check_oidc_token` function:

```python
def check_oidc_token(request_headers) -> bool:
    """Verify a Google-issued OIDC token (from Cloud Scheduler).

    Caches Google's public keys via the underlying transport; lookups are fast
    after the first verification."""
    if is_dev_mode():
        return True
    auth = request_headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[len("Bearer "):]
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as ga_requests

        request_adapter = ga_requests.Request()
        claims = id_token.verify_oauth2_token(token, request_adapter)

        # Optional further check: claims["email"] should match the Scheduler service account.
        expected_sa = os.environ.get("SCHEDULER_SA_EMAIL", "")
        if expected_sa and claims.get("email") != expected_sa:
            return False
        return True
    except Exception:
        return False
```

Add `google-auth>=2.0` to `preview/requirements.txt` if not already present (it's a dep of `google-cloud-firestore`, so usually already in).

- [ ] **Step 2: Commit**

```bash
git add preview/auth.py preview/requirements.txt
git commit -m "Implement real OIDC verification for /tick and /reconcile"
```

### Task 23: Document setup steps

**Files:**
- Create: `preview/SETUP.md`

- [ ] **Step 1: Write SETUP.md**

```markdown
# Setup

One-time GCP wiring. Assumes `gcloud` is installed and authenticated.

## 1. Project + APIs

```bash
PROJECT=wrike-preview-<your-suffix>
REGION=us-central1
gcloud projects create $PROJECT
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com firestore.googleapis.com \
    cloudscheduler.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com cloudbuild.googleapis.com
```

## 2. Firestore (Native mode)

```bash
gcloud firestore databases create --location=$REGION
gcloud firestore indexes create --database='(default)' \
    --file=preview/firestore.indexes.json
```

## 3. Secrets

```bash
echo -n "$WRIKE_TOKEN" | gcloud secrets create wrike-api-token --data-file=-
openssl rand -hex 32 | gcloud secrets create webhook-signing-secret --data-file=-
openssl rand -hex 32 | gcloud secrets create internal-secret --data-file=-
```

## 4. Service accounts

```bash
gcloud iam service-accounts create preview-sa \
    --display-name="Cloud Run runtime for preview service"
gcloud iam service-accounts create preview-scheduler \
    --display-name="Cloud Scheduler invoker for preview service"

# Runtime SA needs Firestore + Secret access
gcloud projects add-iam-policy-binding $PROJECT \
    --member=serviceAccount:preview-sa@$PROJECT.iam.gserviceaccount.com \
    --role=roles/datastore.user
for s in wrike-api-token webhook-signing-secret internal-secret; do
  gcloud secrets add-iam-policy-binding $s \
      --member=serviceAccount:preview-sa@$PROJECT.iam.gserviceaccount.com \
      --role=roles/secretmanager.secretAccessor
done
```

## 5. Artifact Registry + container image

```bash
gcloud artifacts repositories create preview --location=$REGION --repository-format=docker
gcloud builds submit preview/ \
    --tag $REGION-docker.pkg.dev/$PROJECT/preview/server:latest
```

## 6. Deploy Cloud Run

```bash
gcloud run deploy preview \
    --image $REGION-docker.pkg.dev/$PROJECT/preview/server:latest \
    --region $REGION \
    --service-account preview-sa@$PROJECT.iam.gserviceaccount.com \
    --allow-unauthenticated \
    --concurrency 2 \
    --memory 1Gi \
    --timeout 300 \
    --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT,SCHEDULER_SA_EMAIL=preview-scheduler@$PROJECT.iam.gserviceaccount.com \
    --set-secrets WRIKE_API_TOKEN=wrike-api-token:latest,WEBHOOK_SIGNING_SECRET=webhook-signing-secret:latest,INTERNAL_SECRET=internal-secret:latest
```

Capture the deployed URL (e.g., `https://preview-xxxxxx.run.app`).

## 7. Cloud Run invoker IAM for Scheduler

```bash
gcloud run services add-iam-policy-binding preview \
    --region $REGION \
    --member=serviceAccount:preview-scheduler@$PROJECT.iam.gserviceaccount.com \
    --role=roles/run.invoker
```

## 8. Cloud Scheduler jobs

```bash
INTERNAL_SECRET=$(gcloud secrets versions access latest --secret=internal-secret)
URL=https://preview-xxxxxx.run.app
gcloud scheduler jobs create http wrike-tick \
    --location=$REGION \
    --schedule='* * * * *' \
    --uri=$URL/tick \
    --http-method=POST \
    --oidc-service-account-email=preview-scheduler@$PROJECT.iam.gserviceaccount.com \
    --oidc-token-audience=$URL \
    --headers="X-Internal-Secret=$INTERNAL_SECRET"

gcloud scheduler jobs create http wrike-reconcile \
    --location=$REGION \
    --schedule='*/30 * * * *' \
    --uri=$URL/reconcile \
    --http-method=POST \
    --oidc-service-account-email=preview-scheduler@$PROJECT.iam.gserviceaccount.com \
    --oidc-token-audience=$URL \
    --headers="X-Internal-Secret=$INTERNAL_SECRET"
```

## 9. Register the Wrike webhook

```bash
SIGNING=$(gcloud secrets versions access latest --secret=webhook-signing-secret)
python preview/register_webhook.py \
    --hook-url $URL/webhook \
    --secret $SIGNING
```

Wrike will hit `$URL/webhook` with the verification body; the deployed service responds with the HMAC; Wrike marks the webhook active.

## 10. Verify

Upload a DOCX to a real Wrike task and watch Cloud Logging for the preview service.
```

- [ ] **Step 2: Commit**

```bash
git add preview/SETUP.md
git commit -m "Document Cloud Run setup steps"
```

### Task 24: Deploy and smoke-test in production

- [ ] **Step 1: Run through SETUP.md steps 1-7**

Execute each block. Capture the deployed URL.

- [ ] **Step 2: Smoke-test healthz**

`curl https://preview-xxxxxx.run.app/healthz` → `ok`.

- [ ] **Step 3: Smoke-test tick (should be 200 with empty processed list)**

```bash
INTERNAL_SECRET=$(gcloud secrets versions access latest --secret=internal-secret)
URL=https://preview-xxxxxx.run.app
# Without OIDC + secret: should silently 200 with no work (logged WARN)
curl -X POST $URL/tick
# With internal secret only but no OIDC: should also be ignored in prod
curl -X POST $URL/tick -H "X-Internal-Secret: $INTERNAL_SECRET"
```

Both expected: `200`, no work done. Cloud Logging should show "tick: unauthorized" lines.

- [ ] **Step 4: Wait for the first Scheduler tick**

Within 60s of finishing step 8 in SETUP.md, the scheduler should fire. Check Cloud Logging:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="preview"' \
    --limit 20 --format="value(timestamp,jsonPayload.message,textPayload)"
```

Expected: `tick` invocations with `{"processed": []}` (no jobs yet).

- [ ] **Step 5: Register webhook + upload one test file**

Follow steps 9 + 10 in SETUP.md against your test workspace.

- [ ] **Step 6: Verify the production end-to-end**

Within ~1 minute of the upload: refresh the Wrike task. `preview_<id>.pdf` should appear.

---

## Phase 7 — Backfill + monitoring

**Goal:** Let the bootstrap walk run and monitor for problems.

### Task 25: Monitor bootstrap progress

- [ ] **Step 1: After step 6 of Task 24, bootstrap is already running**

The first `/reconcile` call initialized state with `isBootstrap=true`. Every 30 minutes, another chunk walks 28 days further back.

- [ ] **Step 2: Watch progress**

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="preview" AND jsonPayload.message=~"reconcile"' \
    --limit 50 --format="value(timestamp,jsonPayload.message)"
```

For a 5-year account, expect ~65 reconcile cycles before `is_bootstrap_after: false`.

- [ ] **Step 3: Spot-check tasks**

Pick a few historical tasks with Office attachments. After ~24h of bootstrap, verify `preview_<id>.pdf` appears on them.

- [ ] **Step 4: Investigate failures**

```bash
# Connect admin.py to production Firestore (no FIRESTORE_EMULATOR_HOST set)
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py list-failed
GOOGLE_CLOUD_PROJECT=$PROJECT python preview/admin.py stats
```

For each failed job, investigate the `errorCode`. Common ones to expect:
- `deleted` — attachment removed before processing; fine to leave failed.
- `password_protected` — by design; cannot be processed.
- `soffice_crash` / `soffice_timeout` — investigate the underlying file. Use `admin.py requeue <id>` to retry after the cause is fixed.
- `exhausted` — investigate the `error` text for the last transient cause.

- [ ] **Step 5: Update README with operating notes**

Add a short section to the root `README.md` linking to:
- `preview/SETUP.md`
- `preview/admin.py` usage notes
- A mention that bootstrap runs once and takes ~30 hours.

```bash
git add README.md
git commit -m "Document preview service operations in README"
```

---

## Coverage Self-Check

The plan covers each spec section as follows:

| Spec section | Plan coverage |
|---|---|
| Goals (real-time, self-bootstrap, resilient, idempotent, lease-safe, cheap) | Tasks 11, 12, 14, 16-17, 20, 22, 24 |
| Stack (Cloud Run, Firestore, Scheduler, LibreOffice, Secret Manager) | Tasks 5, 9, 22-24 |
| Architecture (4 routes, decoupled handler vs worker) | Task 17 |
| Data model (jobs + state collections) | Tasks 11, 18, 20 |
| `/webhook` (handshake, create-if-absent, 200-on-bad-sig) | Tasks 14, 17 |
| `/tick` (lease, fetch, classify, supersede, convert, upload, retry) | Tasks 10, 12, 13, 16 |
| `/reconcile` (bootstrap + steady-state) | Task 20 |
| Setup (Secret Manager, IAM, scheduler, webhook reg) | Tasks 19, 22, 23, 24 |
| Repo structure | Tasks 2-24 incrementally build it |
| Phased rollout | Plan phases mirror spec phases 0-7 |
| Non-goals | Respected — no UI, no metrics dashboard, no non-Office types |

All tests run inside `docker compose` against the local Firestore emulator until Phase 6. Real Wrike is only touched in Phase 2 (one-off CLI to a test workspace) and Phase 4 (tunnel-routed webhook). Production deploy in Phase 6 is the first Cloud Run cost.
