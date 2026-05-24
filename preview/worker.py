"""The /tick worker: claim one pending job and run the full conversion pipeline."""

import tempfile
import uuid
from pathlib import Path
from typing import Optional

from backoff import MAX_ATTEMPTS, backoff_seconds
from classify import classify_attachment
from convert import ConvertError, convert_to_pdf

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
    """Derive scope from Wrike attachment metadata."""
    if meta.get("taskId"):
        return "task"
    if meta.get("commentId"):
        return "comment"
    if meta.get("folderId"):
        return "folder"
    return "unknown"


def _supersede_old_versions(store, wrike, current_attachment_id, task_id, original_name):
    """Find prior done jobs with same task+name; delete their previews and mark superseded."""
    prior = (
        store.jobs()
        .where("taskId", "==", task_id)
        .where("originalName", "==", original_name)
        .where("status", "==", "done")
        .order_by("taskId")
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
                # Best-effort. If the preview is already gone, mark superseded anyway.
                pass
        snap.reference.update(
            {
                "status": "superseded",
                "previewId": None,
            }
        )
