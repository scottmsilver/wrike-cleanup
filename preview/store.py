"""Firestore wrappers for the preview service queue.

Two collections:
  jobs/{attachmentId}   — one document per attachment we've ever seen.
                          See `create_job_if_absent` for the initial shape.
  state/reconcile       — singleton holding bootstrap cursor, written by
                          `reconcile.py` (not this module).

Why `leasedUntil` is initialized to EPOCH (1970-01-01) rather than null:
Firestore queries cannot match nulls with `<`, so a `null` value would
prevent the lease query from finding unleased jobs. EPOCH is always less
than `now`, so unleased jobs are always eligible.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _now() -> datetime:
    """`datetime.now()` is hard to mock and easy to misuse; centralize."""
    return datetime.now(tz=timezone.utc)


class Store:
    """Firestore-backed CRUD for the jobs queue with lease semantics.

    Construct with a `firestore.Client` so tests can pass an emulator-pointing
    client and production gets ADC-authenticated.
    """

    def __init__(self, db: firestore.Client):
        self.db = db

    def jobs(self):
        """Return the `jobs` collection reference (used directly by reconcile
        + worker for ad-hoc queries that don't fit a CRUD method)."""
        return self.db.collection("jobs")

    def get_job(self, attachment_id: str) -> Optional[dict]:
        """Return the job dict for `attachment_id`, or None if missing."""
        snap = self.jobs().document(attachment_id).get()
        return snap.to_dict() if snap.exists else None

    def create_job_if_absent(self, attachment_id: str, *, task_id: Optional[str]) -> bool:
        """Atomically insert a pending job. Returns True if inserted, False if a
        doc already existed (any status — never overwrite)."""
        ref = self.jobs().document(attachment_id)
        try:
            now = _now()
            ref.create(
                {
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
                }
            )
            return True
        except gax_exceptions.AlreadyExists:
            return False

    def mark_done(self, attachment_id: str, *, preview_id: str, preview_name: str):
        """Mark a job complete; record the resulting Wrike attachment id +
        filename for supersede logic later. Clears the lease."""
        now = _now()
        self.jobs().document(attachment_id).update(
            {
                "status": "done",
                "previewId": preview_id,
                "previewName": preview_name,
                "leasedBy": None,
                "leasedUntil": EPOCH,
                "updatedAt": now,
            }
        )

    def mark_failed(self, attachment_id: str, *, error: str, error_code: str):
        """Permanent failure. `error` is truncated to 500 chars to bound
        Firestore doc size; `errorCode` is one of the well-known short codes
        the worker emits (deleted, password_protected, exhausted, ...)."""
        now = _now()
        self.jobs().document(attachment_id).update(
            {
                "status": "failed",
                "error": error[:500],
                "errorCode": error_code,
                "leasedBy": None,
                "leasedUntil": EPOCH,
                "updatedAt": now,
            }
        )

    def mark_skipped(self, attachment_id: str, *, reason: str):
        """Job classified as not-our-job (wrong scope, wrong type, oversize,
        already previewable, our own preview file). `reason` is recorded
        in `skippedReason` for later inspection via admin.py."""
        now = _now()
        self.jobs().document(attachment_id).update(
            {
                "status": "skipped",
                "skippedReason": reason,
                "leasedBy": None,
                "leasedUntil": EPOCH,
                "updatedAt": now,
            }
        )

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
            # limit(5) gives a small buffer for stale-lease races: if the
            # first candidate was just claimed by a concurrent /tick, we
            # still have backup candidates without re-querying. Returns one
            # eventually per call.
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

    def release_for_retry(
        self,
        attachment_id: str,
        *,
        next_attempt_in_s: int,
        error: str,
        error_code: str,
    ):
        """Transient failure: release the lease and reschedule for retry
        after `next_attempt_in_s` seconds (caller computes per backoff
        schedule). `attempts` is NOT incremented here — that happens at
        lease-claim time so OOM-killed containers still count."""
        now = _now()
        self.jobs().document(attachment_id).update(
            {
                "status": "pending",
                "leasedBy": None,
                "leasedUntil": EPOCH,
                "nextAttemptAt": now + timedelta(seconds=next_attempt_in_s),
                "error": error[:500],
                "errorCode": error_code,
                "updatedAt": now,
            }
        )

    def update_job_metadata(
        self,
        attachment_id: str,
        *,
        scope: str,
        original_name: str,
        mime_type: Optional[str],
        size_bytes: Optional[int],
    ):
        """Populate the metadata fields after the first /tick fetches the
        full attachment from Wrike. Webhook event payload only carries
        attachmentId + taskId, so initial create_job_if_absent leaves
        these null until the worker fills them in."""
        now = _now()
        self.jobs().document(attachment_id).update(
            {
                "scope": scope,
                "originalName": original_name,
                "mimeType": mime_type,
                "sizeBytes": size_bytes,
                "updatedAt": now,
            }
        )


@firestore.transactional
def _try_claim(transaction, ref, claimer_id, lease_ttl_s) -> Optional[dict]:
    """Atomic claim helper for `Store.claim_next_pending`.

    Lives at module level (not as a method) because `@firestore.transactional`
    only decorates standalone functions. Re-reads the doc inside the
    transaction and re-checks eligibility — the outer query is a snapshot
    that may be stale.

    Returns the (now-leased) job dict on success, None if the doc became
    ineligible between the outer query and the transactional re-read.
    """
    snap = ref.get(transaction=transaction)
    if not snap.exists:
        return None
    data = snap.to_dict()
    now = _now()
    if data["status"] != "pending":
        return None
    if data["nextAttemptAt"] > now:
        return None
    if data["leasedUntil"] > now:
        return None

    new_attempts = (data.get("attempts") or 0) + 1
    new_lease_until = now + timedelta(seconds=lease_ttl_s)
    transaction.update(
        ref,
        {
            "leasedBy": claimer_id,
            "leasedUntil": new_lease_until,
            "attempts": new_attempts,
            "updatedAt": now,
        },
    )
    data["leasedBy"] = claimer_id
    data["leasedUntil"] = new_lease_until
    data["attempts"] = new_attempts
    return data
