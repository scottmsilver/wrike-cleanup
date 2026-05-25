from datetime import datetime, timedelta, timezone
from typing import Optional

from google.api_core import exceptions as gax_exceptions
from google.cloud import firestore

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Store:
    def __init__(self, db: firestore.Client):
        self.db = db

    def jobs(self):
        return self.db.collection("jobs")

    def get_job(self, attachment_id: str) -> Optional[dict]:
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
