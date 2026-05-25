"""Reconcile: bootstrap walk + steady-state catch-up."""

import time
from datetime import datetime, timedelta, timezone

BOOTSTRAP_WINDOW_DAYS = 28
BOOTSTRAP_FLOOR_YEARS = 10
STEADY_OVERLAP_HOURS = 1

# Per-invocation budget for bootstrap mode. We keep walking chunks backward
# until ANY of these trips, then stop and return; the next scheduler tick
# resumes from oldestCovered.
#
# Why these numbers:
#  - Cloud Run default request timeout is 300s; 240s leaves 60s safety.
#  - Wrike documents ~400 req/min as the soft cap; 100 calls/invocation
#    keeps reconcile well clear and leaves headroom for /tick to fire
#    concurrently. Each chunk is 1 list call + 1 per page beyond the first;
#    on thin accounts most chunks are 1 call.
BOOTSTRAP_TIME_BUDGET_S = 240
BOOTSTRAP_API_CALL_BUDGET = 100


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
    """Walk 28-day windows backward until floor reached or per-invocation
    budget exhausted. Persists oldestCovered after each window so a partial
    run resumes cleanly on the next /reconcile call."""
    start_time = time.monotonic()
    total_enqueued = 0
    chunks_walked = 0
    api_calls = 0
    oldest_covered = state["oldestCovered"]
    floor = state["bootstrapFloor"]
    is_bootstrap = True
    stop_reason = None

    while True:
        if oldest_covered <= floor:
            is_bootstrap = False
            stop_reason = "reached_floor"
            break

        elapsed = time.monotonic() - start_time
        if elapsed >= BOOTSTRAP_TIME_BUDGET_S:
            stop_reason = "time_budget"
            break
        if api_calls >= BOOTSTRAP_API_CALL_BUDGET:
            stop_reason = "api_budget"
            break

        window_end = oldest_covered
        window_start = max(window_end - timedelta(days=BOOTSTRAP_WINDOW_DAYS), floor)

        enqueued, calls_made = _walk_and_enqueue(store, wrike, window_start, window_end)
        total_enqueued += enqueued
        api_calls += calls_made
        chunks_walked += 1

        # Persist progress after each window so a mid-loop crash (OOM,
        # container kill) doesn't lose ground — next call resumes here.
        oldest_covered = window_start
        store.db.collection("state").document("reconcile").update(
            {
                "oldestCovered": oldest_covered,
                "updatedAt": datetime.now(tz=timezone.utc),
            }
        )

    if not is_bootstrap:
        # We've reached the floor — flip out of bootstrap mode.
        store.db.collection("state").document("reconcile").update(
            {
                "isBootstrap": False,
                "updatedAt": datetime.now(tz=timezone.utc),
            }
        )

    return {
        "mode": "bootstrap",
        "chunks_walked": chunks_walked,
        "enqueued": total_enqueued,
        "api_calls": api_calls,
        "elapsed_s": round(time.monotonic() - start_time, 1),
        "oldest_covered_after": oldest_covered.isoformat(),
        "is_bootstrap_after": is_bootstrap,
        "stop_reason": stop_reason,
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
        enqueued, _ = _walk_and_enqueue(store, wrike, cursor_start, cursor_end)
        total_enqueued += enqueued
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


def _walk_and_enqueue(store, wrike, window_start, window_end) -> tuple[int, int]:
    """Walk all pages of a date window and enqueue new attachments.
    Returns (enqueued_count, api_calls_made)."""
    enqueued = 0
    api_calls = 0
    page_token = None
    while True:
        items, page_token = wrike.list_account_attachments(
            created_from=window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            created_to=window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            next_page_token=page_token,
        )
        api_calls += 1
        for item in items:
            task_id = item.get("taskId")
            if store.create_job_if_absent(item["id"], task_id=task_id):
                enqueued += 1
        if not page_token:
            break
    return enqueued, api_calls
