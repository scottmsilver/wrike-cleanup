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
    ref.update(
        {
            "status": "pending",
            "attempts": 0,
            "nextAttemptAt": now,
            "leasedBy": None,
            "leasedUntil": datetime(1970, 1, 1, tzinfo=timezone.utc),
            "error": None,
            "errorCode": None,
            "updatedAt": now,
        }
    )
    print(f"requeued {args.attachment_id}")


def stats(args, db):
    counts = {}
    for snap in db.collection("jobs").stream():
        s = snap.to_dict().get("status", "?")
        counts[s] = counts.get(s, 0) + 1
    print(json.dumps(counts, indent=2))


def reset_reconcile_state(args, db):
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

    {
        "list-failed": list_failed,
        "requeue": requeue,
        "stats": stats,
        "reset-reconcile-state": reset_reconcile_state,
    }[
        args.cmd
    ](args, db)


if __name__ == "__main__":
    main()
