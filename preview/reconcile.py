"""Reconcile: bootstrap walk + steady-state catch-up."""

from datetime import datetime, timedelta, timezone

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

    store.db.collection("state").document("reconcile").update(
        {
            "oldestCovered": new_oldest,
            "isBootstrap": is_bootstrap,
            "updatedAt": datetime.now(tz=timezone.utc),
        }
    )
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

    store.db.collection("state").document("reconcile").update(
        {
            "lastReconciledThrough": now,
            "updatedAt": now,
        }
    )
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
